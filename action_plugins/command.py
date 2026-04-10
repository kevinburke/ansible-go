"""fastagent action plugin override for command/shell modules.

When using the fastagent connection, this bypasses normal module transfer and
execution, sending an Exec RPC directly to the agent. For non-fastagent
connections, it falls back to the builtin behavior.

This works for both `command` and `shell` because the builtin shell action
sets `_uses_shell=True` and delegates to `ansible.legacy.command`, and our
action plugin name matches `command`.
"""

from __future__ import annotations

import datetime
import glob
import shlex

from ansible.plugins.action import ActionBase
from ansible.module_utils.common.text.converters import to_text
from ansible.utils.vars import merge_hash


class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        self._supports_async = True
        result = super().run(tmp, task_vars)
        del tmp

        # Fall back to builtin for non-fastagent connections.
        if self._connection.transport != "fastagent":
            return merge_hash(
                result,
                self._execute_module(
                    module_name="ansible.legacy.command",
                    task_vars=task_vars,
                    wrap_async=self._task.async_val,
                ),
            )

        # Ensure the connection is established before accessing _agent_client.
        self._connection._connect()

        args = self._task.args
        uses_shell = args.get("_uses_shell", False)
        raw_params = args.get("_raw_params", "")
        cmd = args.get("cmd", "")
        argv = args.get("argv")
        chdir = args.get("chdir")
        creates = args.get("creates")
        removes = args.get("removes")
        stdin = args.get("stdin")
        stdin_add_newline = args.get("stdin_add_newline", True)
        strip_empty_ends = args.get("strip_empty_ends", True)

        r = {
            "changed": False,
            "stdout": "",
            "stderr": "",
            "rc": None,
            "cmd": None,
            "start": None,
            "end": None,
            "delta": None,
            "msg": "",
        }

        # Determine the command to run.
        cmd_string = raw_params or cmd or ""
        if not uses_shell and cmd_string:
            cmd_parts = shlex.split(cmd_string)
        else:
            cmd_parts = None

        if argv:
            r["cmd"] = argv
        elif cmd_parts:
            r["cmd"] = cmd_parts
        else:
            r["cmd"] = cmd_string

        # creates/removes idempotence checks.
        check_mode = self._play_context.check_mode
        shoulda = "Would" if check_mode else "Did"

        if creates:
            # Send Stat RPC to check if file exists on remote.
            try:
                stat_result = self._connection._agent_client.stat(creates)
                if stat_result.get("exists"):
                    r["msg"] = f"{shoulda} not run command since '{creates}' exists"
                    r["stdout"] = f"skipped, since {creates} exists"
                    r["rc"] = 0
                    return merge_hash(result, r)
            except Exception:
                # If stat fails, fall through and try to run the command.
                pass

        if removes:
            try:
                stat_result = self._connection._agent_client.stat(removes)
                if not stat_result.get("exists"):
                    r["msg"] = f"{shoulda} not run command since '{removes}' does not exist"
                    r["stdout"] = f"skipped, since {removes} does not exist"
                    r["rc"] = 0
                    return merge_hash(result, r)
            except Exception:
                pass

        r["changed"] = True

        if check_mode:
            r["rc"] = 0
            r["msg"] = "Command would have run if not in check mode"
            if creates is None and removes is None:
                r["skipped"] = True
                r["changed"] = False
            return merge_hash(result, r)

        # Execute via fastagent RPC.
        start = datetime.datetime.now()

        try:
            exec_result = self._connection._agent_client.exec(
                argv=argv,
                cmd_string=cmd_string if not argv else None,
                use_shell=uses_shell,
                cwd=chdir,
                stdin=stdin,
                stdin_add_newline=stdin_add_newline,
                strip_empty_ends=strip_empty_ends,
            )
        except Exception as e:
            r["rc"] = 1
            r["msg"] = f"fastagent exec failed: {e}"
            r["failed"] = True
            return merge_hash(result, r)

        end = datetime.datetime.now()

        r["rc"] = exec_result.get("rc", 0)
        r["stdout"] = exec_result.get("stdout", "")
        r["stderr"] = exec_result.get("stderr", "")
        r["start"] = to_text(start)
        r["end"] = to_text(end)
        r["delta"] = to_text(end - start)
        r["stdout_lines"] = r["stdout"].splitlines()
        r["stderr_lines"] = r["stderr"].splitlines()

        if r["rc"] != 0:
            r["msg"] = "The command exited with a non-zero return code."
            r["failed"] = True

        return merge_hash(result, r)
