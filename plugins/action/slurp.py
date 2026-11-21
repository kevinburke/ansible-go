"""Read ordinary slurp sources through the existing ReadFile RPC.

Leave path expansion, unarmored reads and privilege changes to stock. Reads
are safe to retry through stock, which also preserves its error diagnostics.
"""

from __future__ import annotations

import re

from ansible.module_utils.common.arg_spec import ArgumentSpecValidator
from ansible.plugins.action import ActionBase
from ansible.release import __version__ as _ANSIBLE_VERSION
from ansible.utils.display import Display
from ansible.utils.vars import merge_hash

try:
    from ansible_collections.kevinburke.fastagent.plugins.module_utils.fastagent_client import (
        FastAgentError,
        ansible_applies_become,
    )
except ImportError:
    from plugins.module_utils.fastagent_client import FastAgentError, ansible_applies_become


display = Display()
BUILTIN_MODULE = "ansible.builtin.slurp"


def ansible_version():
    match = re.match(r"(\d+)\.(\d+)", _ANSIBLE_VERSION)
    return tuple(map(int, match.groups())) if match else (0, 0)


def source_path(args):
    # Do not expand type=path on the controller. Relative paths and variables
    # depend on the target module's working directory and environment.
    spec = {"src": {"type": "raw", "required": True, "aliases": ["path"]}}
    if ansible_version() >= (2, 21):
        spec["armor"] = {"type": "bool", "default": True}
    if "src" in args and "path" in args:
        return None  # Let stock emit its duplicate-alias warning.
    validated = ArgumentSpecValidator(spec).validate(dict(args))
    if (validated.error_messages or getattr(validated, "_warnings", None)
            or getattr(validated, "_deprecations", None)):
        return None
    params = validated.validated_parameters
    path = params.get("src")
    if (not isinstance(path, str) or not path.startswith("/")
            or "\x00" in path or "$" in path or not params.get("armor", True)):
        return None
    return path


class ActionModule(ActionBase):
    _supports_check_mode = True
    _supports_async = True

    def _run_builtin(self, result, task_vars, reason):
        display.vvv(f"fastagent slurp: using {BUILTIN_MODULE}: {reason}")
        result = merge_hash(result, self._execute_module(
            module_name=BUILTIN_MODULE, task_vars=task_vars,
            wrap_async=self._task.async_val))
        if not self._task.async_val:
            self._remove_tmp_path(self._connection._shell.tmpdir)
        return result

    def run(self, tmp=None, task_vars=None):
        task_vars = {} if task_vars is None else task_vars
        result = super().run(tmp, task_vars)
        if (self._connection.transport != "fastagent" or self._task.async_val
                or ansible_version() < (2, 20)
                or ansible_applies_become(self._connection)
                or self._connection.get_become_user() is not None):
            return self._run_builtin(result, task_vars, "connection or task requires stock")
        path = source_path(self._task.args)
        if path is None:
            return self._run_builtin(result, task_vars, "unsupported arguments")
        self._connection._connect()
        try:
            rpc = self._connection._agent_client.read_file(path)
            content, size = rpc["content"], rpc["size"]
            if (not isinstance(content, str) or type(size) is not int or size < 0
                    or len(content) != 4 * ((size + 2) // 3)):
                raise ValueError("invalid ReadFile response")
        except FastAgentError as exc:
            return self._run_builtin(result, task_vars, f"agent error: {exc}")
        except Exception as exc:
            display.warning(f"fastagent slurp: ReadFile failed, using {BUILTIN_MODULE}: {exc}")
            return self._run_builtin(result, task_vars, f"RPC failed: {exc}")
        result.update(changed=False, content=content, source=path, encoding="base64")
        return result
