"""Regression coverage for the command action override's become wiring.

The command action override bypasses Ansible's usual module pipeline
and calls the agent's Exec RPC directly, so it can't piggyback on
exec_command's become_user resolution. A previous iteration read a
removed private attribute (`self._connection._become_user`) via
`getattr(..., None)`, which silently returned None after 0.5.8 removed
the attribute — tasks using `become_user: "{{ app_user }}"` then ran
as root on the remote and hit `dubious ownership` on app-user-owned
git repos. These tests lock the action plugin to the shared
`connection.get_become_user()` helper.
"""

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
    from plugins.action.command import ActionModule  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc


class _RecordingAgentClient:
    def __init__(self):
        self.last_kwargs: dict | None = None

    def exec(self, **kwargs):
        self.last_kwargs = kwargs
        return {"rc": 0, "stdout": "", "stderr": ""}


class _FakeConnection:
    """Minimal duck-typed stand-in for the fastagent Connection.

    The action plugin only touches `.transport`, `._connect`,
    `._agent_client`, and `.get_become_user()`, so mimicking the full
    plugin is unnecessary (and would pull ansible-core's connection
    base class into the test).
    """

    transport = "fastagent"

    def __init__(self, become_user):
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


class _FakePlayContext:
    check_mode = False


def _make_action(connection, *, task_args):
    # Instantiating the real ActionModule drags in ActionBase.__init__,
    # which expects loader/templar/shared_loader. Constructing a bare
    # object and injecting just the attributes command.ActionModule.run
    # touches keeps the test focused on the become-user wiring.
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args)
    action._connection = connection
    action._play_context = _FakePlayContext()
    action._supports_async = False
    action._supports_check_mode = True
    return action


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestCommandActionBecomeUser(unittest.TestCase):
    def _run_with_mocked_base(self, conn):
        action = _make_action(
            conn,
            task_args={
                "_raw_params": "git status",
                "_uses_shell": False,
            },
        )
        # ActionBase.run wires up tmpdir, connection options, etc. —
        # unnecessary for exercising the RPC kwargs.
        with patch.object(ActionBase, "run", return_value={}):
            return action.run(task_vars={})

    def test_become_user_passed_through_to_agent(self) -> None:
        conn = _FakeConnection(become_user="returns")
        result = self._run_with_mocked_base(conn)
        self.assertEqual(conn._agent_client.last_kwargs["become_user"], "returns")
        self.assertEqual(result["rc"], 0)

    def test_no_become_sends_none(self) -> None:
        conn = _FakeConnection(become_user=None)
        self._run_with_mocked_base(conn)
        self.assertIsNone(conn._agent_client.last_kwargs["become_user"])


if __name__ == "__main__":
    unittest.main()
