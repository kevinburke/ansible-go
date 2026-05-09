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
import shlex

from ansible.errors import AnsibleError
from ansible.plugins.action import ActionBase
from ansible.module_utils.common.text.converters import to_text
from ansible.utils.vars import merge_hash


class ActionModule(ActionBase):
    def _run_builtin_command(self, result, task_vars):
        wrap_async = self._task.async_val and not getattr(
            self._connection, "has_native_async", False
        )
        return merge_hash(
            result,
            self._execute_module(
                module_name="ansible.builtin.command",
                task_vars=task_vars,
                wrap_async=wrap_async,
            ),
        )

    def _compute_environment_dict(self):
        """Template + merge ``self._task.environment`` into a flat dict.

        Mirrors ``ActionBase._compute_environment_string`` but returns a
        dict suitable for the Exec RPC's ``env`` field rather than a
        shell prefix string. The action plugin bypasses Ansible's
        usual module pipeline, so the controller-side env-prefix path
        never runs — without this, ``environment:`` blocks declared on
        the task were silently dropped.
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
        self._supports_async = True
        result = super().run(tmp, task_vars)
        del tmp

        # Fall back to builtin for non-fastagent connections (e.g. when a
        # task uses delegate_to: localhost with the local connection).
        # Use ansible.builtin.command rather than ansible.legacy.command
        # because if the user has wired up library = .../fastagent/modules
        # in ansible.cfg, our command shim shadows ansible.legacy.command.
        if self._connection.transport != "fastagent":
            return self._run_builtin_command(result, task_vars)

        args = self._task.args
        uses_shell = args.get("_uses_shell", False)
        raw_params = args.get("_raw_params", "")
        cmd = args.get("cmd", "")
        argv = args.get("argv")
        # ansible.builtin.command's argument_spec coerces argv to
        # elements=str. The override path bypasses that, so a templated
        # int (e.g. "{{ vmid }}" with native jinja types) arrives as an
        # _AnsibleTaggedInt and breaks the scan below with
        # 'AttributeError: ... has no attribute startswith'. Match the
        # builtin's coercion up front so r["cmd"] and the RPC payload
        # are plain strings.
        if argv:
            argv = [to_text(arg) for arg in argv]
        chdir = args.get("chdir")
        creates = args.get("creates")
        removes = args.get("removes")
        executable = args.get("executable")
        expand_argument_vars = args.get("expand_argument_vars", True)
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

        # If the connection left a become plugin attached, it is a become
        # method the agent-side sudo wrapper does not implement. Let Ansible's
        # normal module path apply that plugin rather than silently running
        # with the wrong privilege model.
        if getattr(self._connection, "become", None) is not None:
            return self._run_builtin_command(result, task_vars)

        if executable:
            return self._run_builtin_command(result, task_vars)

        if not uses_shell and expand_argument_vars:
            for arg in r["cmd"] or []:
                if arg.startswith("~") or "$" in arg:
                    return self._run_builtin_command(result, task_vars)

        # creates/removes idempotence checks.
        check_mode = self._play_context.check_mode
        if check_mode and (creates or removes):
            return self._run_builtin_command(result, task_vars)

        r["changed"] = True

        if check_mode:
            r["rc"] = 0
            r["msg"] = "Command would have run if not in check mode"
            if creates is None and removes is None:
                r["skipped"] = True
                r["changed"] = False
            return merge_hash(result, r)

        # Execute via fastagent RPC.
        # For non-shell command tasks, send the shlex-parsed argv rather than
        # the raw cmd_string. The agent rejects non-shell cmd_string payloads
        # because it cannot parse them with Ansible's shlex semantics.
        # For shell tasks, send cmd_string so the remote shell does the
        # parsing.
        exec_argv = argv
        exec_cmd_string = None
        if exec_argv:
            pass
        elif uses_shell:
            exec_cmd_string = cmd_string
        else:
            exec_argv = cmd_parts

        start = datetime.datetime.now()

        # The action override bypasses Ansible's usual module pipeline,
        # so the connection's become-wrap in exec_command doesn't apply.
        # Pass become_user through the RPC instead, and the agent will
        # sudo to that user for us.
        become_user = self._connection.get_become_user()
        if become_user is not None and (creates or removes):
            return self._run_builtin_command(result, task_vars)

        # Ensure the connection is established before accessing _agent_client.
        self._connection._connect()

        # The action override bypasses Ansible's usual module pipeline,
        # which is also what would otherwise wire the task's
        # `environment:` block into the executed command. Compute the
        # env dict ourselves and pass it through the RPC.
        task_env = self._compute_environment_dict()

        try:
            exec_result = self._connection._agent_client.exec(
                argv=exec_argv,
                cmd_string=exec_cmd_string,
                use_shell=uses_shell,
                cwd=chdir,
                env=task_env or None,
                stdin=stdin,
                stdin_add_newline=stdin_add_newline,
                strip_empty_ends=strip_empty_ends,
                creates=creates,
                removes=removes,
                become_user=become_user,
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
        r["changed"] = exec_result.get("changed", r["changed"])
        r["msg"] = exec_result.get("msg", r["msg"])
        if exec_result.get("skipped"):
            r["skipped"] = True
        r["start"] = to_text(start)
        r["end"] = to_text(end)
        r["delta"] = to_text(end - start)
        r["stdout_lines"] = r["stdout"].splitlines()
        r["stderr_lines"] = r["stderr"].splitlines()

        if r["rc"] != 0:
            r["msg"] = "The command exited with a non-zero return code."
            r["failed"] = True

        return merge_hash(result, r)
