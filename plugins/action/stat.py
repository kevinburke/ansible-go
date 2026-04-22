"""fastagent action plugin override for stat module.

When using the fastagent connection, this sends a Stat RPC directly to the
agent instead of transferring and executing the Python module.
For non-fastagent connections, falls back to normal module execution.
"""

from __future__ import annotations

from ansible.plugins.action import ActionBase
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.utils.vars import merge_hash


class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super().run(tmp, task_vars)
        del tmp

        # Fall back for non-fastagent connections. Use explicit module_name
        # so the call hits ansible.builtin.stat rather than our shim module.
        if self._connection.transport != "fastagent":
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.stat", task_vars=task_vars
                ),
            )

        self._connection._connect()

        # The Stat RPC runs as the agent's uid (root when become is in
        # effect). Running stat as root would see files the become_user
        # couldn't — a permission leak — so the agent rejects Stat with
        # a become target. Fall back to the builtin module, which goes
        # through Ansible's normal become path and runs stat as the
        # target user.
        if self._connection.get_become_user() is not None:
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.stat", task_vars=task_vars
                ),
            )

        args = self._task.args
        path = args.get("path")
        follow = boolean(args.get("follow", False), strict=False)
        get_checksum = boolean(args.get("get_checksum", True), strict=False)
        checksum_algorithm = args.get("checksum_algorithm", "sha256")

        if not path:
            result["failed"] = True
            result["msg"] = "path is required"
            return result

        # We only support sha256 checksums (what the Go agent computes).
        # For other algorithms, fall back to the builtin module.
        if get_checksum and checksum_algorithm not in ("sha256", "sha-256"):
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.stat", task_vars=task_vars
                ),
            )

        client = self._connection._agent_client

        try:
            stat_result = client.stat(path, follow=follow, checksum=get_checksum)
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent stat failed: {e}"
            return result

        # Build the stat dict that Ansible expects.
        stat = {
            "exists": stat_result.get("exists", False),
        }

        if stat["exists"]:
            stat["path"] = stat_result.get("path", path)
            stat["isdir"] = stat_result.get("isdir", False)
            stat["islnk"] = stat_result.get("islnk", False)
            stat["isreg"] = not stat.get("isdir", False) and not stat.get("islnk", False)
            stat["mode"] = stat_result.get("mode", "")
            stat["owner"] = stat_result.get("owner", "")
            stat["group"] = stat_result.get("group", "")
            stat["size"] = stat_result.get("size", 0)
            stat["mtime"] = float(stat_result.get("mtime", 0))
            stat["atime"] = float(stat_result.get("atime", 0))

            if stat_result.get("islnk"):
                stat["lnk_source"] = stat_result.get("lnk_source", "")
                stat["lnk_target"] = stat_result.get("lnk_source", "")

            if get_checksum and not stat["isdir"] and stat.get("isreg", False):
                stat["checksum"] = stat_result.get("checksum", "")

        result["stat"] = stat
        result["changed"] = False
        return result
