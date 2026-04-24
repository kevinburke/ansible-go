"""fastagent action plugin override for copy module.

When using the fastagent connection, this bypasses normal module transfer for
simple local-to-remote file copies. For non-fastagent connections or complex
cases (remote_src, directory recursion, content parameter), it falls back to
the builtin copy action.

This also accelerates `template` because the builtin template action renders
locally and then dispatches to `ansible.legacy.copy`.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat

import ansible.plugins.action as _ansible_action_pkg
from ansible.errors import AnsibleActionFail, AnsibleFileNotFound
from ansible.module_utils.common.text.converters import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.hashing import checksum


def _load_builtin_copy_action_class():
    """Return ansible-core's builtin `copy` ActionModule class.

    We can't use `from ansible.plugins.action.copy import ActionModule`:
    callers that put this plugin on the legacy `action_plugins` search
    path (e.g. caracal-server's `ansible.cfg`, to shadow unqualified
    `copy:`) cause ansible's PluginLoader to register *this file* under
    `sys.modules["ansible.plugins.action.copy"]`, aliasing over the
    real builtin. The import then resolves back into this partially-
    loaded module and fails with `ImportError: cannot import name
    'ActionModule' from 'ansible.plugins.action.copy' (…/kevinburke/…/copy.py)`.

    We can't use `action_loader.get("ansible.legacy.copy")` either: under
    the same legacy-path shadowing, `ansible.legacy.copy` resolves to
    this class, so the fallback recurses until CPython raises
    `RecursionError: maximum recursion depth exceeded`.

    Instead, find the real file on disk via the parent package's
    `__path__` (which is not mutated by legacy-plugin registration) and
    load it with `importlib.util.spec_from_file_location` under a name
    that can't clash with anything in `sys.modules`.
    """
    for base in _ansible_action_pkg.__path__:
        candidate = os.path.join(base, "copy.py")
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location(
                "kevinburke.fastagent._builtin_copy_action", candidate
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.ActionModule
    raise AnsibleActionFail(
        "fastagent: could not locate ansible-core's builtin copy "
        f"action plugin under {list(_ansible_action_pkg.__path__)}"
    )


_BUILTIN_COPY_ACTION_CLASS: type | None = None


def _shield_builtin_from_legacy_shims(builtin):
    """Rewrite `_execute_module` on a builtin action instance so any
    `ansible.legacy.<name>` call becomes `ansible.builtin.<name>`.

    Callers register this plugin on ansible.cfg's `library` / `action_plugins`
    search paths to shadow unqualified `copy:` / `stat:` / `file:` tasks.
    A side effect is that every `ansible.legacy.*` module resolution also
    picks up our files first — including the refusal shims in
    `plugins/modules/{stat,copy,file}.py`, which exist to say "you should
    have gone through the action plugin". When the builtin copy action
    we fell back to calls `_execute_module("ansible.legacy.stat")` inside
    `_execute_remote_stat`, it hits those shims and dies.

    `ansible.builtin.*` resolves through the collection loader (not the
    legacy library search path), so it always finds ansible-core's real
    modules regardless of our legacy shadowing.
    """
    original = builtin._execute_module
    legacy_prefix = "ansible.legacy."
    builtin_prefix = "ansible.builtin."

    def _execute_module_rewrite(*args, **kwargs):
        name = kwargs.get("module_name")
        if isinstance(name, str) and name.startswith(legacy_prefix):
            kwargs["module_name"] = builtin_prefix + name[len(legacy_prefix):]
        return original(*args, **kwargs)

    builtin._execute_module = _execute_module_rewrite


class ActionModule(ActionBase):

    def _run_builtin_copy(self, tmp, task_vars):
        """Delegate to the builtin copy action plugin.

        We must dispatch to the *action plugin*, not the copy module. The
        copy module expects `src:` to be readable on the remote host; it's
        the action plugin that walks a local directory, pushes each file
        into a remote tempdir, and only then invokes the module with a
        remote path. Calling `_execute_module("ansible.builtin.copy")`
        ships our controller path (e.g. `/Users/.../migrations/`) to the
        remote and fails with `Source ... not found`.

        See `_load_builtin_copy_action_class` for why we can't reach the
        builtin via `ansible.legacy.copy` or a plain import when callers
        expose this plugin on the legacy action_plugins search path.

        Once we have the builtin action instance, we also need to keep it
        from re-entering our legacy-path shims. The ansible-core copy
        action internally calls `_execute_module("ansible.legacy.stat")`,
        `ansible.legacy.copy`, and `ansible.legacy.file` — each of which,
        under caracal-server's `library = .../fastagent/modules` config,
        resolves to the kevinburke.fastagent shim that refuses direct
        invocation ("shim was invoked directly"). We rewrite those calls
        to `ansible.builtin.*` on this one instance so the real core
        modules run on the remote without disturbing global state.
        """
        global _BUILTIN_COPY_ACTION_CLASS
        if _BUILTIN_COPY_ACTION_CLASS is None:
            _BUILTIN_COPY_ACTION_CLASS = _load_builtin_copy_action_class()
        builtin = _BUILTIN_COPY_ACTION_CLASS(
            task=self._task,
            connection=self._connection,
            play_context=self._play_context,
            loader=self._loader,
            templar=self._templar,
            shared_loader_obj=self._shared_loader_obj,
        )
        _shield_builtin_from_legacy_shims(builtin)
        return builtin.run(task_vars=task_vars)

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super().run(tmp, task_vars)
        del tmp

        # Fall back for non-fastagent connections.
        if self._connection.transport != "fastagent":
            return self._run_builtin_copy(None, task_vars)

        # Fall back when become is active. The fast path uses Stat and
        # ReadFile RPCs that the agent refuses to serve with a
        # become_user (to avoid silently reading as root), and would
        # write files owned by root instead of by the become_user
        # without an explicit owner/group.
        if self._connection.get_become_user() is not None:
            return self._run_builtin_copy(None, task_vars)

        args = self._task.args
        remote_src = boolean(args.get("remote_src", False), strict=False)

        # Fall back for cases we don't handle in the fast path.
        if remote_src:
            return self._run_builtin_copy(None, task_vars)

        source = args.get("src")
        content = args.get("content")
        dest = args.get("dest")

        if not dest:
            result["failed"] = True
            result["msg"] = "dest is required"
            return result
        if not source and content is None:
            result["failed"] = True
            result["msg"] = "src (or content) is required"
            return result
        if source and content is not None:
            result["failed"] = True
            result["msg"] = "src and content are mutually exclusive"
            return result

        # Handle content parameter: write to bytes directly.
        if content is not None:
            if isinstance(content, (dict, list)):
                data = json.dumps(content).encode("utf-8")
            else:
                data = to_bytes(content)
            return self._fastagent_copy_data(data, dest, args, task_vars)

        # Resolve source file.
        try:
            source = self._find_needle("files", source)
        except Exception:
            return self._run_builtin_copy(None, task_vars)

        source_stat = os.stat(source)

        # Fall back if source is a directory (recursive copy is complex).
        if stat.S_ISDIR(source_stat.st_mode):
            return self._run_builtin_copy(None, task_vars)

        # Resolve the source through DataLoader.get_real_file so that
        # vault-encrypted files are decrypted into a temp file before we
        # read them. Reading `source` directly would land raw vault
        # ciphertext on the remote, silently corrupting any secret the
        # playbook was trying to deploy.  See get_real_file() in
        # https://github.com/ansible/ansible/blob/devel/lib/ansible/parsing/dataloader.py
        try:
            real_source = self._loader.get_real_file(source, decrypt=True)
        except Exception:
            return self._run_builtin_copy(None, task_vars)

        with open(real_source, "rb") as f:
            data = f.read()

        return self._fastagent_copy_data(data, dest, args, task_vars)

    def _fastagent_copy_data(self, data, dest, args, task_vars):
        """Copy data bytes to dest via fastagent RPC."""
        result = super().run(None, task_vars)

        # Ensure the connection is established before accessing _agent_client.
        self._connection._connect()

        check_mode = self._play_context.check_mode
        diff = self._play_context.diff
        backup = boolean(args.get("backup", False), strict=False)
        force = boolean(args.get("force", True), strict=False)
        owner = args.get("owner")
        group = args.get("group")
        mode = args.get("mode")
        unsafe_writes = boolean(args.get("unsafe_writes", False), strict=False)

        # Compute local checksum.
        local_checksum = hashlib.sha256(data).hexdigest()

        # Remote stat to check current state.
        client = self._connection._agent_client

        try:
            remote_stat = client.stat(dest, follow=True, checksum=True)
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent stat failed: {e}"
            return result

        # If dest is a directory, append the source basename.
        if remote_stat.get("exists") and remote_stat.get("isdir"):
            src = args.get("src", "")
            basename = os.path.basename(src) if src else "content"
            dest = os.path.join(dest, basename)
            # Re-stat with the full path.
            try:
                remote_stat = client.stat(dest, follow=True, checksum=True)
            except Exception as e:
                result["failed"] = True
                result["msg"] = f"fastagent stat failed: {e}"
                return result

        changed = True

        if remote_stat.get("exists") and not remote_stat.get("isdir", False):
            remote_checksum = remote_stat.get("checksum", "")
            if remote_checksum == local_checksum and force:
                changed = False

        if not changed:
            # Still need to check ownership/mode.
            result["changed"] = False
            result["dest"] = dest
            result["checksum"] = local_checksum

            # Apply ownership/mode even if content unchanged.
            if not check_mode and (owner or group or mode):
                try:
                    file_result = client.file(
                        path=dest,
                        state="file",
                        owner=owner,
                        group=group,
                        mode=self._format_mode(mode),
                    )
                    if file_result.get("changed"):
                        result["changed"] = True
                except Exception as e:
                    result["failed"] = True
                    result["msg"] = f"fastagent file attrs failed: {e}"

            return result

        # Build diff if requested.
        if diff:
            result["diff"] = []
            if remote_stat.get("exists") and not remote_stat.get("isdir", False):
                try:
                    old = client.read_file(dest)
                    old_content = base64.b64decode(old["content"])
                    result["diff"].append({
                        "before": to_text(old_content),
                        "after": to_text(data),
                        "before_header": dest,
                        "after_header": "new content",
                    })
                except Exception:
                    pass

        if check_mode:
            result["changed"] = True
            result["dest"] = dest
            return result

        # Write the file.
        content_b64 = base64.b64encode(data).decode("ascii")

        try:
            write_result = client.write_file(
                dest=dest,
                content=content_b64,
                owner=owner,
                group=group,
                mode=self._format_mode(mode),
                backup=backup,
                unsafe_writes=unsafe_writes,
            )
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent write failed: {e}"
            return result

        result["changed"] = write_result.get("changed", True)
        result["dest"] = dest
        result["checksum"] = write_result.get("checksum", local_checksum)
        if write_result.get("backup_file"):
            result["backup_file"] = write_result["backup_file"]

        return result

    def _format_mode(self, mode):
        """Format mode for the agent (expects octal string like '0644')."""
        if mode is None:
            return None
        if isinstance(mode, int):
            return f"0{mode:o}"
        mode_str = str(mode)
        # Ensure it looks like an octal string.
        if not mode_str.startswith("0"):
            mode_str = "0" + mode_str
        return mode_str
