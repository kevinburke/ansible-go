"""Integration tests for FastAgentClient.

Builds the Go agent binary, launches it as a subprocess, and round-trips
actual RPCs through the client. Requires Go to be installed.
"""

import base64
import io
import json
import os
import subprocess
import tempfile
import unittest

from fastagent_client import (
    FastAgentClient,
    FastAgentError,
    FastAgentVersionMismatch,
)


class TestRPCFailures(unittest.TestCase):
    def test_invalid_response_poisoned_stream_is_not_used_again(self):
        responses = [b"", b"not json\n"] + [
            (json.dumps(value) + "\n").encode() for value in (
                [], {"id": True, "result": {}}, {"id": 2, "result": {}},
                {"id": 1}, {"id": 1, "result": []},
                {"id": 1, "error": "broken"},
                {"id": 1, "error": {"code": "bad", "message": "bad"}},
            )
        ]
        for response in responses:
            with self.subTest(response=response):
                output = io.BytesIO()
                client = FastAgentClient(output, io.BytesIO(response))
                with self.assertRaisesRegex(OSError, "outcome unknown"):
                    client.call("Exec", {"argv": ["a-mutation"]})
                sent = output.getvalue()
                with self.assertRaisesRegex(OSError, "unusable"):
                    client.call("Exec", {"argv": ["a-mutation"]})
                self.assertEqual(output.getvalue(), sent)

    def test_agent_error_does_not_poison_stream(self):
        responses = b'{"id":1,"error":{"code":7,"message":"failed"}}\n{"id":2,"result":{"ok":true}}\n'
        client = FastAgentClient(io.BytesIO(), io.BytesIO(responses))
        with self.assertRaises(FastAgentError):
            client.call("Exec")
        self.assertEqual(client.call("Stat"), {"ok": True})

    def test_serialization_failure_does_not_poison_stream(self):
        output = io.BytesIO()
        client = FastAgentClient(output, io.BytesIO(b'{"id":2,"result":{}}\n'))
        with self.assertRaises(TypeError):
            client.call("Exec", {"bad": object()})
        self.assertEqual(output.getvalue(), b"")
        self.assertEqual(client.call("Stat"), {})


# Build once for all tests.
_agent_binary = None
_agent_tmp_dir = None


def _get_agent_binary():
    global _agent_binary, _agent_tmp_dir
    if _agent_binary is None:
        _agent_tmp_dir = tempfile.mkdtemp(prefix="fastagent-test-")
        _agent_binary = os.path.join(_agent_tmp_dir, "fastagent-test")
        # __file__ is plugins/module_utils/fastagent_client_test.py — go up
        # three levels to reach the repo root where go.mod lives.
        repo_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        subprocess.run(
            ["go", "build", "-trimpath", "-o", _agent_binary, "./cmd/fastagent"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
    return _agent_binary


class AgentSession:
    """Context manager that starts the agent and provides a client."""

    def __enter__(self):
        binary = _get_agent_binary()
        self.proc = subprocess.Popen(
            [binary, "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.client = FastAgentClient(self.proc.stdin, self.proc.stdout)
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        try:
            self.proc.stderr.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        return False


def _get_agent_version():
    """Read the agent version by running the binary with --version."""
    result = subprocess.run(
        [_get_agent_binary(), "--version"],
        capture_output=True,
        check=True,
        text=True,
    )
    # Output is "fastagent <version>\n".
    return result.stdout.strip().split()[-1]


class TestHello(unittest.TestCase):
    def test_hello(self):
        version = _get_agent_version()
        with AgentSession() as client:
            result = client.hello(version)
            self.assertEqual(result["version"], version)
            self.assertIn("capabilities", result)
            self.assertIsInstance(result["capabilities"], list)
            self.assertGreater(len(result["capabilities"]), 0)

    def test_hello_version_mismatch(self):
        with AgentSession() as client:
            with self.assertRaises(FastAgentVersionMismatch) as cm:
                client.hello("0.0.0-not-a-real-version")
            self.assertEqual(cm.exception.expected, "0.0.0-not-a-real-version")
            self.assertEqual(cm.exception.actual, _get_agent_version())


class TestExec(unittest.TestCase):
    def test_completed_mutation_with_lost_response_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory, AgentSession() as client:
            path = os.path.join(directory, "counter")
            original = client._stdout

            class LostResponse:
                def readline(self):
                    original.readline()  # The daemon completed the command.
                    raise OSError("injected response loss")

            client._stdout = LostResponse()
            command = ["sh", "-c", 'printf x >> "$1"', "sh", path]
            with self.assertRaisesRegex(OSError, "outcome unknown"):
                client.exec(argv=command)
            with self.assertRaisesRegex(OSError, "unusable"):
                client.exec(argv=command)
            with open(path) as result:
                self.assertEqual(result.read(), "x")

    def test_echo(self):
        with AgentSession() as client:
            result = client.exec(argv=["echo", "hello world"])
            self.assertEqual(result["rc"], 0)
            self.assertEqual(result["stdout"], "hello world")
            self.assertTrue(result["changed"])

    def test_shell(self):
        with AgentSession() as client:
            result = client.exec(cmd_string="echo $((2 + 3))", use_shell=True)
            self.assertEqual(result["rc"], 0)
            self.assertEqual(result["stdout"], "5")

    def test_nonzero_exit(self):
        with AgentSession() as client:
            result = client.exec(cmd_string="exit 42", use_shell=True)
            self.assertEqual(result["rc"], 42)

    def test_creates_skips(self):
        with AgentSession() as client:
            result = client.exec(
                argv=["echo", "should not run"],
                creates="/dev/null",
            )
            self.assertTrue(result.get("skipped"))

    def test_removes_skips(self):
        with AgentSession() as client:
            result = client.exec(
                argv=["echo", "should not run"],
                removes="/nonexistent-path-fastagent-test",
            )
            self.assertTrue(result.get("skipped"))

    def test_cwd(self):
        with AgentSession() as client:
            result = client.exec(cmd_string="pwd", use_shell=True, cwd="/tmp")
            self.assertEqual(result["rc"], 0)
            # macOS: /tmp is a symlink to /private/tmp
            self.assertIn("tmp", result["stdout"])

    def test_stdin(self):
        with AgentSession() as client:
            result = client.exec(
                cmd_string="cat",
                use_shell=True,
                stdin="hello from stdin",
            )
            self.assertEqual(result["rc"], 0)
            self.assertIn("hello from stdin", result["stdout"])


class TestStat(unittest.TestCase):
    def test_existing_file(self):
        with AgentSession() as client:
            with tempfile.NamedTemporaryFile(suffix=".txt") as f:
                f.write(b"test content")
                f.flush()
                result = client.stat(f.name, checksum=True)
                self.assertTrue(result["exists"])
                self.assertFalse(result.get("isdir", False))
                self.assertEqual(result["size"], 12)
                self.assertIn("checksum", result)
                self.assertNotEqual(result["checksum"], "")

    def test_nonexistent(self):
        with AgentSession() as client:
            result = client.stat("/nonexistent-path-fastagent-test")
            self.assertFalse(result["exists"])

    def test_directory(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                result = client.stat(d)
                self.assertTrue(result["exists"])
                self.assertTrue(result["isdir"])


class TestWriteAndReadFile(unittest.TestCase):
    def test_write_new_file(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                dest = os.path.join(d, "output.txt")
                content = b"new file content"
                b64 = base64.b64encode(content).decode("ascii")

                result = client.write_file(dest=dest, content=b64)
                self.assertTrue(result["changed"])
                self.assertEqual(result["dest"], dest)

                with open(dest, "rb") as f:
                    self.assertEqual(f.read(), content)

    def test_write_idempotent(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                dest = os.path.join(d, "output.txt")
                content = b"idempotent content"
                b64 = base64.b64encode(content).decode("ascii")

                result1 = client.write_file(dest=dest, content=b64)
                self.assertTrue(result1["changed"])

                result2 = client.write_file(dest=dest, content=b64)
                self.assertFalse(result2["changed"])

    def test_write_then_read(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                dest = os.path.join(d, "roundtrip.txt")
                content = b"round trip data\nwith newlines\n"
                b64 = base64.b64encode(content).decode("ascii")

                client.write_file(dest=dest, content=b64)

                result = client.read_file(dest)
                decoded = base64.b64decode(result["content"])
                self.assertEqual(decoded, content)
                self.assertEqual(result["size"], len(content))

    def test_write_with_backup(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                dest = os.path.join(d, "backup.txt")
                os.write(
                    os.open(dest, os.O_CREAT | os.O_WRONLY, 0o644),
                    b"original",
                )

                new_content = base64.b64encode(b"updated").decode("ascii")
                result = client.write_file(
                    dest=dest, content=new_content, backup=True
                )
                self.assertTrue(result["changed"])
                self.assertIn("backup_file", result)
                self.assertNotEqual(result["backup_file"], "")

                with open(result["backup_file"], "rb") as f:
                    self.assertEqual(f.read(), b"original")


class TestFile(unittest.TestCase):
    def test_create_directory(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                new_dir = os.path.join(d, "sub", "nested")
                result = client.file(path=new_dir, state="directory")
                self.assertTrue(result["changed"])
                self.assertTrue(os.path.isdir(new_dir))

    def test_touch(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "touched.txt")
                result = client.file(path=path, state="touch")
                self.assertTrue(result["changed"])
                self.assertTrue(os.path.exists(path))

    def test_absent(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "to-remove.txt")
                with open(path, "w") as f:
                    f.write("delete me")

                result = client.file(path=path, state="absent")
                self.assertTrue(result["changed"])
                self.assertFalse(os.path.exists(path))

    def test_absent_nonexistent(self):
        with AgentSession() as client:
            result = client.file(
                path="/nonexistent-path-fastagent-test",
                state="absent",
            )
            self.assertFalse(result["changed"])

    def test_symlink(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "source.txt")
                with open(src, "w") as f:
                    f.write("source")
                link = os.path.join(d, "link.txt")

                result = client.file(path=link, state="link", src=src)
                self.assertTrue(result["changed"])
                self.assertEqual(os.readlink(link), src)

    def test_symlink_idempotent(self):
        with AgentSession() as client:
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "source.txt")
                with open(src, "w") as f:
                    f.write("source")
                link = os.path.join(d, "link.txt")

                client.file(path=link, state="link", src=src)
                result = client.file(path=link, state="link", src=src)
                self.assertFalse(result["changed"])


class TestErrorHandling(unittest.TestCase):
    def test_unknown_method(self):
        with AgentSession() as client:
            with self.assertRaises(FastAgentError) as ctx:
                client.call("Bogus", {})
            self.assertIn("unknown method", ctx.exception.message)

    def test_read_nonexistent_file(self):
        with AgentSession() as client:
            with self.assertRaises(FastAgentError):
                client.read_file("/nonexistent-path-fastagent-test")

    def test_exec_no_command(self):
        with AgentSession() as client:
            with self.assertRaises(FastAgentError):
                client.exec()


if __name__ == "__main__":
    unittest.main()
