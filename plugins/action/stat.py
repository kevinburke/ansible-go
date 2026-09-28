"""fastagent action plugin override for the stat module.

On the fastagent connection this answers `stat` tasks from the agent's Stat
RPC instead of shipping ansible.builtin.stat to the host, and returns the
result stock would return for the same task. Everything the RPC cannot
reproduce falls back to ansible.builtin.stat, which is always correct and,
since stat changes nothing, safe to run even after the RPC has run.

Division of labour with the agent (StatParams.Builtin in fastagent.go):

- The agent expands `~` and `$VARS` in `path` like Ansible's type='path'
  (with the task's `environment:`), calls lstat or stat, computes the
  checksum when the file is a regular file it can read, and runs `file` and
  `lsattr` the way a module's run_command would. It returns their raw output.
- This plugin validates the arguments with ansible-core's own validator,
  decides what to ask for, and turns the RPC result into stock's result:
  field names, float timestamps, mode formatting, and the parsing of the
  `file` and `lsattr` output.

The parsing rules below were established by observing ansible.builtin.stat
(including with stand-in `file` and `lsattr` programs placed on PATH through
`environment:`); tests/test_stat_differential.py keeps checking them.
"""

from __future__ import annotations

import re

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.arg_spec import ArgumentSpecValidator
from ansible.module_utils.common.text.converters import to_text
from ansible.plugins.action import ActionBase
from ansible.release import __version__ as _ANSIBLE_VERSION
from ansible.utils.display import Display
from ansible.utils.vars import merge_hash

try:
    from ansible_collections.kevinburke.fastagent.plugins.module_utils.fastagent_client import (
        FastAgentError,
    )
except ImportError:
    from plugins.module_utils.fastagent_client import FastAgentError

display = Display()

BUILTIN_MODULE = "ansible.builtin.stat"

# ansible.builtin.stat's options, as `ansible-doc ansible.builtin.stat`
# documents them. tests/test_stat_action.py compares this against
# `ansible-doc --json` so it cannot drift from the installed ansible-core.
#
# `path` is type='path' in stock, which expands `~` and `$VARS`. That must
# happen on the target (the agent does it), not on the controller, so it is
# validated here as 'raw' and checked to be a string separately.
ARGUMENT_SPEC = {
    "path": {"type": "path", "required": True, "aliases": ["dest", "name"]},
    "follow": {"type": "bool", "default": False},
    "get_checksum": {"type": "bool", "default": True},
    "get_mime": {
        "type": "bool",
        "default": True,
        "aliases": ["mime", "mime_type", "mime-type"],
    },
    "get_attributes": {
        "type": "bool",
        "default": True,
        "aliases": ["attr", "attributes"],
    },
    "get_selinux_context": {"type": "bool", "default": False},
    "checksum_algorithm": {
        "type": "str",
        "default": "sha1",
        "choices": ["md5", "sha1", "sha224", "sha256", "sha384", "sha512"],
        "aliases": ["checksum", "checksum_algo"],
    },
}

# The fast path reproduces the stock module of the ansible-core release the
# controller runs. It has been compared against these releases and newer;
# older ones fall back rather than risk returning a different shape.
MIN_FAST_PATH_ANSIBLE = (2, 20)

# ansible-core 2.21 added `disk_usage_bytes` (st_blocks * 512).
DISK_USAGE_BYTES_ANSIBLE = (2, 21)

# StatResult booleans, which the agent omits when false, copied under the
# same names.
_BOOL_FIELDS = (
    "isdir", "ischr", "isblk", "isreg", "isfifo", "islnk", "issock",
    "isuid", "isgid",
    "rusr", "wusr", "xusr", "rgrp", "wgrp", "xgrp", "roth", "woth", "xoth",
    "readable", "writeable", "executable",
)

# StatResult integers, which the agent omits when zero.
_INT_FIELDS = ("uid", "gid", "size", "inode", "dev", "nlink")

# StatResult.Platform keys copied through as integers. Keys not listed here
# or in _PLATFORM_TIMES make the plugin fall back, so a new agent-side field
# can never be dropped or misreported silently.
_PLATFORM_INTS = ("blocks", "block_size", "device_type", "flags", "generation")

# Platform timestamps sent as <name>_sec and <name>_nsec.
_PLATFORM_TIMES = ("birthtime",)


class Unsupported(Exception):
    """The fast path cannot answer this task; run ansible.builtin.stat."""


def ansible_version_tuple(version: str | None = None) -> tuple[int, int]:
    """(major, minor) of `version`, by default the running ansible-core."""
    match = re.match(r"(\d+)\.(\d+)", _ANSIBLE_VERSION if version is None else version)
    if match is None:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def validate_args(task_args: dict) -> dict:
    """Validate task args the way the stock module does.

    Returns the validated parameters (aliases resolved, booleans converted,
    defaults applied). Raises Unsupported whenever stock would report an
    error, warning or deprecation, so stock gets to report it verbatim.
    """
    spec = {name: dict(opts) for name, opts in ARGUMENT_SPEC.items()}
    spec["path"]["type"] = "raw"

    # Stock warns when an option and one of its aliases are both set.
    for name, opts in spec.items():
        given = [k for k in [name] + opts.get("aliases", []) if k in task_args]
        if len(given) > 1:
            raise Unsupported(f"both {given[0]} and {given[1]} are set")

    result = ArgumentSpecValidator(spec).validate(dict(task_args))
    if result.error_messages:
        raise Unsupported("; ".join(result.error_messages))
    if getattr(result, "_warnings", None) or getattr(result, "_deprecations", None):
        raise Unsupported("argument validation produced warnings")

    params = result.validated_parameters
    path = params.get("path")
    if not isinstance(path, str) or not path or "\x00" in path:
        # Stock's own conversions and errors for these (it stats str(None)
        # for a null path, for instance) are not worth reproducing.
        raise Unsupported(f"path {path!r} is not a non-empty string")
    return params


def parse_mime(file_cmd: dict | None) -> dict:
    """Parse `file --mime-type --mime-encoding PATH` output like stock.

    Observed behaviour of ansible.builtin.stat: both fields default to
    'unknown'. The output must exit 0; the text after its last ':' must
    split on ';' into exactly two parts. The mime type is the first part,
    stripped, and is kept even if the second part then fails to parse; the
    charset is whatever follows the first '=' of the second part, up to a
    second '=', stripped.
    """
    out = {"mimetype": "unknown", "charset": "unknown"}
    if file_cmd is None or file_cmd.get("rc", 0) != 0:
        return out
    try:
        mimetype, charset = file_cmd.get("stdout", "").rsplit(":", 1)[1].split(";")
        out["mimetype"] = mimetype.strip()
        out["charset"] = charset.split("=")[1].strip()
    except (IndexError, ValueError):
        pass
    return out


class _RecordedCommand:
    """Stands in for AnsibleModule when replaying an agent-run command.

    Lets ansible-core's own AnsibleModule.get_file_attributes parse the
    `lsattr` output the agent captured, and records the command it asked
    for so the caller can check the agent ran the same one.
    """

    def __init__(self, recorded: dict | None):
        self._recorded = recorded
        self.requested: list | None = None

    def get_bin_path(self, arg, required=False, opt_dirs=None):
        return arg if self._recorded is not None else None

    def run_command(self, args, *unused_args, **unused_kwargs):
        self.requested = list(args)
        return self._recorded.get("rc", 0), self._recorded.get("stdout", ""), ""


def parse_attributes(lsattr_cmd: dict | None, path: str) -> dict:
    """Build stock's attribute fields from the agent's `lsattr -vd` run.

    Stock starts from version=None, attr_flags='' and attributes=[] and
    overlays AnsibleModule.get_file_attributes(path), which is called here
    with the agent's captured output.
    """
    out = {"version": None, "attr_flags": "", "attributes": []}
    replay = _RecordedCommand(lsattr_cmd)
    try:
        parsed = AnsibleModule.get_file_attributes(replay, path, include_version=True)
    except Exception as e:
        raise Unsupported(f"get_file_attributes failed on agent output: {e}")
    if lsattr_cmd is not None and replay.requested != ["lsattr", "-vd", path]:
        # ansible-core now runs a different command than the agent did.
        raise Unsupported(f"get_file_attributes ran {replay.requested!r}")
    out.update(parsed)
    return out


def _stat_time(rpc: dict, name: str) -> float:
    # CPython builds os.stat_result's float times as sec + nsec * 1e-9.
    return rpc.get(name, 0) + rpc.get(name + "_nsec", 0) * 1e-9


def build_stat(rpc: dict, params: dict, ansible_version: tuple[int, int]) -> dict:
    """Turn a Builtin Stat RPC result into stock's `stat` dictionary."""
    path = rpc.get("path")
    if not isinstance(path, str):
        raise Unsupported("agent result has no path")
    if not rpc.get("exists"):
        return {"exists": False}

    st = {"exists": True, "path": path}
    try:
        st["mode"] = "%04o" % int(rpc["mode"], 8)
    except (KeyError, TypeError, ValueError):
        raise Unsupported(f"agent returned mode {rpc.get('mode')!r}")
    for name in _INT_FIELDS:
        st[name] = rpc.get(name, 0)
    for name in ("atime", "mtime", "ctime"):
        st[name] = _stat_time(rpc, name)
    for name in _BOOL_FIELDS:
        st[name] = bool(rpc.get(name, False))

    platform = rpc.get("platform")
    if not isinstance(platform, dict):
        # The agent has not mapped this OS's os.stat_result fields.
        raise Unsupported("agent reported no platform stat fields")
    known = set(_PLATFORM_INTS)
    for name in _PLATFORM_TIMES:
        known.update((name + "_sec", name + "_nsec"))
    unknown = sorted(set(platform) - known)
    if unknown:
        raise Unsupported(f"unknown platform stat fields {unknown}")
    for name in _PLATFORM_INTS:
        if name in platform:
            st[name] = platform[name]
    for name in _PLATFORM_TIMES:
        if name + "_sec" in platform:
            st[name] = platform[name + "_sec"] + platform.get(name + "_nsec", 0) * 1e-9
    if ansible_version >= DISK_USAGE_BYTES_ANSIBLE and "blocks" in platform:
        st["disk_usage_bytes"] = platform["blocks"] * 512

    if rpc.get("owner"):
        st["pw_name"] = rpc["owner"]
    if rpc.get("group"):
        st["gr_name"] = rpc["group"]

    if st["islnk"]:
        if not rpc.get("lnk_target") or not rpc.get("lnk_source"):
            raise Unsupported("agent could not read the symlink")
        st["lnk_target"] = rpc["lnk_target"]
        st["lnk_source"] = rpc["lnk_source"]

    if params["get_checksum"] and st["isreg"] and st["readable"]:
        if not rpc.get("checksum"):
            raise Unsupported("agent returned no checksum for a readable file")
        st["checksum"] = rpc["checksum"]

    if params["get_mime"]:
        st.update(parse_mime(rpc.get("file_cmd")))
    if params["get_attributes"]:
        st.update(parse_attributes(rpc.get("lsattr_cmd"), path))
    return st


def _check_command_output(rpc: dict) -> None:
    for key in ("file_cmd", "lsattr_cmd"):
        cmd = rpc.get(key)
        if cmd is not None and "�" in cmd.get("stdout", ""):
            # Go replaced bytes that are not UTF-8. Stock would see them;
            # let it decide what to do with them.
            raise Unsupported(f"{key} output is not valid UTF-8")


class ActionModule(ActionBase):

    _supports_check_mode = True
    _supports_async = True

    def _run_builtin(self, result, task_vars, reason):
        display.vvv(f"fastagent stat: using {BUILTIN_MODULE}: {reason}")
        wrap_async = self._task.async_val
        result = merge_hash(
            result,
            self._execute_module(
                module_name=BUILTIN_MODULE,
                task_vars=task_vars,
                wrap_async=wrap_async,
            ),
        )
        if not wrap_async:
            self._remove_tmp_path(self._connection._shell.tmpdir)
        return result

    def _task_environment(self) -> dict:
        """The task's `environment:`, templated and merged as ActionBase does."""
        env: dict = {}
        self._compute_environment_string(raw_environment_out=env)
        return {to_text(k): to_text(v) for k, v in env.items()}

    def _unsupported_reason(self) -> str | None:
        if self._connection.transport != "fastagent":
            return "not a fastagent connection"
        if self._task.async_val:
            return "async task"
        if ansible_version_tuple() < MIN_FAST_PATH_ANSIBLE:
            return f"ansible-core {_ANSIBLE_VERSION} is older than the fast path supports"
        # A become method the agent does not implement stays attached to
        # the connection for Ansible's own wrapping (see set_become_plugin).
        if getattr(self._connection, "become", None) is not None:
            return "become method handled by Ansible"
        # The agent stats as its own uid, the SSH user or root. Stat as
        # another user would see different permissions and access bits.
        if self._connection.get_become_user() is not None:
            return "non-root become_user"
        return None

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}
        result = super().run(tmp, task_vars)
        del tmp

        reason = self._unsupported_reason()
        if reason is not None:
            return self._run_builtin(result, task_vars, reason)

        try:
            params = validate_args(self._task.args)
            if params["get_selinux_context"]:
                raise Unsupported("get_selinux_context is not implemented by the agent")
            env = self._task_environment()
        except Unsupported as e:
            return self._run_builtin(result, task_vars, str(e))

        self._connection._connect()
        try:
            rpc = self._connection._agent_client.stat(
                params["path"],
                follow=params["follow"],
                checksum=params["get_checksum"],
                checksum_algorithm=params["checksum_algorithm"],
                builtin=True,
                mime=params["get_mime"],
                attributes=params["get_attributes"],
                env=env,
            )
        except FastAgentError as e:
            return self._run_builtin(result, task_vars, f"agent error: {e}")
        except Exception as e:
            display.warning(
                f"fastagent stat: Stat RPC failed, using {BUILTIN_MODULE}: {e}"
            )
            return self._run_builtin(result, task_vars, f"RPC failed: {e}")

        try:
            path = rpc.get("path")
            if not isinstance(path, str) or not path.startswith("/"):
                # Relative paths resolve against the module's working
                # directory, which the agent does not share by contract.
                raise Unsupported(f"path {path!r} is not absolute")
            if rpc.get("strerror"):
                # Stock fails with the OSError's strerror.
                result.update(changed=False, failed=True, msg=rpc["strerror"])
                return result
            _check_command_output(rpc)
            stat = build_stat(rpc, params, ansible_version_tuple())
        except Unsupported as e:
            return self._run_builtin(result, task_vars, str(e))

        result.update(changed=False, stat=stat)
        return result
