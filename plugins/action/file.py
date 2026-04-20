"""fastagent action plugin override for file module.

When using the fastagent connection, this sends File and Stat RPCs directly
to the agent instead of transferring and executing the Python module.
For non-fastagent connections, falls back to normal module execution.
"""

from __future__ import annotations

from ansible.plugins.action import ActionBase
from ansible.module_utils.common.text.converters import to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.utils.vars import merge_hash

try:
    from ansible_collections.kevinburke.fastagent.plugins.module_utils.file_state import (
        infer_file_state,
    )
except ImportError:
    from plugins.module_utils.file_state import infer_file_state


class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super().run(tmp, task_vars)
        del tmp

        # Fall back for non-fastagent connections. Use explicit module_name
        # so the call hits ansible.builtin.file rather than our shim module
        # (which only exists to make `collections:` routing select this
        # action plugin for unqualified `file:` tasks).
        if self._connection.transport != "fastagent":
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.file", task_vars=task_vars
                ),
            )

        self._connection._connect()

        # The file override uses Stat RPCs internally, which the agent
        # rejects when running as root on behalf of a become_user
        # (see plugins/action/stat.py for the rationale). Additionally,
        # running the File RPC as root would create files owned by
        # root, not by the become_user, diverging from the builtin
        # module's semantics. Fall back for become tasks.
        if getattr(self._connection, "_become_user", None) is not None:
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.file", task_vars=task_vars
                ),
            )

        args = self._task.args
        path = args.get("path") or args.get("dest") or args.get("name")
        state = args.get("state")
        owner = args.get("owner")
        group = args.get("group")
        mode = args.get("mode")
        recurse = boolean(args.get("recurse", False), strict=False)
        follow = boolean(args.get("follow", True), strict=False)
        src = args.get("src")
        force = boolean(args.get("force", False), strict=False)
        modification_time = args.get("modification_time")
        access_time = args.get("access_time")

        if not path:
            result["failed"] = True
            result["msg"] = "path is required"
            return result

        client = self._connection._agent_client
        check_mode = self._play_context.check_mode

        # Match ansible.builtin.file defaults: existing directories remain
        # directories, recurse implies directory, and src implies link.
        try:
            state = infer_file_state(client, path, state, src, recurse, follow)
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent stat failed: {e}"
            return result

        # For state=absent, we don't need to stat first.
        if state == "absent":
            if check_mode:
                try:
                    stat_result = client.stat(path)
                    result["changed"] = stat_result.get("exists", False)
                except Exception:
                    result["changed"] = False
                result["path"] = path
                result["state"] = "absent"
                return result

            try:
                file_result = client.file(path=path, state="absent")
                result["changed"] = file_result.get("changed", False)
                result["path"] = path
                result["state"] = "absent"
            except Exception as e:
                result["failed"] = True
                result["msg"] = f"fastagent file absent failed: {e}"
            return result

        # For state=directory.
        if state == "directory":
            if check_mode:
                try:
                    stat_result = client.stat(path, follow=follow)
                    result["changed"] = not stat_result.get("exists", False)
                except Exception:
                    result["changed"] = True
                result["path"] = path
                result["state"] = "directory"
                return result

            try:
                file_result = client.file(
                    path=path,
                    state="directory",
                    owner=owner,
                    group=group,
                    mode=self._format_mode(mode),
                    recurse=recurse,
                )
                result.update(file_result)
                result["path"] = path
                result["state"] = "directory"
            except Exception as e:
                result["failed"] = True
                result["msg"] = f"fastagent file directory failed: {e}"
            return result

        # For state=touch.
        if state == "touch":
            if check_mode:
                result["changed"] = True
                result["path"] = path
                result["state"] = "file"
                return result

            try:
                file_result = client.file(
                    path=path,
                    state="touch",
                    owner=owner,
                    group=group,
                    mode=self._format_mode(mode),
                )
                result.update(file_result)
                result["path"] = path
            except Exception as e:
                result["failed"] = True
                result["msg"] = f"fastagent file touch failed: {e}"
            return result

        # For state=link or state=hard.
        if state in ("link", "hard"):
            if not src:
                result["failed"] = True
                result["msg"] = "src is required for state=link/hard"
                return result

            if check_mode:
                result["changed"] = True
                result["path"] = path
                result["state"] = state
                return result

            try:
                file_result = client.file(
                    path=path,
                    state=state,
                    src=src,
                    owner=owner,
                    group=group,
                    mode=self._format_mode(mode),
                )
                result.update(file_result)
                result["path"] = path
            except Exception as e:
                result["failed"] = True
                result["msg"] = f"fastagent file link failed: {e}"
            return result

        # For state=file (default): ensure file exists and set attributes.
        try:
            stat_result = client.stat(path, follow=follow, checksum=False)
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent stat failed: {e}"
            return result

        if not stat_result.get("exists"):
            result["changed"] = False
            result["path"] = path
            result["state"] = "absent"
            return result

        if stat_result.get("isdir", False):
            result["failed"] = True
            result["msg"] = f"{path} is a directory, cannot use state=file"
            return result

        if check_mode:
            result["changed"] = False
            result["path"] = path
            result["state"] = "file"
            return result

        try:
            file_result = client.file(
                path=path,
                state="file",
                owner=owner,
                group=group,
                mode=self._format_mode(mode),
            )
            result.update(file_result)
            result["path"] = path
            result["state"] = "file"

            # Add stat-like fields that Ansible expects.
            stat_result = client.stat(path, follow=follow)
            result["uid"] = 0  # TODO: resolve from owner
            result["gid"] = 0
            result["owner"] = stat_result.get("owner", "")
            result["group"] = stat_result.get("group", "")
            result["mode"] = stat_result.get("mode", "")
            result["size"] = stat_result.get("size", 0)
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent file failed: {e}"
        return result

    def _format_mode(self, mode):
        if mode is None:
            return None
        if isinstance(mode, int):
            return f"0{mode:o}"
        mode_str = str(mode)
        if not mode_str.startswith("0"):
            mode_str = "0" + mode_str
        return mode_str
