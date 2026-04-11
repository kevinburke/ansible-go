"""fastagent action plugin override for systemd module.

When using the fastagent connection, this sends a Service RPC directly to the
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
                self._execute_module(task_vars=task_vars),
            )

        self._connection._connect()

        args = self._task.args
        name = args.get("name") or args.get("service") or args.get("unit")
        state = args.get("state")
        enabled = args.get("enabled")
        daemon_reload = boolean(args.get("daemon_reload", False), strict=False)
        daemon_reexec = boolean(args.get("daemon_reexec", False), strict=False)
        masked = args.get("masked")
        scope = args.get("scope", "system")
        no_block = boolean(args.get("no_block", False), strict=False)

        # Fall back for features we don't handle.
        if masked is not None or scope != "system" or daemon_reexec:
            return merge_hash(
                result,
                self._execute_module(task_vars=task_vars),
            )

        client = self._connection._agent_client
        check_mode = self._play_context.check_mode

        changed = False

        # Handle daemon-reload via Exec.
        if daemon_reload:
            if not check_mode:
                try:
                    exec_result = client.exec(
                        argv=["systemctl", "daemon-reload"],
                    )
                    if exec_result.get("rc", 1) != 0:
                        result["failed"] = True
                        result["msg"] = f"daemon-reload failed: {exec_result.get('stderr', '')}"
                        return result
                except Exception as e:
                    result["failed"] = True
                    result["msg"] = f"daemon-reload failed: {e}"
                    return result
            changed = True

        if not name:
            result["changed"] = changed
            return result

        if check_mode:
            result["changed"] = True
            result["name"] = name
            return result

        # Convert enabled to bool if it's a string.
        if enabled is not None:
            enabled = boolean(enabled, strict=False)

        try:
            svc_result = client.service(
                name=name,
                manager="systemd",
                state=state,
                enabled=enabled,
            )
            result["changed"] = svc_result.get("changed", False) or changed
            result["name"] = name
            if svc_result.get("state"):
                result["state"] = svc_result["state"]
            if "enabled" in svc_result:
                result["enabled"] = svc_result["enabled"]
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent systemd failed: {e}"

        return result
