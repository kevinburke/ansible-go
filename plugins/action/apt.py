"""fastagent action plugin override for apt module.

When using the fastagent connection, this sends a Package RPC directly to the
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

        if self._connection.transport != "fastagent":
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.apt", task_vars=task_vars
                ),
            )

        self._connection._connect()

        args = self._task.args
        names = args.get("name") or args.get("package") or args.get("pkg") or []
        if isinstance(names, str):
            names = [names]
        state = args.get("state", "present")
        update_cache = boolean(args.get("update_cache", False), strict=False)
        cache_valid_time = args.get("cache_valid_time", 0)
        purge = boolean(args.get("purge", False), strict=False)
        deb = args.get("deb")
        upgrade = args.get("upgrade")

        # Fall back for features we don't handle in the fast path.
        if deb or upgrade:
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.apt", task_vars=task_vars
                ),
            )

        # Normalize state.
        if state == "installed":
            state = "present"
        elif state == "removed":
            state = "absent"
        elif state == "build-dep":
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.builtin.apt", task_vars=task_vars
                ),
            )

        client = self._connection._agent_client
        check_mode = self._play_context.check_mode

        if check_mode:
            if names:
                result["changed"] = True
                result["msg"] = f"Would {state} {', '.join(names)}"
            else:
                result["changed"] = update_cache
                result["cache_updated"] = update_cache
            return result

        # Send everything to the Package RPC — it handles update_cache
        # deduplication internally (skips if cache was updated recently).
        try:
            pkg_result = client.call("Package", {
                "manager": "apt",
                "names": names,
                "state": state,
                "update_cache": update_cache,
                "cache_valid_time": cache_valid_time,
            })
            result["changed"] = pkg_result.get("changed", False)
            result["cache_updated"] = pkg_result.get("cache_updated", False)
            result["msg"] = pkg_result.get("msg", "")
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent apt failed: {e}"

        return result
