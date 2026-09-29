"""The RPCs a template task sends on a fastagent connection.

These drive ansible-core's real Task, DataLoader, Templar and template
rendering, with only the connection faked, and `ansible.legacy.copy`
resolving to fastagent's copy action as it does when fastagent's action
plugins are on the legacy `action_plugins` path.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from ansible.errors import AnsibleActionFail
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook.play_context import PlayContext
    from ansible.playbook.task import Task
    from ansible.plugins import loader as plugin_loader
    from ansible.plugins.action import ActionBase
    from ansible.template import Templar

    from plugins.action import copy as copy_action  # type: ignore[import-untyped]
    from plugins.action import template as template_action  # type: ignore[import-untyped]
    from plugins.module_utils.builtin_action import load_builtin_action_class
    _ANSIBLE_IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself is not installed. Any other import
    # failure, such as a syntax error in the plugin, must fail the run
    # rather than silently skip every test.
    if exc.name is None or exc.name.split(".")[0] != "ansible":
        raise
    _ANSIBLE_IMPORT_ERROR = exc


class _AgentClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.write_kwargs: dict | None = None

    def write_file(self, **kwargs):
        self.calls.append(("WriteFile", kwargs["dest"]))
        self.write_kwargs = kwargs
        return {"changed": False, "dest": kwargs["dest"], "checksum": "c"}

    def stat(self, path, **_kwargs):
        self.calls.append(("Stat", path))
        return {"exists": False}


class _Connection:
    become = None

    def __init__(self, transport="fastagent"):
        self.transport = transport
        self._shell = plugin_loader.shell_loader.get("sh")
        self._agent_client = _AgentClient()
        self.become_user = None

    def _connect(self):
        return self

    def get_become_user(self):
        return self.become_user

    def is_pipelining_enabled(self, wrap_async=False):
        return False


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestTemplateRoundTrips(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        plugin_loader.init_plugin_loader()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, "app.conf.j2")
        with open(self.src, "w") as f:
            f.write("port={{ port }}\n")
        # Commands ActionBase runs through the connection's shell: the
        # tmp dir's `~` expansion, mkdir and rm.
        self.commands: list[str] = []

    def _low_level_execute_command(self, action, cmd, **_kwargs):
        self.commands.append(cmd)
        stdout = ""
        if "echo ~" in cmd:
            stdout = "/home/u\n"
        elif m := re.search(r"(ansible-tmp-[0-9.-]+)", cmd):
            if "mkdir" in cmd:
                stdout = f"{m.group(1)}=/home/u/.ansible/tmp/{m.group(1)}\n"
        return {"rc": 0, "stdout": stdout, "stderr": ""}

    def _run(self, action_class, connection):
        loader = DataLoader()
        task = Task.load({
            "action": "template",
            "args": {"src": self.src, "dest": "/etc/app.conf", "mode": "0644"},
        })
        action = action_class(
            task=task,
            connection=connection,
            play_context=PlayContext(),
            loader=loader,
            templar=Templar(loader=loader),
            shared_loader_obj=plugin_loader,
        )
        get = plugin_loader.action_loader.get

        def get_legacy_copy(name, *args, **kwargs):
            if name == "ansible.legacy.copy":
                return copy_action.ActionModule(**kwargs)
            return get(name, *args, **kwargs)

        test = self

        def execute(action_self, cmd, **kwargs):
            return test._low_level_execute_command(action_self, cmd, **kwargs)

        with patch.object(plugin_loader.action_loader, "get", get_legacy_copy), \
                patch.object(ActionBase, "_low_level_execute_command", execute):
            return action.run(task_vars={"port": 8080, "ansible_search_path": []})

    def test_unchanged_template_is_one_write_file(self) -> None:
        connection = _Connection()
        result = self._run(template_action.ActionModule, connection)

        self.assertFalse(result.get("failed"), msg=result)
        self.assertFalse(result["changed"])
        self.assertEqual(self.commands, [])
        client = connection._agent_client
        self.assertEqual(client.calls, [("WriteFile", "/etc/app.conf")])
        self.assertEqual(base64.b64decode(client.write_kwargs["content"]), b"port=8080\n")
        self.assertEqual(client.write_kwargs["mode"], "0644")
        self.assertIsNone(connection._shell.tmpdir)

    def test_stock_template_makes_unused_tmp_dir(self) -> None:
        # The baseline the override removes: stock template expands `~`,
        # makes a tmp dir and removes it, three Execs that copy's
        # WriteFile never uses. If ansible-core stops doing this, the
        # override may no longer be needed.
        connection = _Connection()
        result = self._run(load_builtin_action_class("template"), connection)

        self.assertFalse(result.get("failed"), msg=result)
        self.assertEqual(len(self.commands), 3, msg=self.commands)
        self.assertIn("echo ~", self.commands[0])
        self.assertIn("mkdir", self.commands[1])
        self.assertIn("rm -f -r", self.commands[2])
        self.assertEqual(connection._agent_client.calls, [("WriteFile", "/etc/app.conf")])

    def test_other_connections_keep_tmp_dir(self) -> None:
        action = template_action.ActionModule.__new__(template_action.ActionModule)
        action._connection = _Connection(transport="ssh")
        self.assertTrue(action._early_needs_tmp_path())
        action._connection = _Connection()
        self.assertFalse(action._early_needs_tmp_path())

    def _fallback_that_makes_tmp_dir(self, fail):
        """Stand in for copy's fallback to ansible-core's copy action,
        which makes a tmp dir, and removes it only when it succeeds."""
        tmpdir = "/home/u/.ansible/tmp/ansible-tmp-1.2-3"

        def run_builtin_copy(copy_self, _tmp, _task_vars):
            copy_self._connection._shell.tmpdir = tmpdir
            if fail:
                raise AnsibleActionFail("builtin copy failed")
            copy_self._connection._shell.tmpdir = None
            return {"changed": True}

        return tmpdir, patch.object(
            copy_action.ActionModule, "_run_builtin_copy", run_builtin_copy
        )

    def test_failed_fallback_tmp_dir_is_removed(self) -> None:
        connection = _Connection()
        connection.become_user = "app"
        tmpdir, fallback = self._fallback_that_makes_tmp_dir(fail=True)
        with fallback, self.assertRaisesRegex(AnsibleActionFail, "builtin copy failed"):
            self._run(template_action.ActionModule, connection)

        self.assertEqual(len(self.commands), 1, msg=self.commands)
        self.assertIn("rm -f -r", self.commands[0])
        self.assertIn(tmpdir, self.commands[0])
        self.assertIsNone(connection._shell.tmpdir)

    def test_successful_fallback_removes_its_own_tmp_dir(self) -> None:
        connection = _Connection()
        connection.become_user = "app"
        _, fallback = self._fallback_that_makes_tmp_dir(fail=False)
        with fallback:
            result = self._run(template_action.ActionModule, connection)

        self.assertTrue(result["changed"])
        self.assertEqual(self.commands, [])

    def test_tmp_dir_from_before_the_task_is_left_alone(self) -> None:
        connection = _Connection()
        connection._shell.tmpdir = "/home/u/.ansible/tmp/ansible-tmp-9.9-9"
        self._run(template_action.ActionModule, connection)

        self.assertEqual(self.commands, [])
        self.assertEqual(connection._shell.tmpdir, "/home/u/.ansible/tmp/ansible-tmp-9.9-9")


if __name__ == "__main__":
    unittest.main()
