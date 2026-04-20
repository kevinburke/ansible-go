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
                req_id = json.loads(req_line).get("id", 0)
                count += 1
                if count > 1:
                    time.sleep(self._delay_s)
                resp = json.dumps({"id": req_id, "result": {"ok": True}}) + "\n"
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
    return conn


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


if __name__ == "__main__":
    unittest.main()
