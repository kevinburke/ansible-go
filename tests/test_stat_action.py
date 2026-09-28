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
    def __init__(self, response=None):
        self.stat_calls: list[dict] = []
        self.response = response

    def stat(self, path, follow=False, checksum=False, checksum_algorithm=None,
             builtin=False, mime=False, attributes=False, env=None):
        self.stat_calls.append(
            {
                "path": path,
                "follow": follow,
                "checksum": checksum,
                "checksum_algorithm": checksum_algorithm,
                "builtin": builtin,
                "mime": mime,
                "attributes": attributes,
                "env": env,
            }
        )
        if self.response is not None:
            return dict(self.response, path=path)
        return {
            "exists": True,
            "path": path,
            "mode": "0644",
            "isreg": True,
            "checksum": "placeholder",
        }


class _FakeConnection:
    transport = "fastagent"

    def __init__(self, become_user=None, response=None):
        self._agent_client = _RecordingAgentClient(response)
        self._become_user = become_user

    def _connect(self):
        return self

    def get_become_user(self):
        return self._become_user


class _FakeTask:
    def __init__(self, args, environment=None):
        self.args = args
        self.async_val = 0
        self.environment = environment


class _IdentityTemplar:
    def template(self, value):
        return value


def _make_action(task_args, *, become_user=None, response=None, environment=None):
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args, environment)
    action._templar = _IdentityTemplar()
    action._connection = _FakeConnection(become_user=become_user, response=response)
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
    def _run(self, action):
        with patch.object(ActionBase, "run", return_value={}):
            return action.run(task_vars={})

    def test_default_options_use_fast_path(self) -> None:
        # Stock enables get_mime and get_attributes by default; the agent
        # now runs `file` and `lsattr` itself, so plain `stat: path=...`
        # no longer falls back to the builtin module.
        action = _make_action({"path": "/tmp/example"},
                              environment=[{"PATH": "/opt/bin"}])
        result = self._run(action)

        self.assertEqual(action._execute_module_calls, [])
        self.assertFalse(result.get("failed"), msg=result)
        call = action._connection._agent_client.stat_calls[0]
        self.assertEqual(call["path"], "/tmp/example")
        self.assertTrue(call["builtin"])
        self.assertTrue(call["mime"])
        self.assertTrue(call["attributes"])
        self.assertEqual(call["env"], {"PATH": "/opt/bin"})

    def test_aliases_match_stock(self) -> None:
        action = _make_action({"dest": "/tmp/example", "mime": "no",
                               "attr": False, "checksum_algo": "md5"})
        result = self._run(action)

        self.assertEqual(action._execute_module_calls, [])
        call = action._connection._agent_client.stat_calls[0]
        self.assertEqual(call["path"], "/tmp/example")
        self.assertFalse(call["mime"])
        self.assertFalse(call["attributes"])
        self.assertEqual(call["checksum_algorithm"], "md5")
        self.assertNotIn("mimetype", result["stat"])
        self.assertNotIn("attr_flags", result["stat"])

    def test_invalid_arguments_let_stock_report_the_error(self) -> None:
        for args in ({}, {"path": "/x", "bogus": 1}, {"path": "/x", "follow": "maybe"}):
            with self.subTest(args=args):
                action = _make_action(args)
                result = self._run(action)
                self.assertTrue(result["stat"]["builtin"])
                self.assertEqual(action._connection._agent_client.stat_calls, [])

    def test_selinux_context_delegates_to_builtin(self) -> None:
        action = _make_action({"path": "/x", "get_selinux_context": True})
        result = self._run(action)
        self.assertTrue(result["stat"]["builtin"])

    def test_stat_error_uses_stock_message(self) -> None:
        action = _make_action({"path": "/x"}, response={"strerror": "Not a directory"})
        result = self._run(action)
        self.assertEqual(result, {"failed": True, "msg": "Not a directory"})

    def _stat_with(self, **response):
        base = {"exists": True, "mode": "0644", "isreg": True}
        action = _make_action({"path": "/x"}, response=dict(base, **response))
        return self._run(action)["stat"]

    def test_mime_parsing_matches_stock(self) -> None:
        cases = [
            ("/x: text/plain; charset=us-ascii\n", "text/plain", "us-ascii"),
            # stock splits on the last colon
            ("/a: b: application/json; charset=utf-8\n", "application/json", "utf-8"),
            # no charset: stock's unpack fails and both stay "unknown"
            ("/x: text/plain\n", "unknown", "unknown"),
            # charset without "=": mimetype is set before the charset fails
            ("/x: text/plain; binary\n", "text/plain", "unknown"),
            ("", "unknown", "unknown"),
        ]
        for stdout, mimetype, charset in cases:
            with self.subTest(stdout=stdout):
                st = self._stat_with(file_cmd={"rc": 0, "stdout": stdout})
                self.assertEqual((st["mimetype"], st["charset"]), (mimetype, charset))
        st = self._stat_with(file_cmd={"rc": 1, "stdout": "/x: text/plain; charset=x"})
        self.assertEqual((st["mimetype"], st["charset"]), ("unknown", "unknown"))
        st = self._stat_with()  # `file` not found on the remote
        self.assertEqual((st["mimetype"], st["charset"]), ("unknown", "unknown"))

    def test_lsattr_parsing_matches_stock(self) -> None:
        st = self._stat_with(lsattr_cmd={
            "rc": 0, "stdout": "381700746 --S-ia-------e------- /x\n"})
        self.assertEqual(st["version"], "381700746")
        self.assertEqual(st["attr_flags"], "Siae")
        self.assertEqual(st["attributes"],
                         ["synchronous", "immutable", "append", "extents"])
        # A flag stock does not name stays in attr_flags only.
        st = self._stat_with(lsattr_cmd={"rc": 0, "stdout": "1 ----F--- /x"})
        self.assertEqual((st["attr_flags"], st["attributes"]), ("F", []))
        # Only a version: stock keeps it and the defaults for the rest.
        st = self._stat_with(lsattr_cmd={"rc": 0, "stdout": "7"})
        self.assertEqual((st["version"], st["attr_flags"], st["attributes"]),
                         ("7", "", []))
        for cmd in (None, {"rc": 1, "stdout": "lsattr: Operation not supported"}):
            with self.subTest(cmd=cmd):
                st = self._stat_with(lsattr_cmd=cmd) if cmd else self._stat_with()
                self.assertEqual((st["version"], st["attr_flags"], st["attributes"]),
                                 (None, "", []))

    def test_mode_keeps_special_bits(self) -> None:
        self.assertEqual(self._stat_with(mode="04755")["mode"], "4755")
        self.assertEqual(self._stat_with(mode="07")["mode"], "0007")

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
                    "builtin": True,
                    "mime": False,
                    "attributes": False,
                    "env": {},
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
