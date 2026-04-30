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


class _IdentityTemplar:
    """Stand-in for ansible's templar that returns the input unchanged.

    The action plugin only calls ``self._templar.template(value)`` on
    each entry of ``self._task.environment``. Tests pass already-
    materialized dicts, so identity templating is sufficient.
    """

    def template(self, value):
        return value


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
        self.become = None
        self.has_native_async = False

    def _connect(self):
        return self

    def get_become_user(self):
        return self._become_user


class _FakeTask:
    def __init__(self, args, environment=None):
        self.args = args
        self.async_val = 0
        self.environment = environment


class _FakePlayContext:
    check_mode = False


def _make_action(connection, *, task_args, environment=None):
    # Instantiating the real ActionModule drags in ActionBase.__init__,
    # which expects loader/templar/shared_loader. Constructing a bare
    # object and injecting just the attributes command.ActionModule.run
    # touches keeps the test focused on the become-user wiring.
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args, environment=environment)
    action._connection = connection
    action._play_context = _FakePlayContext()
    action._templar = _IdentityTemplar()
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

    def test_creates_and_removes_are_passed_to_agent(self) -> None:
        conn = _FakeConnection(become_user=None)
        action = _make_action(
            conn,
            task_args={
                "_raw_params": "touch marker",
                "_uses_shell": False,
                "creates": "marker",
                "removes": "input.*",
            },
        )
        with patch.object(ActionBase, "run", return_value={}):
            action.run(task_vars={})
        self.assertEqual(conn._agent_client.last_kwargs["creates"], "marker")
        self.assertEqual(conn._agent_client.last_kwargs["removes"], "input.*")

    def test_unsupported_become_method_falls_back_to_builtin(self) -> None:
        conn = _FakeConnection(become_user=None)
        conn.become = object()
        action = _make_action(
            conn,
            task_args={
                "_raw_params": "id -u",
                "_uses_shell": False,
            },
        )
        with patch.object(ActionBase, "run", return_value={}), patch.object(
            ActionModule,
            "_execute_module",
            return_value={"rc": 0, "builtin": True},
        ) as execute_module:
            result = action.run(task_vars={})
        execute_module.assert_called_once()
        self.assertEqual(
            execute_module.call_args.kwargs["module_name"],
            "ansible.builtin.command",
        )
        self.assertTrue(result["builtin"])
        self.assertIsNone(conn._agent_client.last_kwargs)

    def test_target_side_variable_expansion_falls_back(self) -> None:
        conn = _FakeConnection(become_user=None)
        action = _make_action(
            conn,
            task_args={
                "_raw_params": "printf %s $HOME",
                "_uses_shell": False,
            },
        )
        with patch.object(ActionBase, "run", return_value={}), patch.object(
            ActionModule,
            "_execute_module",
            return_value={"rc": 0, "builtin": True},
        ) as execute_module:
            result = action.run(task_vars={})
        execute_module.assert_called_once()
        self.assertTrue(result["builtin"])
        self.assertIsNone(conn._agent_client.last_kwargs)

    def test_disabling_argument_expansion_keeps_fast_path(self) -> None:
        conn = _FakeConnection(become_user=None)
        action = _make_action(
            conn,
            task_args={
                "_raw_params": "printf %s $HOME",
                "_uses_shell": False,
                "expand_argument_vars": False,
            },
        )
        with patch.object(ActionBase, "run", return_value={}):
            action.run(task_vars={})
        self.assertEqual(
            conn._agent_client.last_kwargs["argv"],
            ["printf", "%s", "$HOME"],
        )

    def test_creates_with_become_falls_back_to_builtin(self) -> None:
        conn = _FakeConnection(become_user="app")
        action = _make_action(
            conn,
            task_args={
                "_raw_params": "touch marker",
                "_uses_shell": False,
                "creates": "marker",
            },
        )
        with patch.object(ActionBase, "run", return_value={}), patch.object(
            ActionModule,
            "_execute_module",
            return_value={"rc": 0, "builtin": True},
        ) as execute_module:
            result = action.run(task_vars={})
        execute_module.assert_called_once()
        self.assertTrue(result["builtin"])
        self.assertIsNone(conn._agent_client.last_kwargs)


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestCommandActionTaskEnvironment(unittest.TestCase):
    """The action plugin bypasses Ansible's module pipeline, so it has
    to template ``self._task.environment`` and pass it into the Exec
    RPC explicitly. Earlier versions dropped the env block on the
    floor, which silently broke tasks like
    ``go install`` that rely on ``GO111MODULE`` / ``GOBIN``.
    """

    def _run(self, environment):
        conn = _FakeConnection(become_user=None)
        action = _make_action(
            conn,
            task_args={"_raw_params": "true", "_uses_shell": False},
            environment=environment,
        )
        with patch.object(ActionBase, "run", return_value={}):
            action.run(task_vars={})
        return conn._agent_client.last_kwargs

    def test_dict_environment_propagates(self) -> None:
        kwargs = self._run({"GO111MODULE": "on", "GOBIN": "/opt/burkebot/bin"})
        self.assertEqual(
            kwargs["env"],
            {"GO111MODULE": "on", "GOBIN": "/opt/burkebot/bin"},
        )

    def test_list_environment_merges_in_order(self) -> None:
        kwargs = self._run([
            {"GO111MODULE": "on", "GOBIN": "/old"},
            {"GOBIN": "/new", "PATH": "/usr/local/bin"},
        ])
        self.assertEqual(
            kwargs["env"],
            {"GO111MODULE": "on", "GOBIN": "/new", "PATH": "/usr/local/bin"},
        )

    def test_empty_entries_are_skipped(self) -> None:
        kwargs = self._run([None, {}, {"FOO": "bar"}])
        self.assertEqual(kwargs["env"], {"FOO": "bar"})

    def test_no_environment_passes_none(self) -> None:
        # Distinct from {}: omitting env entirely lets the agent
        # inherit the daemon's env, which matches the historical
        # behavior. Sending an empty dict would be equivalent here, but
        # None keeps the wire payload minimal.
        kwargs = self._run(None)
        self.assertIsNone(kwargs.get("env"))

    def test_non_dict_environment_raises(self) -> None:
        from ansible.errors import AnsibleError
        conn = _FakeConnection(become_user=None)
        action = _make_action(
            conn,
            task_args={"_raw_params": "true", "_uses_shell": False},
            environment=["not-a-dict"],
        )
        with patch.object(ActionBase, "run", return_value={}):
            with self.assertRaises(AnsibleError):
                action.run(task_vars={})


if __name__ == "__main__":
    unittest.main()
