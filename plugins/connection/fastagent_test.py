"""Regression tests for the fastagent connection plugin.

Targets socket lifecycle behavior that is easy to regress, notably: the
2-second timeout used to probe the local forwarding socket must not
persist into later RPC reads. An earlier version shipped without clearing
it, which made any module whose exec took >2s (e.g. ufw modifying
iptables) fail with `timed out` / `cannot read from timed out object`.

The timeout tests use os.pipe() to control reply timing. The daemon readiness
integration test uses a real AF_UNIX socket, so CI must permit Unix sockets.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import select
import signal
import socket as socket_mod
import stat as stat_module
import subprocess
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

    def test_deferred_hello_keeps_probe_timeout_until_answered(self) -> None:
        # The fast path sends the Hello with the first request, so the probe
        # timeout must cover the Hello's answer and then be cleared before
        # the request's answer, which can take longer than 2s.
        client_r, client_w, server_r, server_w = _make_pipe_pair()
        server = _PipeEchoServer(delay_s=2.2, server_r=server_r, server_w=server_w)
        self.addCleanup(server.close)
        mock_sock = _MockSocket(client_r, client_w)
        self.addCleanup(mock_sock.close)

        conn = _bare_connection()
        with mock.patch.object(fastagent_plugin.socket_mod, "socket", return_value=mock_sock), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True):
            self.assertTrue(conn._try_local_socket("/fake/socket.sock", "test-host", verify=False))
        self.assertTrue(conn._connected)
        self.assertEqual(conn._socket.gettimeout(), 2)
        self.assertEqual(conn._agent_client.call("Exec", {}), {"ok": True})
        self.assertIsNone(conn._socket.gettimeout())

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
    """Regression tests for how set_become_plugin attaches sudo.

    ActionBase._low_level_execute_command wraps module invocations
    with `sudo -u <user> sh -c …` whenever `self._connection.become`
    is truthy. The fastagent connection handles become itself via the
    Exec RPC's become_user field, so that wrap must be a no-op.
    Otherwise non-sudoer become_users hit "<user> is not in the sudoers
    file" because the inner sudo runs as the target user.

    An earlier version suppressed the wrap by leaving self.become as
    None. That also told ActionBase._is_become_unprivileged() that no
    become was in effect, so _fixup_perms2 never granted the
    become_user access to files uploaded into the remote tmpdir, and
    every builtin copy/template fallback under a non-root become_user
    failed with "Source ... not found". The sudo plugin must therefore
    stay attached, wrapped so it doesn't build a sudo command.
    """

    def test_sudo_plugin_is_attached_without_command_wrap(self) -> None:
        conn = _bare_connection()
        plugin = _FakeBecomePlugin("sudo", {"become_user": "returns"})
        conn.set_become_plugin(plugin)
        self.assertIsNotNone(
            conn.become,
            "self.become must be set so ActionBase knows become is in effect",
        )
        self.assertEqual(conn.become.name, "sudo")
        self.assertEqual(conn.become.get_option("become_user"), "returns")
        self.assertTrue(conn.become.fastagent_handles_become)
        self.assertEqual(
            conn.become.build_become_command("/bin/sh -c 'echo hi'", None),
            "/bin/sh -c 'echo hi'",
            "the agent does the sudo; ActionBase must not wrap the command",
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
        self.assertTrue(conn.become.fastagent_handles_become)
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
        with mock.patch.object(fastagent_plugin.display, "warning") as warning:
            conn.set_become_plugin(plugin)
        warning.assert_called_once()
        # Ansible's own wrap handles non-sudo methods.
        self.assertIs(conn.become, plugin)
        self.assertFalse(conn._use_become)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestActionBaseSeesBecome(unittest.TestCase):
    """Check the attached become plugin against ansible-core's ActionBase.

    Uses the real sudo become plugin and the real ActionBase methods, so
    a change in how either side reads `connection.become` shows up here.
    """

    def _action(self, become_user: str):
        from ansible.plugins.action import ActionBase
        from ansible.plugins.loader import become_loader

        plugin = become_loader.get("sudo")
        # TaskExecutor populates the options on connection.become; do the
        # same after attaching.
        conn = _bare_connection()
        conn.set_become_plugin(plugin)
        conn.become.set_options(direct={"become_user": become_user})

        class _Action(ActionBase):
            def run(self, tmp=None, task_vars=None):
                raise NotImplementedError

        action = _Action.__new__(_Action)
        action._connection = conn
        action._get_admin_users = lambda: ["root", "toor"]
        action._get_remote_user = lambda: "deploy"
        return action

    def test_unprivileged_become_user_is_detected(self) -> None:
        # This is what makes ActionBase._fixup_perms2 grant the
        # become_user access to files uploaded into the remote tmpdir.
        action = self._action("app")
        self.assertTrue(action._is_become_unprivileged())
        self.assertEqual(action.get_become_option("become_user"), "app")

    def test_root_become_user_is_not_unprivileged(self) -> None:
        action = self._action("root")
        self.assertFalse(action._is_become_unprivileged())

    def test_real_sudo_plugin_command_is_not_wrapped(self) -> None:
        action = self._action("app")
        cmd = "/bin/sh -c 'echo hi'"
        self.assertEqual(
            action._connection.become.build_become_command(cmd, None), cmd
        )

    def test_action_overrides_see_sudo_as_agent_handled(self) -> None:
        # The action overrides decide fast path vs. fallback with
        # ansible_applies_become; sudo must stay on the fast path.
        for user in ("root", "app"):
            with self.subTest(become_user=user):
                conn = self._action(user)._connection
                self.assertFalse(fastagent_client.ansible_applies_become(conn))

    def test_action_overrides_see_unsupported_method(self) -> None:
        from ansible.plugins.loader import become_loader

        conn = _bare_connection()
        conn.set_become_plugin(become_loader.get("su"))
        self.assertTrue(fastagent_client.ansible_applies_become(conn))

    def test_marker_name_matches_helper(self) -> None:
        self.assertTrue(
            getattr(
                fastagent_plugin._AgentHandledBecome,
                fastagent_client.AGENT_HANDLES_BECOME_ATTR,
            )
        )


class _RecordingAgentClient:
    """Captures the kwargs passed to exec() for assertion in tests."""

    def __init__(self):
        self.last_kwargs: dict | None = None
        self.broken = False

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
    ansible only populated plugin options when `connection.become is
    not None` — and at the time we kept it None to suppress the
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

    def _conn(self, become_user, *, use_become=True, remote_user=None):
        conn = _bare_connection()
        conn._use_become = use_become
        conn._play_context = _FakePlayContext(become_user)
        conn.get_option = lambda key, *a, **kw: (
            remote_user if key == "remote_user" else None
        )
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

    def test_become_user_equals_remote_user_still_wraps(self) -> None:
        # Ansible-core's classic SSH path skips the per-task sudo wrap
        # when remote_user == become_user (BECOME_ALLOW_SAME_USER=False)
        # because the SSH session itself is running as remote_user.
        # Fastagent's daemon is sudo-wrapped at launch and runs as
        # root whenever the play uses become, so it does *not* match
        # remote_user — skipping the per-RPC wrap would leave the
        # command running as root and the resulting files root-owned.
        # Hit this with the dotfiles clone in roles/dev (caracal-server)
        # where ansible_user=kevin and become_user=kevin both resolved
        # to "kevin" but the agent was running as root.
        self.assertEqual(
            self._conn("returns", remote_user="returns").get_become_user(),
            "returns",
        )


# The upload tests patch subprocess.run and run the remote script for real.
_REAL_SUBPROCESS_RUN = subprocess.run


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestEnsureAgentDeployed(unittest.TestCase):
    """Binary uploads use the same dedicated SSH master as bootstrap probes.

    Configured SSH arguments are preserved, with the explicit bootstrap
    control socket taking precedence. Uploads used to go through a separate
    scp invocation that only got -i and -P.
    """

    def _conn(self, options):
        conn = _bare_connection()
        conn.get_option = lambda key, *a, **kw: options.get(key)
        return conn

    def _deploy(self, conn, remote_path, run_upload, version_stdout=b""):
        """Run _ensure_agent_deployed with the version check printing
        version_stdout (by default, nothing: no agent) and the upload
        handled by run_upload(cmd, stdin)."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "--version" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0, version_stdout, b"")
            return run_upload(cmd, kwargs["stdin"])

        with tempfile.NamedTemporaryFile(delete=False) as binary:
            binary.write(b"\x7fELF fake agent\n" * 1000)
        self.addCleanup(os.unlink, binary.name)
        with mock.patch.object(conn, "_find_local_binary", return_value=binary.name), \
                mock.patch.object(fastagent_plugin.subprocess, "run", side_effect=fake_run):
            conn._ensure_agent_deployed("fabench", None, None, remote_path, "arm64",
                                        control_socket="/tmp/test-bootstrap.ctl")
        return calls, binary.name

    def test_upload_uses_ssh_command_and_args(self):
        conn = self._conn({
            "ssh_executable": "/usr/bin/ssh",
            "ssh_args": "-F /cfg/ssh_config -o ProxyJump=bastion",
        })
        seen = {}

        def upload(cmd, stdin):
            seen["stdin"] = stdin.read()
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        calls, binary = self._deploy(conn, "/home/k/.ansible/fastagent/agent", upload)
        with open(binary, "rb") as f:
            self.assertEqual(seen["stdin"], f.read())
        self.assertEqual(len(calls), 2, calls)
        upload_cmd = calls[1]
        self.assertEqual(upload_cmd[0], "/usr/bin/ssh")
        self.assertEqual(upload_cmd[5:9], ["-F", "/cfg/ssh_config", "-o", "ProxyJump=bastion"])
        for cmd in calls:
            self.assertEqual(cmd[-4:-2], ["-S", "/tmp/test-bootstrap.ctl"])
        self.assertIn("fabench", upload_cmd)
        self.assertFalse(any("scp" in part for part in upload_cmd[:-1]), upload_cmd)

    def test_skips_upload_only_for_the_exact_version(self):
        # A substring check let a remote agent whose version merely
        # contains ours (a dev build of this release, say) stand in for it.
        version = fastagent_plugin.AGENT_VERSION
        for reply, uploads in [
            (f"fastagent {version}\n", False),
            (f"fastagent {version}-dev.g39d5d02abcde\n", True),
            (f"fastagent 1{version}\n", True),
            (f"{version}\n", True),
            ("", True),
        ]:
            with self.subTest(reply=reply):
                def upload(cmd, stdin):
                    return subprocess.CompletedProcess(cmd, 0, b"", b"")

                calls, _ = self._deploy(self._conn({}), "/x/agent", upload, reply.encode())
                self.assertEqual(len(calls), 2 if uploads else 1, calls)

    def _run_locally(self, cmd, stdin):
        # The last argument is what ssh would hand to the remote shell.
        return _REAL_SUBPROCESS_RUN(["/bin/sh", "-c", cmd[-1]], stdin=stdin, capture_output=True)

    def test_remote_script_installs_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "new dir", "fastagent-0.9.0-linux-arm64")
            _, binary = self._deploy(self._conn({}), dest, self._run_locally)
            with open(binary, "rb") as a, open(dest, "rb") as b:
                self.assertEqual(a.read(), b.read())
            self.assertEqual(stat_module.S_IMODE(os.stat(dest).st_mode), 0o755)
            self.assertEqual(os.listdir(os.path.dirname(dest)), [os.path.basename(dest)])

    def test_truncated_upload_fails_and_cleans_up(self):
        def truncated(cmd, stdin):
            return _REAL_SUBPROCESS_RUN(["/bin/sh", "-c", cmd[-1]], input=stdin.read(10), capture_output=True)

        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "agent")
            with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "received 10 bytes .*expected 16000"):
                self._deploy(self._conn({}), dest, truncated)
            self.assertEqual(os.listdir(d), [])

    def test_upload_failure_reports_stderr(self):
        def fail(cmd, stdin):
            return subprocess.CompletedProcess(cmd, 255, b"", b"ssh: Could not resolve hostname fabench")

        with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "Could not resolve hostname fabench"):
            self._deploy(self._conn({}), "/x/agent", fail)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestPortOption(unittest.TestCase):
    def test_port_has_no_default(self):
        # A default would always be passed as -o Port=22 and override a
        # Port from ~/.ssh/config or an ssh_args -F file, as ansible-core's
        # ssh connection avoids.
        import yaml

        options = yaml.safe_load(fastagent_plugin.DOCUMENTATION)["options"]
        self.assertNotIn("default", options["port"])

    def test_no_port_option_when_unset(self):
        conn = self._conn()
        cmd = conn._build_ssh_command("fabench", None, None, "true")
        self.assertFalse(any(part.startswith("Port=") for part in cmd), cmd)

    def _conn(self):
        conn = _bare_connection()
        conn.get_option = lambda key, *a, **kw: None
        return conn


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

    def test_readiness_probe_against_real_daemon(self):
        from fastagent_client_test import _get_agent_binary

        binary = _get_agent_binary()
        with tempfile.TemporaryDirectory(prefix="fastagent-probe-") as directory:
            path = os.path.join(directory, "agent.sock")
            with subprocess.Popen(
                [binary, "--daemon", "--socket", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as daemon:
                try:
                    self.assertTrue(select.select([daemon.stdout], [], [], 5)[0], "daemon did not become ready")
                    self.assertEqual(daemon.stdout.readline().decode().strip(), path)
                    conn = self._conn()
                    conn.get_option = lambda key, *a, **kw: binary if key == "agent_path" else None

                    def run_probe(host, user, port, command, *, control_socket):
                        self.assertIn("--connect --socket", command)
                        result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=5)
                        return result.returncode, result.stdout, result.stderr

                    with mock.patch.object(conn, "_run_bootstrap_ssh_command", side_effect=run_probe) as ssh, \
                         mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
                         mock.patch.object(conn, "_ensure_agent_deployed"):
                        conn._ensure_remote_daemon("localhost", None, 22, path, False,
                                                   control_socket="/tmp/test-bootstrap.ctl")
                    self.assertEqual(ssh.call_count, 1)
                    self.assertIsNone(daemon.poll())
                finally:
                    daemon.terminate()
                    try:
                        daemon.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.communicate()
                        raise

    def test_become_detaches_sudo_stdio_and_opens_log_as_root(self) -> None:
        conn = self._conn()
        commands = []

        def run_ssh(host, user, port, command, *, control_socket):
            commands.append(command)
            if command.startswith("test -S "):
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(conn, "_run_bootstrap_ssh_command", side_effect=run_ssh), \
             mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
             mock.patch.object(conn, "_ensure_agent_deployed"):
            conn._ensure_remote_daemon(
                "serval",
                "deploy",
                None,
                f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock",
                True,
                control_socket="/tmp/test-bootstrap.ctl",
            )

        start_cmd = commands[-1]
        remote_socket = f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock"
        expected_inner = (
            f"exec /opt/fastagent-{fastagent_plugin.AGENT_VERSION}-linux-amd64 "
            f"--daemon --socket {remote_socket} --allow-user deploy "
            f"</dev/null >>{remote_socket}.log 2>&1"
        )
        self.assertIn("sudo -n true || exit $?; ", start_cmd)
        self.assertIn(
            f"setsid sudo -n sh -c {shlex_quote(expected_inner)} "
            f"</dev/null >/dev/null 2>&1 &",
            start_cmd,
        )
        self.assertNotIn("setsid sudo sh -c", start_cmd)
        self.assertNotIn(f"sudo /opt/fastagent-{fastagent_plugin.AGENT_VERSION}"
                         f"-linux-amd64 --daemon --socket {remote_socket} "
                         f"--allow-user deploy </dev/null >>{remote_socket}.log",
                         start_cmd)

    def test_root_remote_user_skips_sudo_wrap(self) -> None:
        # When the SSH user is already root (e.g. `ansible_user: root`
        # on a Proxmox host), wrapping the daemon launch with `sudo -n`
        # is redundant. More importantly, on minimal hosts that don't
        # ship the sudo package the wrap fails with `sudo: command not
        # found` (rc=127) and the daemon never starts. Mirrors
        # ansible-core's BECOME_ALLOW_SAME_USER=False default.
        conn = self._conn()
        commands = []

        def run_ssh(host, user, port, command, *, control_socket):
            commands.append(command)
            if command.startswith("test -S "):
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(conn, "_run_bootstrap_ssh_command", side_effect=run_ssh), \
             mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
             mock.patch.object(conn, "_ensure_agent_deployed"):
            conn._ensure_remote_daemon(
                "caracal",
                "root",
                None,
                f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock",
                False,  # wrap_with_sudo: False because remote_user is root
                control_socket="/tmp/test-bootstrap.ctl",
            )

        start_cmd = commands[-1]
        # No sudo anywhere — bootstrap must be safe on hosts that don't
        # ship the sudo package.
        self.assertTrue(all("sudo" not in command for command in commands))
        self.assertNotIn("sudo", start_cmd)
        self.assertIn("setsid /opt/fastagent-", start_cmd)

    def test_unusable_socket_is_not_mistaken_for_a_live_daemon(self) -> None:
        # A dead or inaccessible socket may still exist on disk. Probe it
        # as the connecting user, then leave cleanup to the locked daemon.
        conn = self._conn()
        commands = []

        def run_ssh(host, user, port, command, *, control_socket):
            commands.append(command)
            if "--connect --socket" in command:
                return 1, "", "connection refused"
            return 0, "", ""

        with mock.patch.object(conn, "_run_bootstrap_ssh_command", side_effect=run_ssh), \
             mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
             mock.patch.object(conn, "_ensure_agent_deployed"):
            conn._ensure_remote_daemon(
                "serval", "admin", None,
                f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock",
                True,
                control_socket="/tmp/test-bootstrap.ctl",
            )

        liveness_cmd = commands[0]
        self.assertIn("--connect --socket", liveness_cmd)
        self.assertIn('"method": "Hello"', liveness_cmd)
        self.assertIn("--daemon --socket", commands[-1])
        for command in commands:
            self.assertNotIn("pkill", command)
            self.assertNotIn("rm -f", command)

    def test_daemon_running_and_usable_skips_restart(self) -> None:
        conn = self._conn()
        commands = []

        def run_ssh(host, user, port, command, *, control_socket):
            commands.append(command)
            return 0, json.dumps({"id": 1, "result": {"version": fastagent_plugin.AGENT_VERSION}}), ""

        with mock.patch.object(conn, "_run_bootstrap_ssh_command", side_effect=run_ssh), \
             mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
             mock.patch.object(conn, "_ensure_agent_deployed"):
            conn._ensure_remote_daemon(
                "serval", "kevin", None,
                f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock",
                True,
                control_socket="/tmp/test-bootstrap.ctl",
            )

        self.assertEqual(len(commands), 1)
        self.assertIn("--connect --socket", commands[0])
        self.assertNotIn("--daemon", commands[0])

    def test_malformed_or_wrong_version_probe_cannot_skip_bootstrap(self):
        for reply in ("not json", '{"id":1}', '{"id":1,"result":{"version":"old"}}'):
            with self.subTest(reply=reply):
                conn = self._conn()
                with mock.patch.object(conn, "_run_bootstrap_ssh_command", return_value=(0, reply, "")) as ssh, \
                     mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
                     mock.patch.object(conn, "_ensure_agent_deployed"):
                    conn._ensure_remote_daemon("serval", "deploy", 22, "/tmp/test.sock", False,
                                               control_socket="/tmp/test-bootstrap.ctl")
                self.assertIn("--daemon --socket", ssh.call_args.args[3])


def shlex_quote(value: str) -> str:
    return fastagent_plugin.shlex.quote(value)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestConnectRetriesLocalSocketProbe(unittest.TestCase):
    """A freshly created forwarder's socket *file* can exist before the
    remote-side streamlocal proxy is ready to serve a connection, so the
    post-forwarding Hello probe can lose a benign race. _connect must
    retry that probe instead of failing on the first attempt.
    """

    def _conn(self):
        conn = _bare_connection()
        conn.get_option = lambda key, *a, **kw: {
            "host": "serval",
            "remote_user": "kevin",
            "port": None,
        }.get(key)
        # A no-op setup lock: these tests are about the post-forwarding
        # probe, and the real lock would create a file in /tmp, which is
        # read-only inside the CI sandbox (TestLocalSocketSetupLock covers
        # the lock against a temporary directory).
        conn._local_socket_setup_lock = (
            lambda *a, **kw: contextlib.nullcontext())
        return conn

    def test_transient_race_after_fresh_forwarding_is_retried(self) -> None:
        conn = self._conn()
        # Call 1: the fast-path probe, before forwarding exists (fails).
        # Call 2: the setup-lock re-check, which also fails (no other
        # fork has finished setup either), so setup proceeds.
        # Calls 3-4: the post-forwarding probe loses the race twice.
        # Call 5: the race clears and the probe succeeds.
        probe_results = iter([False, False, False, False, True])
        with mock.patch.object(
                conn, "_try_local_socket",
                side_effect=lambda *a, **kw: next(probe_results)), \
             mock.patch.object(conn, "_ensure_remote_daemon"), \
             mock.patch.object(conn, "_ensure_ssh_forwarding"), \
             mock.patch.object(fastagent_plugin.time_mod, "sleep") as mock_sleep:
            result = conn._connect()

        self.assertIs(result, conn)
        # One backoff sleep per failed retry attempt, none after success.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_gives_up_after_retries_exhausted(self) -> None:
        conn = self._conn()
        with mock.patch.object(conn, "_try_local_socket", return_value=False), \
             mock.patch.object(conn, "_ensure_remote_daemon"), \
             mock.patch.object(conn, "_ensure_ssh_forwarding"), \
             mock.patch.object(fastagent_plugin.time_mod, "sleep"):
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure):
                conn._connect()


@unittest.skipIf(_FASTAGENT_IMPORT_ERROR is not None, "ansible is required")
class TestBootstrapSshReuse(unittest.TestCase):
    def _conn(self):
        conn = _bare_connection()
        conn.get_option = lambda key: {
            "ssh_args": "-F none -o ControlMaster=auto -o ControlPersist=60s -S /tmp/shared",
        }.get(key)
        return conn

    def test_bootstrap_uses_the_tunnel_master_before_probing_the_daemon(self):
        conn = self._conn()
        events = []
        commands = []
        ordinary_command = conn._build_ssh_command("host", "deploy", 2222, "uname -m")

        def bootstrap(*args, control_socket):
            events.append("daemon")
            commands.append(conn._build_bootstrap_ssh_command(
                "host", "deploy", 2222, "uname -m", control_socket=control_socket))
            # Identity calculation uses the ordinary builder. Its output
            # must stay unchanged even while bootstrap is in progress.
            self.assertEqual(conn._build_ssh_command("host", "deploy", 2222, "uname -m"),
                             ordinary_command)

        with tempfile.TemporaryDirectory(prefix="fa-bootstrap-") as directory:
            local = directory + "/local.sock"
            with mock.patch.object(conn, "_try_local_socket", side_effect=[False, True]), \
                 mock.patch.object(conn, "_ensure_ssh_forwarding",
                                   side_effect=lambda *args: events.append("forward")), \
                 mock.patch.object(conn, "_ensure_remote_daemon", side_effect=bootstrap):
                conn._setup_local_socket("host", "deploy", 2222, local, "/tmp/remote.sock", True)

            self.assertEqual(events, ["forward", "daemon"])
            # Ask OpenSSH to resolve competing options, rather than merely
            # checking that our preferred spelling appears in the argv.
            result = subprocess.run(commands[0][:-2] + ["-G", "host"],
                                    capture_output=True, text=True, check=True)
            config = dict(line.split(" ", 1) for line in result.stdout.splitlines())
            self.assertEqual(config["controlpath"], local + ".ctl")
            self.assertEqual(config["controlmaster"], "false")
            self.assertEqual(config["proxycommand"], "false")
            self.assertEqual(config["user"], "deploy")
            self.assertEqual(config["port"], "2222")
            self.assertEqual(conn._build_ssh_command("host", "deploy", 2222, "uname -m"),
                             ordinary_command)

    def test_bootstrap_failure_retires_the_tunnel(self):
        conn = self._conn()
        ordinary_command = conn._build_ssh_command("host", None, None, "")
        with tempfile.TemporaryDirectory(prefix="fa-bootstrap-") as directory:
            local = directory + "/local.sock"
            with mock.patch.object(conn, "_try_local_socket", return_value=False), \
                 mock.patch.object(conn, "_ensure_ssh_forwarding"), \
                 mock.patch.object(conn, "_ensure_remote_daemon", side_effect=RuntimeError("bootstrap failed")), \
                 mock.patch.object(conn, "_retire_stale_forwarder") as retire:
                with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                    conn._setup_local_socket("host", None, None, local, "/tmp/remote.sock", False)
            retire.assert_called_once_with(local, "host")
            self.assertEqual(conn._build_ssh_command("host", None, None, ""), ordinary_command)

    def test_missing_master_cannot_silently_open_another_ssh_connection(self):
        conn = self._conn()
        with tempfile.TemporaryDirectory(prefix="fa-bootstrap-") as directory:
            marker = directory + "/fresh-connection"
            proxy = shlex_quote("touch " + shlex_quote(marker))
            conn.get_option = lambda key: {
                "ssh_args": f"-F none -o BatchMode=yes -o ProxyCommand={proxy}",
            }.get(key)
            rc, _, _ = conn._run_bootstrap_ssh_command(
                "host", None, None, "true", control_socket=directory + "/missing.ctl")
            self.assertNotEqual(rc, 0)
            self.assertFalse(os.path.exists(marker), "SSH silently fell back to a fresh connection")


@unittest.skipIf(_FASTAGENT_IMPORT_ERROR is not None, "ansible is required")
class TestConnectionIsolation(unittest.TestCase):
    def _paths(self, **options):
        conn = _bare_connection()
        values = {"host": "serval", "remote_user": "deploy", "port": 22}
        values.update(options)
        conn._use_become = values.pop("become", False)
        conn.get_option = lambda key, *a, **kw: values.get(key)
        # False, False: the fast-path probe and the setup-lock re-check
        # both fail (no other fork has set this local_socket up), so
        # setup proceeds to _ensure_remote_daemon/_ensure_ssh_forwarding.
        # True: the post-forwarding probe then succeeds immediately.
        #
        # The setup lock itself is mocked out to a no-op here: these
        # tests are about socket *path* isolation (the identity digest),
        # not the lock's own file-safety checks (covered separately by
        # TestLocalSocketSetupLock), and some subtests patch os.getuid()
        # to vary the digest, which would otherwise make a freshly
        # created (real-uid-owned) lock file fail that safety check
        # against the mocked uid.
        with mock.patch.object(conn, "_try_local_socket", side_effect=[False, False, True]), \
             mock.patch.object(conn, "_ensure_remote_daemon") as daemon, \
             mock.patch.object(conn, "_ensure_ssh_forwarding") as forward, \
             mock.patch.object(
                conn, "_local_socket_setup_lock",
                side_effect=lambda *a, **kw: contextlib.nullcontext()):
            conn._connect()
        return forward.call_args.args[3], daemon.call_args.args[3]

    def test_transport_options_isolate_both_ends(self):
        original = self._paths()
        for options in (
            {"remote_user": "admin"}, {"port": 2222},
            {"private_key": "/tmp/another-key"},
            {"ssh_args": "-F /tmp/another-config"},
            {"ssh_executable": "/tmp/another-ssh"}, {"become": True},
            {"agent_path": "/opt/another-agent"},
        ):
            with self.subTest(options=options):
                changed = self._paths(**options)
                self.assertNotEqual(original[0], changed[0])
                self.assertNotEqual(original[1], changed[1])
        self.assertEqual(original, self._paths())

    def test_working_directory_and_agent_environment_isolate_connections(self):
        original = self._paths()
        with mock.patch.object(fastagent_plugin.os, "getcwd", return_value="/another-checkout"):
            self.assertNotEqual(original, self._paths())
        with mock.patch.dict(os.environ, SSH_AUTH_SOCK="/tmp/another-auth-agent"):
            self.assertNotEqual(original, self._paths())
        with mock.patch.object(fastagent_plugin.os, "getuid", return_value=os.getuid() + 1):
            self.assertNotEqual(original, self._paths())

    def test_socket_names_fit_unix_limit_for_long_hostnames(self):
        local, remote = self._paths(host="x" * 180 + ".example.com")
        # OpenSSH appends a dot plus 16 random characters while binding
        # the control socket atomically, before renaming it into place.
        self.assertLess(len((local + ".ctl").encode()) + 17, 104)
        self.assertLess(len(local.encode()), 104)
        self.assertLess(len(remote.encode()), 104)

    def test_changing_options_reconnects_an_existing_connection(self):
        conn = _bare_connection()
        options = {"host": "serval", "remote_user": "deploy", "port": 22}
        conn.get_option = lambda key, *a, **kw: options.get(key)
        def connected(*args, **kwargs):
            conn._connected = True
            return True
        with mock.patch.object(conn, "_try_local_socket", side_effect=connected) as probe:
            conn._connect()
            conn._connect()
            self.assertEqual(probe.call_count, 1)
            options["remote_user"] = "admin"
            conn._connect()
            self.assertEqual(probe.call_count, 2)
            self.assertNotEqual(probe.call_args_list[0].args[0], probe.call_args_list[1].args[0])


class _HangUpServer:
    """Answers Hello, then records requests and optionally hangs up.

    With hang_up=True it closes the stream after reading the first request
    that follows Hello, without answering, the way a connection looks when
    its `ssh -L` forwarder is killed mid-request. Otherwise it answers every
    request. `methods` records each request received after Hello.
    """

    def __init__(self, server_r, server_w, *, hang_up):
        self._server_r = server_r
        self._server_w = server_w
        self._hang_up = hang_up
        self.methods = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while True:
                line = self._server_r.readline()
                if not line:
                    return
                req = json.loads(line)
                if req["method"] == "Hello":
                    result = {"version": req["params"]["version"], "capabilities": []}
                else:
                    self.methods.append((req["method"], req["params"].get("cmd_string")))
                    if self._hang_up:
                        self._server_w.close()
                        return
                    result = {"rc": 0, "stdout": "ok\n", "stderr": ""}
                self._server_w.write((json.dumps({"id": req["id"], "result": result}) + "\n").encode())
                self._server_w.flush()
        except Exception:
            pass

    def join(self):
        self._thread.join(timeout=5)

    def close(self):
        for f in (self._server_r, self._server_w):
            try:
                f.close()
            except Exception:
                pass


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestReconnectAfterBrokenStream(unittest.TestCase):
    """A stream that breaks mid-request must not poison later requests.

    Seen 2026-09-28: the local `ssh -L` forwarder was killed while a ufw
    loop item was running. That item correctly failed with "execution
    outcome unknown", but every remaining item of the loop then failed with
    "RPC stream is unusable after an earlier failure", because the
    connection kept handing out the broken client. The failed request must
    still never be replayed; only the next request gets a new stream.
    """

    def _connection(self):
        conn = _bare_connection()
        conn._play_context = _FakePlayContext(None)
        options = {"host": "serval", "remote_user": "deploy", "port": 22}
        conn.get_option = lambda key, *a, **kw: options.get(key)
        servers, sockets = [], []
        for hang_up in (True, False):
            client_r, client_w, server_r, server_w = _make_pipe_pair()
            server = _HangUpServer(server_r, server_w, hang_up=hang_up)
            self.addCleanup(server.close)
            sock = _MockSocket(client_r, client_w)
            self.addCleanup(sock.close)
            servers.append(server)
            sockets.append(sock)
        patches = [
            mock.patch.object(fastagent_plugin.socket_mod, "socket", side_effect=sockets),
            mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True),
            # Reaching setup would mean the probe of the new stream failed.
            mock.patch.object(conn, "_setup_local_socket", side_effect=AssertionError("setup")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        conn._connect()
        return conn, servers

    def test_exec_command_reconnects_without_replaying(self) -> None:
        conn, (dying, fresh) = self._connection()
        rc, _, stderr = conn.exec_command("ufw allow 1")
        self.assertEqual(rc, 1)
        self.assertIn(b"execution outcome unknown", stderr)

        rc, stdout, stderr = conn.exec_command("ufw allow 2")
        self.assertEqual((rc, stdout, stderr), (0, b"ok\n", b""))
        dying.join()
        self.assertEqual(dying.methods, [("Exec", "ufw allow 1")])
        self.assertEqual(fresh.methods, [("Exec", "ufw allow 2")])

    def test_connect_replaces_a_broken_client(self) -> None:
        # Action plugin overrides call _connect() and then use
        # _agent_client directly, so _connect itself must not return early
        # while the client is broken.
        conn, (dying, fresh) = self._connection()
        broken = conn._agent_client
        with self.assertRaisesRegex(IOError, "execution outcome unknown"):
            broken.exec(cmd_string="ufw allow 1")
        conn._connect()
        self.assertIsNot(conn._agent_client, broken)
        self.assertEqual(conn._agent_client.exec(cmd_string="ufw allow 2")["rc"], 0)
        self.assertEqual(fresh.methods, [("Exec", "ufw allow 2")])

    def test_put_file_reconnects(self) -> None:
        conn, (dying, fresh) = self._connection()
        conn.exec_command("ufw allow 1")
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"x")
            f.flush()
            conn.put_file(f.name, "/tmp/dest")
        self.assertEqual(fresh.methods, [("WriteFile", None)])


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestDeferredHello(unittest.TestCase):
    """The fast path sends Hello with the task's first request.

    A stale local socket (the daemon idle-timed out, or the host rebooted)
    used to be caught by a Hello at connect time, before any request went
    out. Now it is caught by the first request's Hello, and the connection
    must still set up a new daemon and forwarder and run the request there.
    """

    def _connection(self, first_server_cls):
        conn = _bare_connection()
        conn._play_context = _FakePlayContext(None)
        options = {"host": "serval", "remote_user": "deploy", "port": 22}
        conn.get_option = lambda key, *a, **kw: options.get(key)

        client_r, client_w, server_r, server_w = _make_pipe_pair()
        first = first_server_cls(server_r, server_w)
        self.addCleanup(first.close)
        first_sock = _MockSocket(client_r, client_w)
        self.addCleanup(first_sock.close)

        client_r, client_w, server_r, server_w = _make_pipe_pair()
        fresh = _HangUpServer(server_r, server_w, hang_up=False)
        self.addCleanup(fresh.close)
        fresh_sock = _MockSocket(client_r, client_w)
        self.addCleanup(fresh_sock.close)

        setups = []

        def setup(*args):
            # What _setup_local_socket leaves behind once a new
            # forwarder answers a Hello.
            setups.append(args)
            self.assertTrue(conn._try_local_socket(args[3], args[0]))

        patches = [
            mock.patch.object(fastagent_plugin.socket_mod, "socket",
                              side_effect=[first_sock, fresh_sock]),
            mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True),
            mock.patch.object(conn, "_setup_local_socket", side_effect=setup),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        conn._connect()
        self.assertEqual(setups, [], "the fast path must not probe before the first request")
        return conn, first, fresh, setups

    def test_stale_socket_runs_first_request_on_new_connection(self) -> None:
        conn, _, fresh, setups = self._connection(_SilentServer)
        client = conn._agent_client
        rc, stdout, stderr = conn.exec_command("ufw allow 1")
        self.assertEqual((rc, stdout, stderr), (0, b"ok\n", b""))
        self.assertEqual(len(setups), 1)
        self.assertEqual(fresh.methods, [("Exec", "ufw allow 1")])
        # Action plugins hold the client object across calls.
        self.assertIs(conn._agent_client, client)
        self.assertFalse(client.broken)
        self.assertEqual(client.exec(cmd_string="ufw allow 2")["rc"], 0)
        self.assertEqual(fresh.methods, [("Exec", "ufw allow 1"), ("Exec", "ufw allow 2")])

    def test_live_socket_needs_no_setup(self) -> None:
        def answering(server_r, server_w):
            return _HangUpServer(server_r, server_w, hang_up=False)

        conn, first, fresh, setups = self._connection(answering)
        rc, stdout, _ = conn.exec_command("ufw allow 1")
        self.assertEqual((rc, stdout), (0, b"ok\n"))
        self.assertEqual(setups, [])
        self.assertEqual(first.methods, [("Exec", "ufw allow 1")])
        self.assertEqual(fresh.methods, [])

    def test_failed_setup_reports_outcome_unknown(self) -> None:
        conn, _, _, _ = self._connection(_SilentServer)
        conn._setup_local_socket.side_effect = fastagent_plugin.AnsibleConnectionFailure("no route")
        rc, _, stderr = conn.exec_command("ufw allow 1")
        self.assertEqual(rc, 1)
        self.assertIn(b"execution outcome unknown", stderr)
        self.assertIn(b"no route", stderr)
        self.assertTrue(conn._agent_client.broken)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestRetireStaleForwarder(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(fastagent_plugin.os, "lstat", return_value=types.SimpleNamespace(
            st_mode=stat_module.S_IFSOCK | 0o600, st_uid=os.getuid()))
        patch.start()
        self.addCleanup(patch.stop)

    def _conn(self):
        conn = _bare_connection()
        conn.get_option = lambda key: "ssh" if key == "ssh_executable" else None
        return conn

    def test_cleanup_failure_preserves_socket_and_prevents_replacement(self):
        conn = self._conn()
        with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=True), \
             mock.patch.object(fastagent_plugin.subprocess, "run",
                               side_effect=FileNotFoundError("ssh missing")), \
             mock.patch.object(fastagent_plugin.os, "remove") as remove:
            with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure,
                                        "cannot retire.*preserving"):
                conn._ensure_ssh_forwarding("host", None, None, "/tmp/local.sock", "/tmp/remote.sock")
        remove.assert_not_called()

    def test_stop_drains_connections_without_signalling_processes(self):
        conn = self._conn()
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=True), \
             mock.patch.object(fastagent_plugin.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(fastagent_plugin.os, "kill") as kill:
            conn._retire_stale_forwarder("/tmp/local.sock", "host")
        self.assertEqual(run.call_args.args[0],
                         ["ssh", "-F", "none", "-S", "/tmp/local.sock.ctl", "-O", "stop", "host"])
        kill.assert_not_called()

    def test_nonzero_exit_and_timeout_are_visible(self):
        for outcome in (subprocess.CompletedProcess([], 255, b"", b"control socket refused"),
                        subprocess.TimeoutExpired("ssh", 5)):
            with self.subTest(outcome=outcome):
                conn = self._conn()
                with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=True), \
                     mock.patch.object(fastagent_plugin.subprocess, "run") as run:
                    if isinstance(outcome, Exception):
                        run.side_effect = outcome
                    else:
                        run.return_value = outcome
                    with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "preserving"):
                        conn._retire_stale_forwarder("/tmp/local.sock", "host")

    def test_live_legacy_listener_is_preserved(self):
        conn = self._conn()
        with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=False), \
             mock.patch.object(fastagent_plugin.socket_mod, "socket") as socket:
            with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "unmanaged.*preserving"):
                conn._retire_stale_forwarder("/tmp/local.sock", "host")
        socket.return_value.__enter__.return_value.connect.assert_called_once_with("/tmp/local.sock")

    def test_dead_legacy_socket_and_absent_socket_allow_setup(self):
        for error in (ConnectionRefusedError(), FileNotFoundError()):
            with self.subTest(error=error):
                conn = self._conn()
                with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=False), \
                     mock.patch.object(fastagent_plugin.socket_mod, "socket") as socket:
                    socket.return_value.__enter__.return_value.connect.side_effect = error
                    conn._retire_stale_forwarder("/tmp/local.sock", "host")

    def test_permission_error_is_not_mistaken_for_dead_listener(self):
        conn = self._conn()
        with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=False), \
             mock.patch.object(fastagent_plugin.socket_mod, "socket") as socket:
            socket.return_value.__enter__.return_value.connect.side_effect = PermissionError("denied")
            with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "cannot inspect.*preserving"):
                conn._retire_stale_forwarder("/tmp/local.sock", "host")

    def test_unsafe_control_socket_is_preserved(self):
        for mode, uid in ((stat_module.S_IFLNK | 0o600, os.getuid()),
                          (stat_module.S_IFSOCK | 0o600, os.getuid() + 1),
                          (stat_module.S_IFSOCK | 0o666, os.getuid())):
            with self.subTest(mode=mode, uid=uid):
                conn = self._conn()
                with mock.patch.object(fastagent_plugin.os.path, "lexists", return_value=True), \
                     mock.patch.object(fastagent_plugin.os, "lstat", return_value=types.SimpleNamespace(st_mode=mode, st_uid=uid)), \
                     mock.patch.object(fastagent_plugin.subprocess, "run") as run:
                    with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "unsafe.*preserving"):
                        conn._retire_stale_forwarder("/tmp/local.sock", "host")
                run.assert_not_called()


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestEnsureSshForwardingRetiresStaleForwarder(unittest.TestCase):
    def test_retires_stale_forwarder_before_replacing_socket(self) -> None:
        conn = _bare_connection()
        conn.get_option = lambda key, *a, **kw: {
            "ssh_executable": "ssh",
            "ssh_args": None,
            "private_key": None,
        }.get(key)

        local_socket = "/tmp/fastagent-local-host-root-0.0.0.sock"
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(conn, "_retire_stale_forwarder") as retire, \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True), \
             mock.patch.object(fastagent_plugin.os, "remove") as mock_remove, \
             mock.patch.object(fastagent_plugin.subprocess, "run", return_value=completed):
            conn._ensure_ssh_forwarding(
                "serval", "kevin", None, local_socket,
                "/tmp/fastagent-root-0.0.0.sock")

        retire.assert_called_once_with(local_socket, "serval")
        mock_remove.assert_called_once_with(local_socket)

    def test_tunnel_has_its_own_master_even_with_shared_ssh_args(self):
        conn = _bare_connection()
        conn.get_option = lambda key: {
            "ssh_args": "-o ControlMaster=auto -o ControlPersist=60s -S /tmp/shared",
        }.get(key)
        with mock.patch.object(conn, "_retire_stale_forwarder"), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True), \
             mock.patch.object(fastagent_plugin.os, "remove"), \
             mock.patch.object(fastagent_plugin.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            conn._ensure_ssh_forwarding("host", None, None, "/tmp/local.sock", "/tmp/remote.sock")
        cmd = run.call_args.args[0]
        self.assertLess(cmd.index("ControlMaster=yes"), cmd.index("ControlMaster=auto"))
        self.assertLess(cmd.index("ControlPersist=no"), cmd.index("ControlPersist=60s"))
        self.assertEqual(cmd[len(cmd) - 6:],
                         ["/tmp/local.sock.ctl", "-f", "-N", "-L", "/tmp/local.sock:/tmp/remote.sock", "host"])

    def test_missing_control_socket_is_a_visible_startup_failure(self):
        conn = _bare_connection()
        conn.get_option = lambda key: None
        with mock.patch.object(conn, "_retire_stale_forwarder"), \
             mock.patch.object(fastagent_plugin.os.path, "exists", side_effect=lambda p: not p.endswith(".ctl")), \
             mock.patch.object(fastagent_plugin.os, "remove"), \
             mock.patch.object(fastagent_plugin.time_mod, "sleep"), \
             mock.patch.object(fastagent_plugin.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, b"", b"")):
            with self.assertRaisesRegex(fastagent_plugin.AnsibleConnectionFailure, "control socket did not appear"):
                conn._ensure_ssh_forwarding("host", None, None, "/tmp/local.sock", "/tmp/remote.sock")


@unittest.skipIf(_FASTAGENT_IMPORT_ERROR is not None, "ansible is required")
@unittest.skipUnless(os.environ.get("FASTAGENT_TEST_TUNNEL_HOST"), "set FASTAGENT_TEST_TUNNEL_HOST for real SSH lifecycle test")
class TestSshTunnelLifecycle(unittest.TestCase):
    def test_bootstrap_commands_and_upload_share_one_transport(self):
        host = os.environ["FASTAGENT_TEST_TUNNEL_HOST"]
        conn = _bare_connection()
        conn.get_option = lambda key: {
            "ssh_args": "-o BatchMode=yes -o ControlMaster=auto -o ControlPersist=60s",
        }.get(key)
        connections = []

        def bootstrap(*args, control_socket):
            for _ in range(3):
                rc, stdout, stderr = conn._run_bootstrap_ssh_command(
                    host, None, None, 'printf "%s\\n" "$SSH_CONNECTION"',
                    control_socket=control_socket)
                self.assertEqual(rc, 0, stderr)
                self.assertEqual(len(stdout.split()), 4, stdout)
                connections.append(stdout)
            # Exercise the stdin path the binary upload uses, without
            # creating any remote files or deploying an agent.
            upload = subprocess.run(
                conn._build_bootstrap_ssh_command(host, None, None, "cat",
                                                  control_socket=control_socket),
                input=b"bootstrap upload\n", capture_output=True, timeout=10, check=True)
            self.assertEqual(upload.stdout, b"bootstrap upload\n")

        with tempfile.TemporaryDirectory(prefix="fa-bootstrap-", dir="/tmp") as directory:
            local = directory + "/local.sock"
            try:
                # No daemon exists at this remote path. The tunnel must
                # still start and carry the bootstrap SSH sessions.
                with mock.patch.object(conn, "_try_local_socket", side_effect=[False, True]), \
                     mock.patch.object(conn, "_ensure_remote_daemon", side_effect=bootstrap):
                    conn._setup_local_socket(host, None, None, local,
                                             directory + "/missing.sock", False)
                self.assertEqual(len(set(connections)), 1, connections)
            finally:
                if os.path.exists(local + ".ctl"):
                    conn._retire_stale_forwarder(local, host)

    def test_replacement_drains_active_stream_and_reaps_idle_master(self):
        host = os.environ["FASTAGENT_TEST_TUNNEL_HOST"]
        # This isolated echo server performs no agent RPCs and uses only
        # temporary sockets. Closing stdin shuts it down and removes them.
        server_code = '''
import os, socket, sys, tempfile, threading
def echo(conn):
    with conn:
        while True:
            data = conn.recv(4096)
            if not data:
                return
            conn.sendall(data)
def accept(server):
    while True:
        conn, _ = server.accept()
        threading.Thread(target=echo, args=(conn,), daemon=True).start()
with tempfile.TemporaryDirectory(prefix="fa-echo-", dir="/tmp") as directory:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        path = directory + "/echo.sock"
        server.bind(path)
        server.listen()
        threading.Thread(target=accept, args=(server,), daemon=True).start()
        print(path, flush=True)
        sys.stdin.readline()
'''
        server = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", host, "python3 -u -c " + shlex_quote(server_code)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        conn = _bare_connection()
        conn.get_option = lambda key: {
            # Both the config and these arguments may enable sharing. Our
            # dedicated master must take precedence over either source.
            "ssh_args": "-o BatchMode=yes -o ControlMaster=auto -o ControlPersist=60s -S /tmp/fa-shared",
        }.get(key)
        known_pids = set()
        streams = []

        def echo(stream, message):
            stream.sendall(message)
            response = b""
            while len(response) < len(message):
                part = stream.recv(len(message) - len(response))
                self.assertTrue(part, "echo stream closed early")
                response += part
            self.assertEqual(response, message)

        try:
            self.assertTrue(select.select([server.stdout], [], [], 15)[0], "echo server did not become ready")
            remote = server.stdout.readline().decode().strip()
            self.assertTrue(remote.startswith("/tmp/fa-echo-"), remote)
            with tempfile.TemporaryDirectory(prefix="fa-tunnel-", dir="/tmp") as directory:
                local = directory + "/local.sock"
                try:
                    for cycle in range(3):
                        conn._ensure_ssh_forwarding(host, None, None, local, remote)
                        check = subprocess.run(
                            ["ssh", "-F", "none", "-S", local + ".ctl", "-O", "check", host],
                            capture_output=True, timeout=5, check=True,
                        )
                        match = re.search(rb"pid=(\d+)", check.stderr)
                        self.assertIsNotNone(match, check.stderr)
                        pid = int(match.group(1))
                        self.assertNotIn(pid, known_pids)
                        known_pids.add(pid)
                        if streams:
                            old, old_pid = streams[-1]
                            echo(old, b"still running\n")
                            old.close()
                            deadline = time.monotonic() + 5
                            while True:
                                try:
                                    os.kill(old_pid, 0)
                                except ProcessLookupError:
                                    known_pids.remove(old_pid)
                                    break
                                if time.monotonic() >= deadline:
                                    self.fail(f"retired master {old_pid} did not exit after its stream closed")
                                time.sleep(0.05)
                        stream = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                        streams.append((stream, pid))
                        stream.settimeout(5)
                        stream.connect(local)
                        echo(stream, b"hello\n")
                finally:
                    for stream, _ in streams:
                        stream.close()
                    if os.path.exists(local + ".ctl"):
                        conn._retire_stale_forwarder(local, host)
        finally:
            # These PIDs came from this test's private control socket.
            # Clean up even if an assertion failed partway through setup.
            for pid in known_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            stdout, stderr = server.communicate(input=b"\n", timeout=10)
            self.assertEqual(server.returncode, 0, stderr.decode())


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestLocalSocketSetupLock(unittest.TestCase):
    """Regression tests for the controller-side setup race.

    Production, 2026-09-23: a playbook ran a task on two hosts in parallel
    forks, each with `delegate_to: columbus-buckeye`. Both worker processes
    computed the same identity digest (commit 5388172) and so targeted the
    same local_socket. Both saw _try_local_socket() fail and both raced
    `ssh -L` to bind it; the loser's ssh got "Address already in use" and
    Ansible marked the host UNREACHABLE. Commit 5388172's advisory lock
    only covers the *remote* daemon's own startup (RunDaemon's flock in
    daemon.go) — it does nothing for this controller-side bind race, since
    two controller processes never contend for that lock at all.

    _setup_local_socket now serializes controller-side setup for one
    local_socket with a sibling advisory lock file, and re-checks
    _try_local_socket() after acquiring it so the loser reuses the
    winner's connection instead of racing ssh a second time.
    """

    def test_concurrent_setup_serializes_and_reuses_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-test-") as directory:
            local_socket = os.path.join(directory, "fastagent-local-test.sock")
            remote_socket = "/tmp/fastagent-test.sock"

            usable = threading.Event()
            forwarding_calls: list[str] = []
            forwarding_calls_lock = threading.Lock()
            start_gate = threading.Barrier(2, timeout=5)

            def fake_ensure_ssh_forwarding(host, user, port, ls, rs):
                with forwarding_calls_lock:
                    forwarding_calls.append(threading.current_thread().name)
                # Simulate the time `ssh -L` takes to authenticate and bind
                # before the local socket file becomes connectable — the
                # window the production race fell into.
                time.sleep(0.3)
                usable.set()

            def fake_try_local_socket(ls, host):
                return usable.is_set()

            results: dict[str, bool] = {}
            errors: list[BaseException] = []

            def worker(name: str) -> None:
                conn = _bare_connection()
                conn.get_option = lambda key, *a, **kw: None
                try:
                    start_gate.wait()
                    with mock.patch.object(
                            conn, "_try_local_socket",
                            side_effect=fake_try_local_socket), \
                         mock.patch.object(conn, "_ensure_remote_daemon"), \
                         mock.patch.object(
                            conn, "_ensure_ssh_forwarding",
                            side_effect=fake_ensure_ssh_forwarding):
                        # _setup_local_socket has no return value on
                        # success (it raises AnsibleConnectionFailure on
                        # failure instead), so simply completing without
                        # raising is the success signal.
                        conn._setup_local_socket(
                            "test-host", "kevin", None,
                            local_socket, remote_socket, False,
                        )
                        results[name] = True
                except BaseException as e:  # noqa: BLE001 - surfaced below
                    errors.append(e)

            fork_a = threading.Thread(target=worker, args=("fork-a",), name="fork-a")
            fork_b = threading.Thread(target=worker, args=("fork-b",), name="fork-b")
            fork_a.start()
            fork_b.start()
            fork_a.join(timeout=5)
            fork_b.join(timeout=5)

            self.assertFalse(errors, f"worker(s) raised: {errors!r}")
            self.assertTrue(results.get("fork-a"))
            self.assertTrue(results.get("fork-b"))
            # Whichever fork gets there first, only ONE of them may ever
            # call _ensure_ssh_forwarding (i.e. race `ssh -L`). The other
            # must block on the setup lock and then reuse the winner's
            # now-usable socket via the post-lock _try_local_socket
            # re-check, instead of racing a second bind.
            self.assertEqual(
                len(forwarding_calls), 1,
                f"exactly one fork should set up forwarding, got "
                f"{forwarding_calls!r}",
            )

    def test_lock_file_is_created_with_safe_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-test-") as directory:
            local_socket = os.path.join(directory, "fastagent-local-test.sock")
            conn = _bare_connection()
            with conn._local_socket_setup_lock(local_socket, "test-host"):
                pass
            lock_path = local_socket + ".lock"
            st = os.stat(lock_path)
            self.assertEqual(stat_module.S_IMODE(st.st_mode), 0o600)
            self.assertEqual(st.st_uid, os.getuid())

    def test_lock_wait_times_out_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-test-") as directory:
            local_socket = os.path.join(directory, "fastagent-local-test.sock")
            lock_path = local_socket + ".lock"
            holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(holder_fd, fcntl.LOCK_EX)
                conn = _bare_connection()
                with mock.patch.object(
                        fastagent_plugin, "_LOCAL_SOCKET_LOCK_TIMEOUT", 0.2), \
                     mock.patch.object(
                        fastagent_plugin, "_LOCAL_SOCKET_LOCK_POLL", 0.05):
                    with self.assertRaises(
                            fastagent_plugin.AnsibleConnectionFailure) as ctx:
                        with conn._local_socket_setup_lock(local_socket, "test-host"):
                            pass  # pragma: no cover - must not be reached
                self.assertIn("timed out", str(ctx.exception).lower())
                self.assertIn(lock_path, str(ctx.exception))
            finally:
                fcntl.flock(holder_fd, fcntl.LOCK_UN)
                os.close(holder_fd)

    def test_rejects_group_writable_lock_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-test-") as directory:
            lock_path = os.path.join(directory, "fastagent-local-test.sock.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            os.close(fd)
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure):
                fastagent_plugin._open_local_socket_lock(lock_path)

    def test_rejects_symlink_lock_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-test-") as directory:
            target = os.path.join(directory, "real-file")
            with open(target, "wb"):
                pass
            lock_path = os.path.join(directory, "fastagent-local-test.sock.lock")
            os.symlink(target, lock_path)
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure):
                fastagent_plugin._open_local_socket_lock(lock_path)

    def test_rejects_lock_file_owned_by_another_user(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-test-") as directory:
            lock_path = os.path.join(directory, "fastagent-local-test.sock.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            # Simulate "owned by someone else" by making the current uid
            # check disagree with the file's real (our own) uid, since the
            # test process cannot actually chown a file to another user.
            with mock.patch.object(
                    fastagent_plugin.os, "getuid",
                    return_value=os.getuid() + 1):
                with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure):
                    fastagent_plugin._open_local_socket_lock(lock_path)


@unittest.skipIf(
    _FASTAGENT_IMPORT_ERROR is not None,
    "ansible is required to run connection plugin tests",
)
class TestEnsureSshForwardingBindRace(unittest.TestCase):
    """Defense in depth: even with the setup lock, retry the local socket
    probe a few times if `ssh -L` fails to bind with "Address already in
    use" before failing the task outright. Covers a leftover forwarder
    started outside the lock (e.g. by an older fastagent build) or a lock
    holder that only just released.
    """

    def _conn(self):
        conn = _bare_connection()
        conn.get_option = lambda key, *a, **kw: {
            "ssh_executable": "ssh",
            "ssh_args": None,
            "private_key": None,
        }.get(key)
        return conn

    def test_address_in_use_retries_before_failing(self) -> None:
        conn = self._conn()
        local_socket = "/tmp/fastagent-local-host-root-0.0.0.sock"
        failed = subprocess.CompletedProcess(
            args=["ssh"], returncode=255, stdout=b"",
            stderr=b"unix_listener: cannot bind to path "
                   b"/tmp/fastagent-local-host-root-0.0.0.sock: "
                   b"Address already in use\n",
        )

        probe_results = iter([False, True])
        with mock.patch.object(conn, "_retire_stale_forwarder"), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True), \
             mock.patch.object(fastagent_plugin.os, "remove"), \
             mock.patch.object(fastagent_plugin.subprocess, "run", return_value=failed), \
             mock.patch.object(
                conn, "_try_local_socket",
                side_effect=lambda *a, **kw: next(probe_results)) as probe, \
             mock.patch.object(fastagent_plugin.time_mod, "sleep") as sleep:
            conn._ensure_ssh_forwarding(
                "serval", "kevin", None, local_socket,
                "/tmp/fastagent-root-0.0.0.sock",
            )

        self.assertEqual(probe.call_count, 2)
        self.assertGreaterEqual(sleep.call_count, 1)

    def test_address_in_use_raises_if_never_becomes_usable(self) -> None:
        conn = self._conn()
        local_socket = "/tmp/fastagent-local-host-root-0.0.0.sock"
        failed = subprocess.CompletedProcess(
            args=["ssh"], returncode=255, stdout=b"",
            stderr=b"Address already in use\n",
        )

        with mock.patch.object(conn, "_retire_stale_forwarder"), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True), \
             mock.patch.object(fastagent_plugin.os, "remove"), \
             mock.patch.object(fastagent_plugin.subprocess, "run", return_value=failed), \
             mock.patch.object(conn, "_try_local_socket", return_value=False), \
             mock.patch.object(fastagent_plugin.time_mod, "sleep"):
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure):
                conn._ensure_ssh_forwarding(
                    "serval", "kevin", None, local_socket,
                    "/tmp/fastagent-root-0.0.0.sock",
                )

    def test_other_failures_still_raise_immediately(self) -> None:
        conn = self._conn()
        local_socket = "/tmp/fastagent-local-host-root-0.0.0.sock"
        failed = subprocess.CompletedProcess(
            args=["ssh"], returncode=255, stdout=b"",
            stderr=b"Permission denied (publickey).\n",
        )

        with mock.patch.object(conn, "_retire_stale_forwarder"), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=True), \
             mock.patch.object(fastagent_plugin.os, "remove"), \
             mock.patch.object(fastagent_plugin.subprocess, "run", return_value=failed), \
             mock.patch.object(conn, "_try_local_socket") as probe:
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure):
                conn._ensure_ssh_forwarding(
                    "serval", "kevin", None, local_socket,
                    "/tmp/fastagent-root-0.0.0.sock",
                )
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()


_DF_HEADER_K = "Filesystem     1024-blocks    Used Available Capacity Mounted on"
_DF_HEADER_I = "Filesystem      Inodes  IUsed   IFree IUse% Mounted on"


def _diag_stderr(df_k: str, df_i: str, log: str = "", writable: bool = True) -> str:
    m = "==> fastagent-diag "
    lines = ["timeout waiting for socket"]
    if not writable:
        lines.append(m + "not-writable")
    lines += [m + "df-k", _DF_HEADER_K, df_k, m + "df-i", _DF_HEADER_I, df_i,
              m + "log", log]
    return "\n".join(lines) + "\n"


@unittest.skipIf(_FASTAGENT_IMPORT_ERROR is not None, "ansible is not installed")
class TestRemoteFilesystemDiagnostics(unittest.TestCase):
    """A full remote /tmp used to fail as a bare "timeout waiting for
    socket": the daemon's own error went to a log on the same full
    filesystem (or to /dev/null under sudo). The start script now prints
    df output and the log tail on failure, and the plugin names the cause.
    """

    def test_full_blocks(self) -> None:
        stderr = _diag_stderr(
            "tmpfs              1048576 1048576         0     100% /tmp",
            "tmpfs               262144     312  261832    1% /tmp",
        )
        hint = fastagent_plugin._remote_fs_hint("/tmp", stderr)
        self.assertIsNotNone(hint)
        self.assertIn("remote /tmp is full (0 KiB available, 100% used)", hint)

    def test_exhausted_inodes(self) -> None:
        stderr = _diag_stderr(
            "/dev/sda1         20511312 9000000  10444720      47% /tmp",
            "/dev/sda1          1310720 1310720       0  100% /tmp",
        )
        hint = fastagent_plugin._remote_fs_hint("/tmp", stderr)
        self.assertIsNotNone(hint)
        self.assertIn("has no free inodes (100% used)", hint)

    def test_not_writable(self) -> None:
        stderr = _diag_stderr(
            "/dev/sda1         20511312 9000000  10444720      47% /tmp",
            "/dev/sda1          1310720  20000 1290720    2% /tmp",
            writable=False,
        )
        self.assertIn("is not writable",
                      fastagent_plugin._remote_fs_hint("/tmp", stderr))

    def test_log_mentions_enospc(self) -> None:
        # e.g. a quota: df shows plenty of room, but the daemon could not write.
        stderr = _diag_stderr(
            "/dev/sda1         20511312 9000000  10444720      47% /tmp",
            "/dev/sda1          1310720  20000 1290720    2% /tmp",
            log="write daemon pid file: open /tmp/x.pid: disk quota exceeded",
        )
        self.assertIn("cannot accept new files",
                      fastagent_plugin._remote_fs_hint("/tmp", stderr))

    def test_healthy_filesystem_gives_no_hint(self) -> None:
        # btrfs reports 0 total inodes and "-" usage; not an exhaustion.
        stderr = _diag_stderr(
            "/dev/nvme0n1p2    20511312 9000000  10444720      47% /tmp",
            "/dev/nvme0n1p2           0       0        0     - /tmp",
            log="daemon started",
        )
        self.assertIsNone(fastagent_plugin._remote_fs_hint("/tmp", stderr))

    def test_extra_columns_and_spaces_in_filesystem_name(self) -> None:
        # coreutils df on macOS prints two Capacity columns; a network
        # filesystem name may contain spaces.
        m = "==> fastagent-diag "
        stderr = "\n".join([
            m + "df-k",
            "Filesystem     1024-blocks  Used Available Capacity Capacity Mounted on",
            "//nas/tmp share       2008  2008         0        -     100% /tmp",
            m + "df-i",
            "Filesystem         Inodes IUsed      IFree IUse% Mounted on",
            "//nas/tmp share      1000   400        600   40% /tmp",
        ])
        hint = fastagent_plugin._remote_fs_hint("/tmp", stderr)
        self.assertIn("is full (0 KiB available, 100% used)", hint)
        self.assertNotIn("inodes", hint)

    def test_busybox_inode_layout(self) -> None:
        m = "==> fastagent-diag "
        stderr = "\n".join([
            m + "df-k",
            "Filesystem           1024-blocks    Used Available Capacity Mounted on",
            "tmpfs                    65536     120     65416   0% /tmp",
            m + "df-i",
            "Filesystem              Inodes      Used Available Use% Mounted on",
            "tmpfs                     1024      1024         0 100% /tmp",
        ])
        self.assertIn("has no free inodes (100% used)",
                      fastagent_plugin._remote_fs_hint("/tmp", stderr))

    def test_output_without_diagnostics_gives_no_hint(self) -> None:
        self.assertIsNone(fastagent_plugin._remote_fs_hint(
            "/tmp", "sudo: a password is required\n"))

    def test_diag_command_runs_and_parses_on_this_host(self) -> None:
        # Guard the shell syntax and the df parsing against a real df.
        with tempfile.TemporaryDirectory(prefix="fastagent-diag-") as d:
            log = os.path.join(d, "agent.sock.log")
            with open(log, "w") as f:
                f.write("daemon started\n")
            cmd = (f"echo 'timeout waiting for socket' >&2; "
                   f"{fastagent_plugin._remote_fs_diag_cmd(d, log)} exit 1")
            result = subprocess.run(["sh", "-c", cmd], capture_output=True,
                                    text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        sections = result.stderr.split("==> fastagent-diag ")
        df_k = sections[1].splitlines()[1:]
        parsed = fastagent_plugin._parse_df(df_k, ("1024-blocks",), ("Available",))
        self.assertIsNotNone(parsed, result.stderr)
        self.assertTrue(parsed[1] is not None and parsed[1].isdigit(), result.stderr)
        self.assertIsNone(fastagent_plugin._remote_fs_hint(d, result.stderr))
        self.assertIn("daemon started", result.stderr)

    def test_start_failure_reports_full_tmp(self) -> None:
        conn = TestEnsureRemoteDaemon._conn(self)
        remote_socket = f"/tmp/fastagent-root-{fastagent_plugin.AGENT_VERSION}.sock"
        stderr = _diag_stderr(
            "tmpfs              1048576 1048576         0     100% /tmp",
            "tmpfs               262144     312  261832    1% /tmp",
        )
        commands = []

        def run_ssh(host, user, port, command, *, control_socket):
            commands.append(command)
            if "--daemon" in command:
                return 1, "", stderr
            return 1, "", ""

        with mock.patch.object(conn, "_run_bootstrap_ssh_command", side_effect=run_ssh), \
             mock.patch.object(conn, "_detect_remote_arch", return_value="amd64"), \
             mock.patch.object(conn, "_ensure_agent_deployed"):
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure) as ctx:
                conn._ensure_remote_daemon("serval", "deploy", None, remote_socket, True,
                                           control_socket="/tmp/test-bootstrap.ctl")

        msg = str(ctx.exception)
        self.assertIn("failed to start daemon on serval: remote /tmp is full", msg)
        # Raw output is kept for anything the summary does not cover.
        self.assertIn("timeout waiting for socket", msg)
        self.assertIn(f"tail -n 20 {remote_socket}.log", commands[-1])


@unittest.skipIf(_FASTAGENT_IMPORT_ERROR is not None, "ansible is not installed")
class TestLocalFilesystemDiagnostics(unittest.TestCase):
    def test_lock_open_enospc_reports_free_space(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-lock-") as d:
            path = os.path.join(d, "local.sock.lock")
            err = OSError(fastagent_plugin.errno.ENOSPC, "No space left on device")
            with mock.patch.object(fastagent_plugin.os, "open", side_effect=err):
                with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure) as ctx:
                    fastagent_plugin._open_local_socket_lock(path)
        self.assertIn(f"Local {d} has", str(ctx.exception))
        self.assertIn("inodes available", str(ctx.exception))

    def test_lock_open_other_error_has_no_hint(self) -> None:
        err = OSError(fastagent_plugin.errno.EACCES, "Permission denied")
        with mock.patch.object(fastagent_plugin.os, "open", side_effect=err):
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure) as ctx:
                fastagent_plugin._open_local_socket_lock("/nonexistent/x.lock")
        self.assertNotIn("inodes available", str(ctx.exception))

    def test_forwarding_enospc_reports_free_space(self) -> None:
        conn = TestEnsureSshForwardingBindRace._conn(self)
        local_socket = "/tmp/fastagent-local-host-root-0.0.0.sock"
        failed = subprocess.CompletedProcess(
            args=["ssh"], returncode=255, stdout=b"",
            stderr=b"unix_listener: cannot bind to path "
                   b"/tmp/fastagent-local-host-root-0.0.0.sock: "
                   b"No space left on device\n",
        )
        with mock.patch.object(conn, "_retire_stale_forwarder"), \
             mock.patch.object(fastagent_plugin.os.path, "exists", return_value=False), \
             mock.patch.object(fastagent_plugin.subprocess, "run", return_value=failed):
            with self.assertRaises(fastagent_plugin.AnsibleConnectionFailure) as ctx:
                conn._ensure_ssh_forwarding(
                    "serval", "kevin", None, local_socket,
                    "/tmp/fastagent-root-0.0.0.sock",
                )
        self.assertIn("Local /tmp has", str(ctx.exception))
