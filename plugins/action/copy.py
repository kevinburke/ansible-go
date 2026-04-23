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
import json
import os
import stat

from ansible.errors import AnsibleActionFail, AnsibleFileNotFound
from ansible.module_utils.common.text.converters import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.hashing import checksum


class ActionModule(ActionBase):

    def _run_builtin_copy(self, tmp, task_vars):
        """Delegate to the builtin copy action plugin.

        We must dispatch to the *action plugin*, not the module. The copy
        module expects `src:` to be readable on the remote host; it's the
        action plugin that walks a local directory, pushes each file into a
        remote tempdir, and invokes the module with the remote path. Calling
        `_execute_module("ansible.builtin.copy")` here ships our controller
        path (e.g. `/Users/kevin/src/foo/migrations/`) to the remote and
        fails with "Source ... not found".

        `ansible.legacy.copy` resolves to `ansible.builtin.copy`: this
        override lives under `kevinburke.fastagent.copy`, so the legacy
        path doesn't loop back into us.
        """
        action = self._shared_loader_obj.action_loader.get(
            "ansible.legacy.copy",
            task=self._task,
            connection=self._connection,
            play_context=self._play_context,
            loader=self._loader,
            templar=self._templar,
            shared_loader_obj=self._shared_loader_obj,
        )
        return action.run(task_vars=task_vars)

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
