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
    from plugins.action.apt import ActionModule  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc


class _RecordingAgentClient:
    def __init__(self):
        self.calls = []

    def call(self, name, params):
        self.calls.append((name, params))
        return {"changed": False, "cache_updated": params.get("update_cache", False)}


class _FakeConnection:
    transport = "fastagent"

    def __init__(self):
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


def _make_action(task_args, *, check_mode=False):
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args)
    action._connection = _FakeConnection()
    action._play_context = _FakePlayContext(check_mode=check_mode)
    action._supports_async = False
    action._supports_check_mode = True
    return action


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestAptActionCompatibilityPreflight(unittest.TestCase):
    def _run(self, task_args, *, check_mode=False):
        action = _make_action(task_args, check_mode=check_mode)
        fallback_result = {"changed": False, "fallback": True}
        with patch.object(ActionBase, "run", return_value={}):
            with patch.object(
                action,
                "_execute_module",
                return_value=fallback_result,
            ) as execute_module:
                result = action.run(task_vars={})
        return action, execute_module, result

    def test_purge_falls_back_before_package_rpc(self) -> None:
        action, execute_module, result = self._run(
            {"name": "curl", "state": "absent", "purge": True}
        )

        execute_module.assert_called_once_with(
            module_name="ansible.builtin.apt",
            task_vars={},
        )
        self.assertEqual(action._connection._agent_client.calls, [])
        self.assertTrue(result["fallback"])

    def test_package_specs_fall_back_before_package_rpc(self) -> None:
        action, execute_module, _ = self._run({"name": "curl=1.2.3"})

        execute_module.assert_called_once()
        self.assertEqual(action._connection._agent_client.calls, [])

    def test_check_mode_falls_back_to_builtin_apt(self) -> None:
        action, execute_module, _ = self._run({"name": "curl"}, check_mode=True)

        execute_module.assert_called_once()
        self.assertEqual(action._connection._agent_client.calls, [])

    def test_cache_valid_time_implies_update_cache(self) -> None:
        action, execute_module, _ = self._run({"cache_valid_time": 3600})

        execute_module.assert_not_called()
        self.assertEqual(
            action._connection._agent_client.calls,
            [
                (
                    "Package",
                    {
                        "manager": "apt",
                        "names": [],
                        "state": "present",
                        "update_cache": True,
                        "cache_valid_time": 3600,
                    },
                )
            ],
        )

    def test_supported_install_uses_package_rpc(self) -> None:
        action, execute_module, result = self._run({"name": ["curl", "git"]})

        execute_module.assert_not_called()
        self.assertFalse(result["changed"])
        self.assertEqual(
            action._connection._agent_client.calls,
            [
                (
                    "Package",
                    {
                        "manager": "apt",
                        "names": ["curl", "git"],
                        "state": "present",
                        "update_cache": False,
                        "cache_valid_time": 0,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
