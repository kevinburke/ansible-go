"""Regression tests for the fastagent connection plugin.

Targets socket lifecycle behavior that is easy to regress, notably: the
2-second timeout used to probe the local forwarding socket must not
persist into later RPC reads. An earlier version shipped without clearing
it, which made any module whose exec took >2s (e.g. ufw modifying
iptables) fail with `timed out` / `cannot read from timed out object`.
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
import fastagent as fastagent_plugin  # noqa: E402


class _DelayedEchoServer:
    """Unix socket server that sleeps before responding to one RPC.

    Reads one JSON-RPC line, waits `delay_s`, then writes back a single
    valid response echoing the request id. Used to exercise the client
    read path with a response time longer than the connect probe timeout.
    """

    def __init__(self, delay_s: float):
        self._delay_s = delay_s
        self._tmpdir = tempfile.mkdtemp(prefix="fastagent-test-")
        self.path = os.path.join(self._tmpdir, "s.sock")
        self._server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        try:
            req_line = conn.makefile("rb").readline()
            if not req_line:
                return
            req_id = json.loads(req_line).get("id", 0)
            time.sleep(self._delay_s)
            resp = json.dumps({"id": req_id, "result": {"ok": True}}) + "\n"
            conn.sendall(resp.encode("utf-8"))
        finally:
            conn.close()

    def close(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self._tmpdir)
        except OSError:
            pass


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


class TestTryLocalSocket(unittest.TestCase):
    def test_connect_clears_probe_timeout(self) -> None:
        server = _DelayedEchoServer(delay_s=0.0)
        self.addCleanup(server.close)
        conn = _bare_connection()
        self.addCleanup(lambda: conn._socket and conn._socket.close())

        self.assertTrue(conn._try_local_socket(server.path, "test-host"))
        # The probe timeout must not persist — blocking I/O means None.
        self.assertIsNone(conn._socket.gettimeout())
        self.assertTrue(conn._connected)

    def test_rpc_survives_delay_longer_than_probe_timeout(self) -> None:
        # The regressed bug: a response taking longer than the 2s connect
        # probe timeout raised `timed out` because the timeout was never
        # cleared. Use 2.2s so the test catches that exact regression.
        server = _DelayedEchoServer(delay_s=2.2)
        self.addCleanup(server.close)
        conn = _bare_connection()
        self.addCleanup(lambda: conn._socket and conn._socket.close())

        self.assertTrue(conn._try_local_socket(server.path, "test-host"))
        result = conn._agent_client.call("Exec", {})
        self.assertEqual(result, {"ok": True})

    def test_missing_socket_file_returns_false(self) -> None:
        conn = _bare_connection()
        missing = os.path.join(tempfile.gettempdir(), "fastagent-nonexistent.sock")
        self.assertFalse(conn._try_local_socket(missing, "test-host"))
        self.assertFalse(conn._connected)
        self.assertIsNone(conn._socket)


if __name__ == "__main__":
    unittest.main()
