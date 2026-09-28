"""fastagent action plugin override for stat module.

When using the fastagent connection, this sends a Stat RPC directly to the
agent instead of transferring and executing the Python module.
For non-fastagent connections, falls back to normal module execution.

The result must be indistinguishable from ansible.builtin.stat's. The agent
does the work that has to happen on the remote host (stat, path expansion,
realpath, running `file` and `lsattr`); everything else, including parsing
the `file` and `lsattr` output, is done here with the same Python the stock
module runs. tests/test_stat_differential.py compares the two directly.
"""

from __future__ import annotations

from ansible.errors import AnsibleError
from ansible.module_utils.common.arg_spec import ArgumentSpecValidator
from ansible.module_utils.common.file import format_attributes
from ansible.plugins.action import ActionBase
from ansible.release import __version__ as ansible_version
from ansible.utils.vars import merge_hash


def _core_version() -> tuple[int, int]:
    major, minor = ansible_version.split(".")[:2]
    return int(major), int("".join(c for c in minor if c.isdigit()) or 0)


# ansible.builtin.stat's argument_spec. Only `path` differs: stock declares
# type='path', which expands ~ and $VARS in the *remote* environment, so we
# take it as a string and let the agent expand it.
_ARGUMENT_SPEC = dict(
    path=dict(type="str", required=True, aliases=["dest", "name"]),
    follow=dict(type="bool", default=False),
    get_checksum=dict(type="bool", default=True),
    get_mime=dict(type="bool", default=True, aliases=["mime", "mime_type", "mime-type"]),
    get_attributes=dict(type="bool", default=True, aliases=["attr", "attributes"]),
    checksum_algorithm=dict(
        type="str", default="sha1",
        choices=["md5", "sha1", "sha224", "sha256", "sha384", "sha512"],
        aliases=["checksum", "checksum_algo"],
    ),
)
# get_selinux_context was added to stat in ansible-core 2.20. On older
# controllers the stock module rejects it as an unsupported parameter, so
# leave it out of the spec: validation then fails and we fall back to the
# builtin module, which reports that error in its own words.
if _core_version() >= (2, 20):
    _ARGUMENT_SPEC["get_selinux_context"] = dict(type="bool", default=False)

# format_output's platform-dependent fields, in stock's order. Linux has
# the first three; macOS and the BSDs add flags, generation and birthtime.
_PLATFORM_FIELDS = ("blocks", "block_size", "device_type", "flags", "generation")

# stat's disk_usage_bytes return value was added in ansible-core 2.21
# (first in v2.21.0b1). Older stock modules don't return it, so neither do we.
_HAS_DISK_USAGE = _core_version() >= (2, 21)

# st_blocks counts 512-byte units on every POSIX system regardless of the
# filesystem's block size (st_blksize); stock computes st_blocks * 512.
_ST_BLOCKS_UNIT = 512


def _stat_time(sec, nsec) -> float:
    # CPython's os.stat_result float times are `sec + nsec * 1e-9`.
    return sec + nsec * 1e-9


class ActionModule(ActionBase):

    def _builtin(self, result, task_vars):
        # Explicit module_name so the call hits ansible.builtin.stat rather
        # than our shim module.
        return merge_hash(
            result,
            self._execute_module(module_name="ansible.builtin.stat", task_vars=task_vars),
        )

    def _compute_environment_dict(self):
        """Template + merge ``self._task.environment`` into a flat dict.

        Stock stat sees the task's `environment:` because Ansible prepends
        it to the module command; it decides where `file` and `lsattr` are
        found and what ~ and $VARS in `path` expand to. Keep in sync with
        the same method in command.py.
        """
        final: dict[str, str] = {}
        envs = self._task.environment
        if envs is None:
            return final
        if not isinstance(envs, list):
            envs = [envs]
        for entry in envs:
            if not entry:
                continue
            templated = self._templar.template(entry)
            if not isinstance(templated, dict):
                raise AnsibleError(
                    "environment must template to a dict, got %r" % (templated,)
                )
            final.update({str(k): str(v) for k, v in templated.items()})
        return final

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super().run(tmp, task_vars)
        del tmp

        if self._connection.transport != "fastagent":
            return self._builtin(result, task_vars)

        self._connection._connect()

        # The Stat RPC runs as the agent's uid (root when become is in
        # effect). Running stat as root would see files the become_user
        # couldn't — a permission leak — so the agent rejects Stat with
        # a become target. Fall back to the builtin module, which goes
        # through Ansible's normal become path and runs stat as the
        # target user.
        if self._connection.get_become_user() is not None:
            return self._builtin(result, task_vars)

        # Validate with stock's spec so aliases, defaults and bool parsing
        # match. On any problem, let the stock module report it in its own
        # words.
        validation = ArgumentSpecValidator(_ARGUMENT_SPEC).validate(dict(self._task.args))
        if validation.error_messages:
            return self._builtin(result, task_vars)
        params = validation.validated_parameters

        # SELinux contexts come from libselinux on the remote host, which
        # the agent does not link.
        if params.get("get_selinux_context"):
            return self._builtin(result, task_vars)

        get_checksum = params["get_checksum"]
        get_mime = params["get_mime"]
        get_attributes = params["get_attributes"]

        client = self._connection._agent_client
        try:
            res = client.stat(
                params["path"],
                follow=params["follow"],
                checksum=get_checksum,
                checksum_algorithm=params["checksum_algorithm"] if get_checksum else None,
                builtin=True,
                mime=get_mime,
                attributes=get_attributes,
                env=self._compute_environment_dict(),
            )
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent stat failed: {e}"
            return result

        if res.get("strerror"):
            # Stock: module.fail_json(msg=ex.strerror, exception=ex)
            result["failed"] = True
            result["msg"] = res["strerror"]
            return result

        result["stat"] = self._format(res, get_mime, get_attributes)
        result["changed"] = False
        return result

    @staticmethod
    def _format(res: dict, get_mime: bool, get_attributes: bool) -> dict:
        """Build the dict ansible.builtin.stat returns, in its key order.

        The agent omits zero values on the wire, so every field stock
        always sets gets an explicit default here.
        """
        if not res.get("exists", False):
            return {"exists": False}

        mode = int(res.get("mode") or "0", 8)
        output = {
            "exists": True,
            "path": res.get("path", ""),
            # Stock: "%04o" % stat.S_IMODE(mode). The agent already masked
            # to S_IMODE, so this only pads to four octal digits.
            "mode": "%04o" % mode,
            "isdir": bool(res.get("isdir")),
            "ischr": bool(res.get("ischr")),
            "isblk": bool(res.get("isblk")),
            "isreg": bool(res.get("isreg")),
            "isfifo": bool(res.get("isfifo")),
            "islnk": bool(res.get("islnk")),
            "issock": bool(res.get("issock")),
            "uid": res.get("uid", 0),
            "gid": res.get("gid", 0),
            "size": res.get("size", 0),
            "inode": res.get("inode", 0),
            "dev": res.get("dev", 0),
            "nlink": res.get("nlink", 0),
            "atime": _stat_time(res.get("atime", 0), res.get("atime_nsec", 0)),
            "mtime": _stat_time(res.get("mtime", 0), res.get("mtime_nsec", 0)),
            "ctime": _stat_time(res.get("ctime", 0), res.get("ctime_nsec", 0)),
        }
        for k in ("wusr", "rusr", "xusr", "wgrp", "rgrp", "xgrp",
                  "woth", "roth", "xoth", "isuid", "isgid"):
            output[k] = bool(res.get(k))

        platform = res.get("platform") or {}
        for k in _PLATFORM_FIELDS:
            if k in platform:
                output[k] = platform[k]
                if k == "blocks" and _HAS_DISK_USAGE:
                    output["disk_usage_bytes"] = platform[k] * _ST_BLOCKS_UNIT
        if "birthtime_sec" in platform:
            output["birthtime"] = _stat_time(
                platform["birthtime_sec"], platform.get("birthtime_nsec", 0))

        for k in ("readable", "writeable", "executable"):
            output[k] = bool(res.get(k))

        if output["islnk"]:
            output["lnk_source"] = res.get("lnk_source", "")
            output["lnk_target"] = res.get("lnk_target", "")

        # Stock sets pw_name/gr_name only when the lookup succeeds.
        if res.get("owner"):
            output["pw_name"] = res["owner"]
        if res.get("group"):
            output["gr_name"] = res["group"]

        if "checksum" in res:
            output["checksum"] = res["checksum"]

        # From here to the end mirrors stat.py's main() line for line.
        if get_mime:
            output["mimetype"] = output["charset"] = "unknown"
            cmd = res.get("file_cmd")
            if cmd is not None:
                rc, out = cmd.get("rc"), cmd.get("stdout", "")
                try:
                    if rc == 0:
                        mimetype, charset = out.rsplit(":", 1)[1].split(";")
                        output["mimetype"] = mimetype.strip()
                        output["charset"] = charset.split("=")[1].strip()
                except Exception:
                    pass

        if get_attributes:
            output["version"] = None
            output["attributes"] = []
            output["attr_flags"] = ""
            # AnsibleModule.get_file_attributes(path, include_version=True)
            attrs: dict = {}
            cmd = res.get("lsattr_cmd")
            if cmd is not None:
                rc, out = cmd.get("rc"), cmd.get("stdout", "")
                try:
                    if rc == 0:
                        fields = out.split()
                        attrs["version"] = fields[0].strip()
                        attrs["attr_flags"] = fields[1].replace("-", "").strip()
                        attrs["attributes"] = format_attributes(attrs["attr_flags"])
                except Exception:
                    pass
            for x in ("version", "attributes", "attr_flags"):
                if x in attrs:
                    output[x] = attrs[x]

        return output
