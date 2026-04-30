import unittest
from unittest.mock import patch

try:
    from ansible.plugins.action import ActionBase  # type: ignore[import-untyped]
    from plugins.action.file import ActionModule  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc

from plugins.module_utils.file_state import (
    format_octal_mode,
    infer_file_state,
    requires_builtin_file,
)


class FakeClient:
    def __init__(self, stat_result):
        self.stat_result = stat_result
        self.calls = []

    def stat(self, path, follow, checksum):
        self.calls.append(
            {
                "path": path,
                "follow": follow,
                "checksum": checksum,
            }
        )
        return self.stat_result


class TestInferFileState(unittest.TestCase):
    def test_explicit_state_bypasses_stat(self):
        client = FakeClient({"exists": True, "isdir": True})

        state = infer_file_state(
            client,
            path="/tmp/existing",
            state="directory",
            src=None,
            recurse=False,
            follow=True,
        )

        self.assertEqual(state, "directory")
        self.assertEqual(client.calls, [])

    def test_src_implies_link(self):
        client = FakeClient({"exists": True, "isdir": True})

        state = infer_file_state(
            client,
            path="/tmp/link",
            state=None,
            src="/tmp/src",
            recurse=False,
            follow=True,
        )

        self.assertEqual(state, "link")
        self.assertEqual(client.calls, [])

    def test_recurse_implies_directory(self):
        client = FakeClient({"exists": False, "isdir": False})

        state = infer_file_state(
            client,
            path="/tmp/missing",
            state=None,
            src=None,
            recurse=True,
            follow=True,
        )

        self.assertEqual(state, "directory")
        self.assertEqual(client.calls, [])

    def test_existing_directory_defaults_to_directory(self):
        client = FakeClient({"exists": True, "isdir": True})

        state = infer_file_state(
            client,
            path="/tmp/existing-dir",
            state=None,
            src=None,
            recurse=False,
            follow=True,
        )

        self.assertEqual(state, "directory")
        self.assertEqual(
            client.calls,
            [{"path": "/tmp/existing-dir", "follow": True, "checksum": False}],
        )

    def test_missing_path_defaults_to_file(self):
        client = FakeClient({"exists": False, "isdir": False})

        state = infer_file_state(
            client,
            path="/tmp/missing",
            state=None,
            src=None,
            recurse=False,
            follow=False,
        )

        self.assertEqual(state, "file")
        self.assertEqual(
            client.calls,
            [{"path": "/tmp/missing", "follow": False, "checksum": False}],
        )


class TestFileFastPathSupport(unittest.TestCase):
    def test_symbolic_mode_requires_builtin_file(self):
        self.assertTrue(requires_builtin_file({"mode": "u=rw,g=r,o="}))
        self.assertEqual(format_octal_mode("0640"), "0640")
        self.assertEqual(format_octal_mode(0o640), "0640")

    def test_time_controls_require_builtin_file(self):
        self.assertTrue(requires_builtin_file({"modification_time": "preserve"}))
        self.assertTrue(requires_builtin_file({"access_time": "now"}))
        self.assertTrue(
            requires_builtin_file({"modification_time_format": "%Y-%m-%d"})
        )

    def test_link_semantics_require_builtin_file(self):
        self.assertTrue(requires_builtin_file({"state": "link", "src": "target"}))
        self.assertTrue(requires_builtin_file({"state": "hard", "src": "target"}))
        self.assertTrue(requires_builtin_file({"state": None, "src": "target"}))

    def test_follow_false_requires_builtin_except_absent(self):
        self.assertTrue(requires_builtin_file({"state": "file", "follow": False}))
        self.assertFalse(requires_builtin_file({"state": "absent", "follow": False}))


class _RecordingAgentClient:
    def __init__(self, stat_result):
        self.stat_result = stat_result
        self.file_calls = []
        self.stat_calls = []

    def stat(self, path, follow=True, checksum=True):
        self.stat_calls.append(
            {"path": path, "follow": follow, "checksum": checksum}
        )
        return self.stat_result

    def file(self, **kwargs):
        self.file_calls.append(kwargs)
        return {"changed": False}


class _FakeConnection:
    transport = "fastagent"

    def __init__(self, client):
        self._agent_client = client
        self.connects = 0

    def _connect(self):
        self.connects += 1
        return self

    def get_become_user(self):
        return None


class _FakeTask:
    def __init__(self, args):
        self.args = args
        self.async_val = 0


class _FakePlayContext:
    check_mode = False
    diff = False


def _make_action(task_args, stat_result):
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args)
    action._connection = _FakeConnection(_RecordingAgentClient(stat_result))
    action._play_context = _FakePlayContext()
    action._supports_async = False
    action._supports_check_mode = True
    return action


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestFileAction(unittest.TestCase):
    def test_unsupported_symbolic_mode_falls_back_before_connect(self):
        action = _make_action(
            {"path": "/tmp/file", "state": "touch", "mode": "u=rw,g=r,o="},
            {"exists": False},
        )
        fallback_calls = []

        def _execute_module(**kwargs):
            fallback_calls.append(kwargs)
            return {"changed": True, "fallback": True}

        action._execute_module = _execute_module

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={"inventory_hostname": "h"})

        self.assertEqual(
            fallback_calls,
            [
                {
                    "module_name": "ansible.builtin.file",
                    "task_vars": {"inventory_hostname": "h"},
                }
            ],
        )
        self.assertEqual(result.get("fallback"), True)
        self.assertEqual(action._connection.connects, 0)
        self.assertEqual(action._connection._agent_client.file_calls, [])

    def test_state_file_copies_uid_gid_from_stat_result(self):
        action = _make_action(
            {"path": "/tmp/file", "state": "file"},
            {
                "exists": True,
                "isdir": False,
                "uid": 501,
                "gid": 20,
                "owner": "kevin",
                "group": "staff",
                "mode": "0644",
                "size": 12,
            },
        )
        action._execute_module = lambda **kwargs: self.fail(
            f"unexpected fallback: {kwargs}"
        )

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertFalse(result.get("failed"), msg=result)
        self.assertEqual(result.get("uid"), 501)
        self.assertEqual(result.get("gid"), 20)
        self.assertEqual(result.get("owner"), "kevin")
        self.assertEqual(result.get("group"), "staff")


if __name__ == "__main__":
    unittest.main()
