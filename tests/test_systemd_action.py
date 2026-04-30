"""Regression coverage for systemd fast-path routing decisions."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from ansible.plugins.action import ActionBase  # type: ignore[import-untyped]
    from plugins.action.systemd import ActionModule  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc


class _RecordingAgentClient:
    def __init__(self):
        self.exec_calls = []
        self.service_kwargs = None

    def exec(self, **kwargs):
        self.exec_calls.append(kwargs)
        return {"rc": 0, "stdout": "", "stderr": ""}

    def service(self, **kwargs):
        self.service_kwargs = kwargs
        return {"changed": True, "state": "active", "enabled": True}


class _FakeConnection:
    def __init__(self, transport="fastagent"):
        self.transport = transport
        self._agent_client = _RecordingAgentClient()

    def _connect(self):
        return self


class _FakeTask:
    def __init__(self, args):
        self.args = args
        self.async_val = 0


class _FakePlayContext:
    def __init__(self, check_mode=False):
        self.check_mode = check_mode


def _make_action(args, *, connection=None, check_mode=False):
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(args)
    action._connection = connection or _FakeConnection()
    action._play_context = _FakePlayContext(check_mode=check_mode)
    return action


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestSystemdActionFallbacks(unittest.TestCase):
    def _run(self, args, *, connection=None):
        action = _make_action(args, connection=connection)
        with (
            patch.object(ActionBase, "run", return_value={}),
            patch.object(ActionModule, "_execute_module", return_value={"changed": False}) as execute_module,
        ):
            result = action.run(task_vars={})
        return result, execute_module, action

    def test_non_fastagent_uses_builtin_systemd(self):
        _, execute_module, _ = self._run(
            {"name": "cron", "state": "started"},
            connection=_FakeConnection(transport="ssh"),
        )
        execute_module.assert_called_once()
        self.assertEqual(
            execute_module.call_args.kwargs["module_name"],
            "ansible.builtin.systemd",
        )

    def test_masked_falls_back_to_builtin_systemd(self):
        _, execute_module, _ = self._run({"name": "cron", "masked": True})
        execute_module.assert_called_once()
        self.assertEqual(
            execute_module.call_args.kwargs["module_name"],
            "ansible.builtin.systemd",
        )

    def test_non_system_scope_falls_back_to_builtin_systemd(self):
        _, execute_module, _ = self._run({"name": "timer", "scope": "user", "state": "started"})
        execute_module.assert_called_once()
        self.assertEqual(
            execute_module.call_args.kwargs["module_name"],
            "ansible.builtin.systemd",
        )

    def test_daemon_reexec_falls_back_to_builtin_systemd(self):
        _, execute_module, _ = self._run({"daemon_reexec": True})
        execute_module.assert_called_once()
        self.assertEqual(
            execute_module.call_args.kwargs["module_name"],
            "ansible.builtin.systemd",
        )


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestSystemdActionRPC(unittest.TestCase):
    def test_daemon_reload_does_not_mark_changed_by_itself(self):
        action = _make_action({"daemon_reload": True})
        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertFalse(result["changed"])
        self.assertEqual(
            action._connection._agent_client.exec_calls,
            [{"argv": ["systemctl", "daemon-reload"]}],
        )

    def test_no_block_is_sent_to_service_rpc(self):
        action = _make_action({"name": "cron", "state": "restarted", "no_block": True})
        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result["changed"])
        self.assertEqual(
            action._connection._agent_client.service_kwargs,
            {
                "name": "cron",
                "manager": "systemd",
                "state": "restarted",
                "enabled": None,
                "no_block": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
