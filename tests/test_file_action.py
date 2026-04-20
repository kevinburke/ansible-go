import unittest

from plugins.module_utils.file_state import infer_file_state


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


if __name__ == "__main__":
    unittest.main()
