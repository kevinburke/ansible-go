"""Regression tests for the fastagent connection plugin.

Targets socket lifecycle behavior that is easy to regress, notably: the
2-second timeout used to probe the local forwarding socket must not
persist into later RPC reads. An earlier version shipped without clearing
it, which made any module whose exec took >2s (e.g. ufw modifying
iptables) fail with `timed out` / `cannot read from timed out object`.

The tests use os.pipe() instead of real AF_UNIX sockets so they work
inside sandboxed CI environments where the socket() syscall is blocked
by seccomp.
"""

from __future__ import annotations

import json
import os
import socket as socket_mod
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


# The plugin imports fastagent_client via its installed-collection path.
# When these tests run against the source tree (not an installed
# collection), register the local module under that import name so
# `from ansible_collections.kevinburke.fastagent...` resolves.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_UTILS_DIR = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", "module_utils"))

sys.path.insert(0, _MODULE_UTILS_DIR)
import fastagent_client  # noqa: E402

for _pkg in (
    "ansible_collections",
    "ansible_collections.kevinburke",
    "ansible_collections.kevinburke.fastagent",
    "ansible_collections.kevinburke.fastagent.plugins",
    "ansible_collections.kevinburke.fastagent.plugins.module_utils",
):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))
sys.modules[
    "ansible_collections.kevinburke.fastagent.plugins.module_utils.fastagent_client"
] = fastagent_client

sys.path.insert(0, _PLUGIN_DIR)
_FASTAGENT_IMPORT_ERROR = None
try:
    import fastagent as fastagent_plugin  # noqa: E402
except ModuleNotFoundError as e:
    if e.name == "ansible":
        _FASTAGENT_IMPORT_ERROR = e
    else:
        raise


class _MockSocket:
    """Fake socket backed by pipe file objects.

    Tracks settimeout() calls so tests can verify the probe timeout is
    cleared, without needing a real AF_UNIX socket.
    """

    def __init__(self, client_r, client_w):
        self._client_r = client_r
        self._client_w = client_w
        self._timeout = None

    def settimeout(self, timeout):
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def connect(self, path):
        pass  # pipes are already connected

    def makefile(self, mode):
        if "w" in mode:
            return self._client_w
        return self._client_r

    def close(self):
        for f in (self._client_r, self._client_w):
            try:
                f.close()
            except Exception:
                pass


class _PipeEchoServer:
    """Echo server that reads/writes JSON-RPC via pipes.

    Reads JSON-RPC lines in a loop, echoing each request id back. The first
    response (the connect-time Hello probe) is always sent immediately so
    the probe timeout doesn't fire; `delay_s` applies only to subsequent
    responses, simulating slow real work like `ufw` reloading iptables.
    Replaces _DelayedEchoServer so tests run without the AF_UNIX socket()
    syscall.
    """

    def __init__(self, delay_s, server_r, server_w):
        self._delay_s = delay_s
        self._server_r = server_r
        self._server_w = server_w
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        count = 0
        try:
            while True:
                req_line = self._server_r.readline()
                if not req_line:
                    return
                req = json.loads(req_line)
                req_id = req.get("id", 0)
                count += 1
                if count > 1:
                    time.sleep(self._delay_s)
                # Hello expects the daemon to echo the controller's version
                # back. Any other method just gets the canned {"ok": True}
                # the tests assert against.
                if req.get("method") == "Hello":
                    params = req.get("params") or {}
                    result = {"version": params.get("version", ""),
                              "capabilities": []}
                else:
                    result = {"ok": True}
                resp = json.dumps({"id": req_id, "result": result}) + "\n"
                self._server_w.write(resp.encode("utf-8"))
                self._server_w.flush()
        except Exception:
            pass

    def close(self):
        for f in (self._server_r, self._server_w):
            try:
                f.close()
            except Exception:
                pass


class _SilentServer:
    """Server that reads but never responds. Simulates a stale SSH -L
    tunnel whose remote-side connect to the dead daemon socket got
    refused: local connect succeeds, but the first read sees EOF once
    the server side closes its write end.
    """

    def __init__(self, server_r, server_w, *, close_on_read=True):
        self._server_r = server_r
        self._server_w = server_w
        self._close_on_read = close_on_read
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._server_r.readline()
            if self._close_on_read:
                self._server_w.close()
        except Exception:
            pass

    def close(self):
        for f in (self._server_r, self._server_w):
            try:
                f.close()
            except Exception:
                pass

    def close(self):
        for f in (self._server_r, self._server_w):
            try:
                f.close()
            except Exception:
                pass


def _make_pipe_pair():
    """Create two pipes and return (client_r, client_w, server_r, server_w).

    client_w -> server_r: requests from client to server
    server_w -> client_r: responses from server to client
    """
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()
    return (
        os.fdopen(resp_r, "rb"),  # client reads responses
        os.fdopen(req_w, "wb"),   # client writes requests
        os.fdopen(req_r, "rb"),   # server reads requests
        os.fdopen(resp_w, "wb"),  # server writes responses
    )


def _bare_connection() -> fastagent_plugin.Connection:
    """Build a Connection instance without invoking Ansible's __init__.

    `_try_local_socket` only touches `_socket`, `_agent_client`, and
    `_connected` on self, so a stub with those attributes is sufficient.
    """
    conn = fastagent_plugin.Connection.__new__(fastagent_plugin.Connection)
    conn._socket = None
    conn._agent_client = None
    conn._connected = False
    conn.become = None
    conn._use_become = False
    return conn


class _FakeBecomePlugin:
    """Minimal stand-in for ansible-core's BecomeBase for testing.

    `set_become_plugin` only touches `.name` and `.get_option(...)`.
    """

    def __init__(self, name: str, options: dict | None = None):
        self.name = name
        self._options = options or {}

    def get_option(self, key, default=None):
        return self._options.get(key, default)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestTryLocalSocket(unittest.TestCase):
    def test_connect_clears_probe_timeout(self) -> None:
        client_r, client_w, server_r, server_w = _make_pipe_pair()
        server = _PipeEchoServer(delay_s=0.0, server_r=server_r, server_w=server_w)
        self.addCleanup(server.close)
        mock_sock = _MockSocket(client_r, client_w)
        self.addCleanup(mock_sock.close)

        conn = _bare_connection()
        with mock.patch.object(fastagent_plugin.socket_mod, "socket", return_value=mock_sock), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True):
            self.assertTrue(conn._try_local_socket("/fake/socket.sock", "test-host"))

        # The probe timeout must not persist — blocking I/O means None.
        self.assertIsNone(conn._socket.gettimeout())
        self.assertTrue(conn._connected)

    def test_rpc_survives_delay_longer_than_probe_timeout(self) -> None:
        # The regressed bug: a response taking longer than the 2s connect
        # probe timeout raised `timed out` because the timeout was never
        # cleared. Use 2.2s so the test catches that exact regression.
        client_r, client_w, server_r, server_w = _make_pipe_pair()
        server = _PipeEchoServer(delay_s=2.2, server_r=server_r, server_w=server_w)
        self.addCleanup(server.close)
        mock_sock = _MockSocket(client_r, client_w)
        self.addCleanup(mock_sock.close)

        conn = _bare_connection()
        with mock.patch.object(fastagent_plugin.socket_mod, "socket", return_value=mock_sock), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True):
            self.assertTrue(conn._try_local_socket("/fake/socket.sock", "test-host"))
            result = conn._agent_client.call("Exec", {})
            self.assertEqual(result, {"ok": True})

    def test_missing_socket_file_returns_false(self) -> None:
        conn = _bare_connection()
        missing = os.path.join(tempfile.gettempdir(), "fastagent-nonexistent.sock")
        self.assertFalse(conn._try_local_socket(missing, "test-host"))
        self.assertFalse(conn._connected)
        self.assertIsNone(conn._socket)

    def test_stale_tunnel_probe_fails(self) -> None:
        # A stale SSH -L tunnel pointing at a dead remote socket accepts
        # local connects and even local writes, but the first read sees EOF.
        # The Hello probe must catch this and fall through to bootstrap.
        client_r, client_w, server_r, server_w = _make_pipe_pair()
        server = _SilentServer(server_r=server_r, server_w=server_w)
        self.addCleanup(server.close)
        mock_sock = _MockSocket(client_r, client_w)
        self.addCleanup(mock_sock.close)

        conn = _bare_connection()
        with mock.patch.object(fastagent_plugin.socket_mod, "socket", return_value=mock_sock), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True):
            self.assertFalse(conn._try_local_socket("/fake/socket.sock", "test-host"))
        self.assertFalse(conn._connected)
        self.assertIsNone(conn._socket)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestSetBecomePlugin(unittest.TestCase):
    """Regression tests for the `self.become` swallowing behavior.

    ActionBase._low_level_execute_command wraps module invocations
    with `sudo -u <user> sh -c …` whenever `self._connection.become`
    is truthy. The fastagent connection handles become itself via the
    Exec RPC's become_user field, so set_become_plugin must leave
    self.become as None to suppress the redundant wrap. Without this,
    non-sudoer become_users hit "<user> is not in the sudoers file"
    because the inner sudo runs as the target user.

    The actual become_user is *not* captured off the plugin here —
    see set_become_plugin for the explanation. These tests only
    verify the `_use_become` flag and `self.become` state.
    """

    def test_sudo_plugin_is_swallowed(self) -> None:
        conn = _bare_connection()
        plugin = _FakeBecomePlugin("sudo", {"become_user": "returns"})
        conn.set_become_plugin(plugin)
        self.assertIsNone(
            conn.become,
            "self.become must stay None so ActionBase doesn't wrap the command",
        )
        self.assertTrue(conn._use_become)

    def test_sudo_to_root_is_still_use_become(self) -> None:
        # Even when the target is root, _use_become must flip so the
        # connection picks the root-daemon socket path. The "skip the
        # sudo wrap" optimization for become_user=root is decided at
        # exec_command time from play_context.become_user, not here.
        conn = _bare_connection()
        plugin = _FakeBecomePlugin("sudo", {"become_user": "root"})
        conn.set_become_plugin(plugin)
        self.assertIsNone(conn.become)
        self.assertTrue(conn._use_become)

    def test_none_plugin_clears_state(self) -> None:
        conn = _bare_connection()
        conn._use_become = True
        conn.set_become_plugin(None)
        self.assertIsNone(conn.become)
        self.assertFalse(conn._use_become)

    def test_unsupported_method_falls_back_to_ansible(self) -> None:
        conn = _bare_connection()
        plugin = _FakeBecomePlugin("su", {"become_user": "returns"})
        conn.set_become_plugin(plugin)
        # Ansible's own wrap handles non-sudo methods.
        self.assertIs(conn.become, plugin)
        self.assertFalse(conn._use_become)


class _RecordingAgentClient:
    """Captures the kwargs passed to exec() for assertion in tests."""

    def __init__(self):
        self.last_kwargs: dict | None = None

    def exec(self, **kwargs):
        self.last_kwargs = kwargs
        return {"rc": 0, "stdout": "", "stderr": ""}


class _FakePlayContext:
    def __init__(self, become_user: str | None):
        self.become_user = become_user


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestExecCommandBecomeUser(unittest.TestCase):
    """Regression tests for where become_user is sourced at exec time.

    A previous iteration read it off `plugin.get_option("become_user")`
    during set_become_plugin, which returned the default "root" because
    ansible only populates plugin options when `connection.become is
    not None` — and we deliberately keep it None to suppress the
    ActionBase wrap. The resulting command ran as root on the remote,
    which git rejected with `dubious ownership` on app-user-owned
    repos. exec_command must instead read the templated value from
    `self._play_context.become_user`.
    """

    def _prepare(self, become_user, *, use_become=True):
        conn = _bare_connection()
        conn._use_become = use_become
        conn._play_context = _FakePlayContext(become_user)
        client = _RecordingAgentClient()
        conn._agent_client = client
        # super().exec_command() checks self._connected.
        conn._connected = True
        # The connection base class's exec_command reads _play_context
        # for logging; give it a get_option that returns a sensible
        # remote_user for the sudoable=False branch.
        conn.get_option = lambda key, *a, **kw: "kevin" if key == "remote_user" else None
        return conn, client

    def test_templated_become_user_reaches_agent(self) -> None:
        conn, client = self._prepare("returns")
        conn.exec_command("echo hi")
        self.assertEqual(client.last_kwargs["become_user"], "returns")

    def test_root_become_user_skips_wrap(self) -> None:
        # Daemon already runs as root; no point sudo-wrapping to root.
        conn, client = self._prepare("root")
        conn.exec_command("echo hi")
        self.assertIsNone(client.last_kwargs["become_user"])

    def test_not_using_become_sends_none(self) -> None:
        conn, client = self._prepare("returns", use_become=False)
        conn.exec_command("echo hi")
        self.assertIsNone(client.last_kwargs["become_user"])

    def test_sudoable_false_drops_to_remote_user(self) -> None:
        # Connection plumbing (e.g. mkdir ~/.ansible/tmp) runs as the
        # ssh user so files don't end up root-owned.
        conn, client = self._prepare("returns")
        conn.exec_command("mkdir -p /tmp/foo", sudoable=False)
        self.assertEqual(client.last_kwargs["become_user"], "kevin")


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestGetBecomeUser(unittest.TestCase):
    """Direct coverage for the helper that action-plugin overrides call.

    The command/copy/file/stat action overrides bypass exec_command and
    hit the agent client directly, so they need the same resolution as
    exec_command but can't share its body. They funnel through
    get_become_user() instead. A previous iteration read a removed
    private attribute (`_become_user`) via getattr-with-default and
    silently returned None — commands then ran as root on the remote.
    """

    def _conn(self, become_user, *, use_become=True):
        conn = _bare_connection()
        conn._use_become = use_become
        conn._play_context = _FakePlayContext(become_user)
        return conn

    def test_returns_templated_user(self) -> None:
        self.assertEqual(self._conn("returns").get_become_user(), "returns")

    def test_root_becomes_none(self) -> None:
        # Daemon already runs as root under become; sudo -u root is a
        # needless fork per RPC.
        self.assertIsNone(self._conn("root").get_become_user())

    def test_unset_defaults_to_root_and_returns_none(self) -> None:
        # play_context.become_user can be None when the task didn't
        # specify one; ansible-core treats that as "root".
        self.assertIsNone(self._conn(None).get_become_user())

    def test_not_using_become_returns_none(self) -> None:
        self.assertIsNone(
            self._conn("returns", use_become=False).get_become_user()
        )


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestEnsureRemoteDaemon(unittest.TestCase):
    def _conn(self):
        conn = _bare_connection()
        conn.get_option = lambda key, *a, **kw: (
            "/opt/fastagent-{version}-{os}-{arch}"
            if key == "agent_path" else None
        )
        return conn

    def test_become_opens_daemon_log_inside_sudo_shell(self) -> None:
        conn = self._conn()
        commands = []

        def run_ssh(host, user, port, command):
            commands.append(command)
            if command.startswith("test -S "):
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(conn, "_run_ssh_command", side_effect=run_ssh), \
             mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
             mock.patch.object(conn, "_ensure_agent_deployed"):
            conn._ensure_remote_daemon(
                "serval",
                "deploy",
                None,
                f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock",
                True,
            )

        start_cmd = commands[-1]
        remote_socket = f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock"
        expected_inner = (
            f"/opt/fastagent-{fastagent_plugin.AGENT_VERSION}-linux-amd64 "
            f"--daemon --socket {remote_socket} --allow-user deploy "
            f"</dev/null >>{remote_socket}.log 2>&1"
        )
        self.assertIn(f"setsid sudo sh -c {shlex_quote(expected_inner)} &",
                      start_cmd)
        self.assertNotIn(f"sudo /opt/fastagent-{fastagent_plugin.AGENT_VERSION}"
                         f"-linux-amd64 --daemon --socket {remote_socket} "
                         f"--allow-user deploy </dev/null >>{remote_socket}.log",
                         start_cmd)


def shlex_quote(value: str) -> str:
    return fastagent_plugin.shlex.quote(value)


if __name__ == "__main__":
    unittest.main()
