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
    from plugins.action.stat import ActionModule  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc


class _RecordingAgentClient:
    def __init__(self):
        self.stat_calls: list[dict] = []

    def stat(self, path, follow=False, checksum=False, checksum_algorithm=None):
        self.stat_calls.append(
            {
                "path": path,
                "follow": follow,
                "checksum": checksum,
                "checksum_algorithm": checksum_algorithm,
            }
        )
        return {
            "exists": True,
            "path": path,
            "isreg": True,
            "checksum": "placeholder",
        }


class _FakeConnection:
    transport = "fastagent"

    def __init__(self, become_user=None):
        self._agent_client = _RecordingAgentClient()
        self._become_user = become_user

    def _connect(self):
        return self

    def get_become_user(self):
        return self._become_user


class _FakeTask:
    def __init__(self, args):
        self.args = args
        self.async_val = 0


def _make_action(task_args, *, become_user=None):
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args)
    action._connection = _FakeConnection(become_user=become_user)
    action._supports_async = False
    action._supports_check_mode = True
    action._execute_module_calls = []

    def _execute_module(**kwargs):
        action._execute_module_calls.append(kwargs)
        return {"changed": False, "stat": {"exists": False, "builtin": True}}

    action._execute_module = _execute_module
    return action


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestStatActionCompatibility(unittest.TestCase):
    def test_default_mime_and_attributes_delegate_to_builtin(self) -> None:
        action = _make_action({"path": "/tmp/example"})

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result["stat"]["builtin"])
        self.assertEqual(len(action._execute_module_calls), 1)
        self.assertEqual(
            action._execute_module_calls[0]["module_name"],
            "ansible.builtin.stat",
        )
        self.assertEqual(action._connection._agent_client.stat_calls, [])

    def test_opted_out_default_checksum_uses_stock_sha1(self) -> None:
        action = _make_action(
            {
                "path": "/tmp/example",
                "get_mime": False,
                "get_attributes": False,
            }
        )

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertFalse(result.get("failed"), msg=result)
        self.assertEqual(action._execute_module_calls, [])
        self.assertEqual(
            action._connection._agent_client.stat_calls,
            [
                {
                    "path": "/tmp/example",
                    "follow": False,
                    "checksum": True,
                    "checksum_algorithm": "sha1",
                }
            ],
        )

    def test_supported_explicit_checksum_algorithm_uses_fast_path(self) -> None:
        action = _make_action(
            {
                "path": "/tmp/example",
                "get_mime": False,
                "get_attributes": False,
                "checksum_algorithm": "sha512",
            }
        )

        with patch.object(ActionBase, "run", return_value={}):
            action.run(task_vars={})

        self.assertEqual(
            action._connection._agent_client.stat_calls[0]["checksum_algorithm"],
            "sha512",
        )

    def test_unsupported_checksum_algorithm_delegates_to_builtin(self) -> None:
        action = _make_action(
            {
                "path": "/tmp/example",
                "get_mime": False,
                "get_attributes": False,
                "checksum_algorithm": "blake2",
            }
        )

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result["stat"]["builtin"])
        self.assertEqual(action._connection._agent_client.stat_calls, [])

    def test_become_delegates_to_builtin_before_fast_path(self) -> None:
        action = _make_action(
            {
                "path": "/tmp/example",
                "get_mime": False,
                "get_attributes": False,
            },
            become_user="nobody",
        )

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result["stat"]["builtin"])
        self.assertEqual(action._connection._agent_client.stat_calls, [])


if __name__ == "__main__":
    unittest.main()
