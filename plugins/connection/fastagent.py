"""fastagent connection plugin for Ansible.

Bootstraps and communicates with a persistent Go agent on the remote host
via SSH. The agent speaks newline-delimited JSON-RPC over stdio.

Usage in inventory:
    ansible_connection: fastagent

Or via ansible.cfg / CLI:
    -c fastagent
"""

from __future__ import annotations

DOCUMENTATION = r"""
name: fastagent
short_description: Connect via a persistent Go agent over SSH
description:
    - This connection plugin bootstraps a Go agent binary on the remote host
      via SSH and communicates with it using JSON-RPC over stdio.
    - It preserves existing SSH authentication (keys, agent forwarding, config).
author: Kevin Burke
version_added: "0.1.0"
options:
    host:
        description: Hostname/IP to connect to
        default: inventory_hostname
        vars:
            - name: inventory_hostname
            - name: ansible_host
            - name: ansible_ssh_host
    port:
        description: SSH port
        type: int
        default: 22
        ini:
            - section: defaults
              key: remote_port
        env:
            - name: ANSIBLE_REMOTE_PORT
        vars:
            - name: ansible_port
            - name: ansible_ssh_port
    remote_user:
        description: User to log in as
        ini:
            - section: defaults
              key: remote_user
        env:
            - name: ANSIBLE_REMOTE_USER
        vars:
            - name: ansible_user
            - name: ansible_ssh_user
    ssh_executable:
        description: SSH executable to use
        default: ssh
        ini:
            - section: ssh_connection
              key: ssh_executable
        env:
            - name: ANSIBLE_SSH_EXECUTABLE
        vars:
            - name: ansible_ssh_executable
    scp_executable:
        description: SCP executable to use for uploading the agent binary
        default: scp
        ini:
            - section: ssh_connection
              key: scp_executable
        vars:
            - name: ansible_scp_executable
    ssh_args:
        description: Extra SSH arguments
        default: ""
        ini:
            - section: ssh_connection
              key: ssh_args
        env:
            - name: ANSIBLE_SSH_ARGS
        vars:
            - name: ansible_ssh_extra_args
    agent_path:
        description: >
            Path on the remote host where the agent binary is stored.
            The string {version} is replaced with the agent version,
            {os} with the target OS, and {arch} with the target architecture.
            The daemon runs as the SSH user (direct) or as root (when
            become is enabled), so the binary can always live in the
            connecting user's home — the become_user never needs to exec
            it itself.
        default: "~/.ansible/fastagent/fastagent-{version}-{os}-{arch}"
        vars:
            - name: fastagent_agent_path
    local_agent_dir:
        description: >
            Local directory containing pre-built agent binaries.
            Expected layout: fastagent-linux-amd64, fastagent-linux-arm64, etc.
        vars:
            - name: fastagent_local_agent_dir
    download_url:
        description: >
            URL template used to download the agent binary when it is not
            already present locally. The tokens {version} and {arch} are
            substituted with the agent version and target architecture
            (amd64 or arm64). Set to an empty string to disable auto-download.
        default: "https://github.com/kevinburke/ansible-go/releases/download/v{version}/fastagent-{version}-linux-{arch}"
        env:
            - name: FASTAGENT_DOWNLOAD_URL
        vars:
            - name: fastagent_download_url
    private_key:
        description: SSH private key file
        ini:
            - section: defaults
              key: private_key_file
        env:
            - name: ANSIBLE_PRIVATE_KEY_FILE
        vars:
            - name: ansible_ssh_private_key_file
            - name: ansible_private_key_file
    pipelining:
        description: >
            Whether to send module contents to the remote interpreter via
            stdin instead of writing a temp file first. When enabled,
            non-overridden modules avoid an extra put_file round trip.
            Has no effect on action-plugin overrides (apt, copy, file, etc.),
            which already bypass module execution entirely.
        type: bool
        default: false
        ini:
            - section: defaults
              key: pipelining
            - section: connection
              key: pipelining
            - section: ssh_connection
              key: pipelining
        env:
            - name: ANSIBLE_PIPELINING
            - name: ANSIBLE_SSH_PIPELINING
            - name: FASTAGENT_PIPELINING
        vars:
            - name: ansible_pipelining
            - name: ansible_ssh_pipelining
"""

import base64
import os
import shlex
import socket as socket_mod
import subprocess
import tempfile
import time as time_mod
import typing as t
from ansible.errors import AnsibleConnectionFailure, AnsibleFileNotFound
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.kevinburke.fastagent.plugins.module_utils.fastagent_client import (
    FastAgentClient,
    FastAgentError,
)

display = Display()

# Agent version must match the Go constant.
AGENT_VERSION = "0.6.6"

class Connection(ConnectionBase):
    """fastagent connection plugin."""

    transport = "fastagent"
    # Capability flag — actual enablement is controlled by the `pipelining`
    # option (read by Ansible into play_context.pipelining at task time).
    has_pipelining = True
    supports_persistence = False

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self._socket: socket_mod.socket | None = None
        self._agent_client: FastAgentClient | None = None
        # Set per-task in set_become_plugin(): True if the task is run
        # with become. The actual target user is read from
        # self._play_context.become_user at exec_command time — see
        # the comment on set_become_plugin for why the plugin itself
        # isn't a reliable source here.
        self._use_become: bool = False

    def set_become_plugin(self, plugin) -> None:
        """Swallow Ansible's become plugin so it doesn't wrap commands.

        Ansible's default `set_become_plugin` attaches the become plugin
        to `self.become`, and `ActionBase._low_level_execute_command`
        wraps every module invocation with `sudo -u <user> sh -c …`
        whenever `self._connection.become` is truthy. That wrap runs
        inside our agent's exec context — which is already root — and
        tries to drop to the target user. Fine for sudoers, but for a
        dedicated app user that isn't in `/etc/sudoers` (e.g. `returns`)
        it fails with `<user> is not in the sudoers file`.

        Fastagent handles become itself: the connection passes
        `become_user` to the Exec RPC, and the agent wraps with sudo at
        dispatch (running as root, so sudoers policy never applies).
        To suppress Ansible's redundant wrap, we leave `self.become` as
        None regardless of what Ansible hands us, and read the target
        user from `self._play_context.become_user` at exec_command time.

        Why not read `become_user` off the plugin here? TaskExecutor
        only populates `plugin.get_option('become_user')` via
        `_set_plugin_options('become', ...)` when `connection.become is
        not None` (task_executor.py:1085). Leaving `self.become = None`
        is exactly what suppresses the ActionBase wrap — but it also
        skips that populate step, so the plugin returns the *default*
        become_user ("root") rather than the task's templated value.
        Reading from play_context sidesteps this: ansible templates
        `play_context.become_user` from the task (play_context.py:189)
        before handing the play_context to the connection.

        Called once per task by ansible-core's TaskExecutor before the
        action plugin runs.
        """
        if plugin is None:
            self._use_become = False
            return
        if plugin.name != "sudo":
            # Unrecognized become method — let ansible handle it the
            # normal way. Agent-side sudo wrap only covers `sudo`.
            display.warning(
                f"fastagent: unsupported become_method {plugin.name!r}, "
                f"falling back to ansible's own become wrap"
            )
            self.become = plugin
            self._use_become = False
            return
        self._use_become = True
        # Deliberately do NOT set self.become — that's what suppresses
        # the ActionBase wrap.

    def get_become_user(self) -> str | None:
        """Return the task's effective non-root become_user, or None.

        Single source of truth for "which user should the agent's Exec
        RPC run this task as?" Used by exec_command and by action plugin
        overrides (command, copy, file, stat) that talk to the agent
        directly and so can't rely on the exec_command code path.

        Returns None when become is off, or when become_user is root
        (the daemon already runs as root under become, so sudo -u root
        would be a no-op fork+exec per RPC).
        """
        if not self._use_become:
            return None
        become_user = self._play_context.become_user or "root"
        if become_user == "root":
            return None
        return become_user

    def _connect(self) -> Connection:
        if self._connected:
            return self

        use_become = self._use_become

        host = self.get_option("host")
        user = self.get_option("remote_user")
        port = self.get_option("port")


        # Socket paths. When become is enabled the daemon always runs as
        # root (it re-executes commands via ansible's own `sudo -u X`
        # wrapper), so one socket serves every become_user on the host.
        # The local socket name must still differ between the become and
        # non-become cases so a stale forwarding session from one mode
        # cannot be reused by the other (they point at different remote
        # sockets owned by different uids).
        #
        # The version is embedded in the socket path so a controller on
        # version X never connects to a daemon on version Y. Without
        # this, Go's JSON decoder silently drops unknown fields, so an
        # older daemon would accept RPCs for new features (e.g. become
        # handling added in 0.5.5) and run them as if the new fields
        # hadn't been set. A per-version path makes `test -S <sock>`
        # an accurate proxy for "correct-version daemon is running".
        if use_become:
            remote_socket = f"/tmp/fastagent-root-{AGENT_VERSION}.sock"
            local_socket = f"/tmp/fastagent-local-{host}-root-{AGENT_VERSION}.sock"
        else:
            remote_socket = f"/tmp/fastagent-{AGENT_VERSION}.sock"
            local_socket = f"/tmp/fastagent-local-{host}-{AGENT_VERSION}.sock"

        # Fast path: try connecting to the local forwarding socket directly.
        # This is a local Unix socket connect (~1ms), no SSH involved.
        if self._try_local_socket(local_socket, host):
            return self

        display.vvv(f"FASTAGENT: local socket not available, setting up", host=host)

        # Ensure the remote daemon is running.
        self._ensure_remote_daemon(host, user, port, remote_socket, use_become)

        # Start SSH socket forwarding if not already running.
        self._ensure_ssh_forwarding(host, user, port, local_socket, remote_socket)

        # Now connect to the local socket.
        if not self._try_local_socket(local_socket, host):
            raise AnsibleConnectionFailure(
                f"fastagent: failed to connect to local forwarding socket {local_socket}"
            )

        return self

    def _try_local_socket(self, local_socket: str, host: str) -> bool:
        """Try connecting to the local forwarding socket and probe the daemon.

        A stale SSH -L forwarder pointing at a dead remote socket will accept
        local connects but the first RPC read will see EOF. So in addition to
        connecting, we send a Hello and only declare success once we get a
        valid response back with a matching version. If any step fails
        (including version mismatch) we tear down the socket so the caller
        falls through to the bootstrap path, which kills any stale daemon
        and starts a fresh one at the right version.
        """
        if not os.path.exists(local_socket):
            return False

        sock = None
        try:
            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            # Short timeout for connect + handshake so a stale socket/tunnel
            # can't hang us. Clear it after the probe so subsequent RPC reads
            # block as long as the remote work takes (e.g. ufw reloading
            # iptables).
            sock.settimeout(2)
            sock.connect(local_socket)
            client = FastAgentClient(sock.makefile("wb"), sock.makefile("rb"))
            client.hello(AGENT_VERSION)
            sock.settimeout(None)
            display.vvv(f"FASTAGENT: connected via local socket", host=host)
            self._socket = sock
            self._agent_client = client
            self._connected = True
            return True
        except Exception as e:
            display.vvv(f"FASTAGENT: local socket probe failed: {e}", host=host)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            return False

    def _ensure_remote_daemon(
        self,
        host: str,
        user: str | None,
        port: int | None,
        remote_socket: str,
        use_become: bool,
    ) -> None:
        """Ensure the remote daemon is running, bootstrapping if needed."""
        # Check if daemon is already running by testing the remote socket.
        rc, _, _ = self._run_ssh_command(
            host, user, port, f"test -S {shlex.quote(remote_socket)}"
        )
        if rc == 0:
            display.vvv(f"FASTAGENT: remote daemon already running", host=host)
            return

        display.vvv(f"FASTAGENT: bootstrapping remote daemon", host=host)

        # Detect arch and deploy binary. The daemon runs as root when
        # become is enabled, so it can always exec the binary out of the
        # connecting user's home — no need to stage to a system path.
        remote_arch = self._detect_remote_arch(host, user, port)
        agent_path_template = self.get_option("agent_path")
        remote_agent_path = agent_path_template.format(
            version=AGENT_VERSION, os="linux", arch=remote_arch,
        )
        if remote_agent_path.startswith("~/"):
            rc, home, _ = self._run_ssh_command(host, user, port, "echo $HOME")
            if rc == 0 and home.strip():
                remote_agent_path = home.strip() + remote_agent_path[1:]

        self._ensure_agent_deployed(
            host, user, port, remote_agent_path, remote_arch,
        )

        agent_bin = shlex.quote(remote_agent_path)

        # Kill any old daemon. Prefer the PID file (the daemon writes it at
        # {socket}.pid); `pkill -F` parses it as an integer, so a garbage or
        # adversarial pid file can't trick us into signalling arbitrary PIDs
        # the way `kill $(cat …)` would. Fall back to pkill with a pattern
        # that matches the versioned binary name (fastagent-X.Y.Z-OS-ARCH) —
        # a literal 'fastagent --daemon' pattern never matches since the
        # cmdline has no space between 'fastagent' and the version suffix.
        pid_file = remote_socket + ".pid"
        kill_cmd = (
            f"pkill -F {shlex.quote(pid_file)} 2>/dev/null || true;"
            f" pkill -f 'fastagent[^ ]* --daemon' 2>/dev/null || true;"
            f" rm -f {shlex.quote(remote_socket)} {shlex.quote(pid_file)}"
        )
        if use_become:
            kill_cmd = f"sudo sh -c {shlex.quote(kill_cmd)}"
        self._run_ssh_command(host, user, port, kill_cmd)

        # Start the daemon. Pass --allow-user so the socket is accessible to
        # the SSH user (needed for SSH socket forwarding).
        debug_flag = " --debug" if display.verbosity >= 3 else ""
        allow_flag = f" --allow-user {shlex.quote(user)}" if user else ""
        daemon_cmd = f"{agent_bin} --daemon --socket {shlex.quote(remote_socket)}{allow_flag}{debug_flag}"
        if use_become:
            # Daemon runs as root so a single instance can sudo to any
            # become_user the play requests (including non-sudoers, where
            # launching the daemon as that user would produce recursive
            # sudo-not-in-sudoers failures when ansible's own `sudo -u`
            # wrapper arrives).
            daemon_cmd = f"sudo {daemon_cmd}"

        log_path = remote_socket + ".log"
        start_cmd = (
            f"setsid {daemon_cmd} </dev/null >>{shlex.quote(log_path)} 2>&1 &"
            f" for i in 1 2 3 4 5; do"
            f"   test -S {shlex.quote(remote_socket)} && exit 0;"
            f"   sleep 0.1;"
            f" done;"
            f" echo 'timeout waiting for socket' >&2; exit 1"
        )
        rc, stdout, stderr = self._run_ssh_command(host, user, port, start_cmd)
        if rc != 0:
            raise AnsibleConnectionFailure(
                f"fastagent: failed to start daemon: rc={rc}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        display.vvv(f"FASTAGENT: daemon started at {remote_socket}", host=host)

    def _ensure_ssh_forwarding(
        self,
        host: str,
        user: str | None,
        port: int | None,
        local_socket: str,
        remote_socket: str,
    ) -> None:
        """Start a background SSH session that forwards local_socket to remote_socket."""
        # Clean up stale local socket.
        if os.path.exists(local_socket):
            os.remove(local_socket)

        ssh_executable = self.get_option("ssh_executable") or "ssh"
        cmd = [ssh_executable]

        ssh_args = self.get_option("ssh_args")
        if ssh_args:
            cmd.extend(shlex.split(ssh_args))

        private_key = self.get_option("private_key")
        if private_key:
            cmd.extend(["-o", f"IdentityFile={private_key}"])

        if user:
            cmd.extend(["-o", f"User={user}"])
        if port:
            cmd.extend(["-o", f"Port={port}"])

        # -f: go to background after authentication
        # -N: no remote command
        # -L: forward local Unix socket to remote Unix socket
        cmd.extend([
            "-f", "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"{local_socket}:{remote_socket}",
            host,
        ])

        display.vvv(f"FASTAGENT: starting SSH forwarding: {' '.join(cmd)}", host=host)

        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise AnsibleConnectionFailure(
                f"fastagent: SSH forwarding failed: "
                f"{result.stderr.decode('utf-8', errors='replace')}"
            )

        # Wait for the local socket to appear.
        for _ in range(10):
            if os.path.exists(local_socket):
                display.vvv(f"FASTAGENT: SSH forwarding established", host=host)
                return
            time_mod.sleep(0.05)

        raise AnsibleConnectionFailure(
            f"fastagent: SSH forwarding socket {local_socket} did not appear"
        )

    def exec_command(
        self,
        cmd: str,
        in_data: bytes | None = None,
        sudoable: bool = True,
    ) -> tuple[int, bytes, bytes]:
        super().exec_command(cmd, in_data=in_data, sudoable=sudoable)

        # Become is handled by the agent: when set_become_plugin
        # recorded that the task is using become, we ask the agent to
        # run the command as the task's become_user via the Exec RPC's
        # become_user field. Ansible's own ActionBase wrap is
        # suppressed by our set_become_plugin override (which leaves
        # self.become as None), so `cmd` here is the raw module
        # invocation with no `sudo -u X` prefix.
        #
        # become_user is read from self._play_context.become_user,
        # which ansible templates from the task before handing the
        # play_context to the connection; the become plugin itself
        # is not a reliable source here (see set_become_plugin for
        # the full explanation).
        #
        # become_user=root is treated as "no wrap needed" because the
        # daemon already runs as root when become is in effect;
        # wrapping with `sudo -u root` would be a no-op fork+exec per
        # RPC.
        #
        # sudoable=False is Ansible's hint that a particular command is
        # connection-layer plumbing (e.g. making an ~/.ansible/tmp dir)
        # and should run as the ssh user rather than the become target,
        # so files in the ssh user's home don't end up root-owned.
        become_user = self.get_become_user()
        if become_user is not None and not sudoable:
            become_user = self.get_option("remote_user") or None

        stdin_data = None
        if in_data is not None:
            stdin_data = in_data.decode("utf-8", errors="surrogateescape")

        try:
            result = self._agent_client.exec(
                cmd_string=cmd,
                use_shell=True,
                stdin=stdin_data,
                # Pass in_data through verbatim. The agent's default of
                # appending a newline is a convenience for direct RPC callers;
                # for connection-plugin traffic (especially pipelined module
                # payloads) we must not mutate the bytes Ansible handed us.
                stdin_add_newline=False,
                become_user=become_user,
            )
        except IOError as e:
            return (1, b"", to_bytes(f"fastagent exec_command failed: {e}"))
        except FastAgentError as e:
            return (1, b"", to_bytes(str(e)))

        stdout = to_bytes(result.get("stdout", ""))
        stderr = to_bytes(result.get("stderr", ""))
        rc = result.get("rc", 0)
        return (rc, stdout, stderr)

    def put_file(self, in_path: str, out_path: str) -> None:
        super().put_file(in_path, out_path)

        if not os.path.exists(in_path):
            raise AnsibleFileNotFound(f"file not found: {in_path}")

        display.vvv(f"FASTAGENT: put_file {in_path} -> {out_path}", host=self.get_option("host"))

        with open(in_path, "rb") as f:
            data = f.read()

        content_b64 = base64.b64encode(data).decode("ascii")

        # Set ownership to the connecting user so files in ~/.ansible/tmp/
        # aren't owned by root.
        remote_user = self.get_option("remote_user")

        try:
            self._agent_client.write_file(
                dest=out_path,
                content=content_b64,
                owner=remote_user,
                group=remote_user,
            )
        except (FastAgentError, IOError) as e:
            raise AnsibleConnectionFailure(f"fastagent put_file failed: {e}")

    def fetch_file(self, in_path: str, out_path: str) -> None:
        super().fetch_file(in_path, out_path)

        display.vvv(f"FASTAGENT: fetch_file {in_path} -> {out_path}", host=self.get_option("host"))

        try:
            result = self._agent_client.read_file(in_path)
        except (FastAgentError, IOError) as e:
            raise AnsibleConnectionFailure(f"fastagent fetch_file failed: {e}")

        data = base64.b64decode(result["content"])
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir)
        with open(out_path, "wb") as f:
            f.write(data)

    def close(self) -> None:
        # Close the local socket connection. The SSH forwarding session and
        # remote daemon both stay alive for the next task.
        if self._socket is not None:
            display.vvv("FASTAGENT: closing socket", host=self.get_option("host"))
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
            self._agent_client = None
        self._connected = False

    def reset(self) -> None:
        self.close()
        self._connect()

    def _build_ssh_command(
        self,
        host: str,
        user: str | None,
        port: int | None,
        remote_cmd: str,
    ) -> list[str]:
        ssh_executable = self.get_option("ssh_executable") or "ssh"
        cmd = [ssh_executable]

        ssh_args = self.get_option("ssh_args")
        if ssh_args:
            cmd.extend(shlex.split(ssh_args))

        private_key = self.get_option("private_key")
        if private_key:
            cmd.extend(["-o", f"IdentityFile={private_key}"])

        if user:
            cmd.extend(["-o", f"User={user}"])
        if port:
            cmd.extend(["-o", f"Port={port}"])

        # Disable pseudo-terminal allocation (we want raw stdio).
        cmd.append("-T")

        cmd.append(host)
        cmd.append(remote_cmd)

        return cmd

    def _run_ssh_command(
        self,
        host: str,
        user: str | None,
        port: int | None,
        remote_cmd: str,
    ) -> tuple[int, str, str]:
        """Run a one-shot SSH command and return (rc, stdout, stderr)."""
        ssh_cmd = self._build_ssh_command(host, user, port, remote_cmd)
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            timeout=30,
        )
        return (
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )

    def _detect_remote_arch(
        self,
        host: str,
        user: str | None,
        port: int | None,
    ) -> str:
        """Detect the remote host's architecture."""
        rc, stdout, stderr = self._run_ssh_command(host, user, port, "uname -m")
        if rc != 0:
            raise AnsibleConnectionFailure(
                f"fastagent: failed to detect remote arch: {stderr}"
            )
        uname = stdout.strip()
        arch_map = {
            "x86_64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }
        arch = arch_map.get(uname)
        if arch is None:
            raise AnsibleConnectionFailure(
                f"fastagent: unsupported remote architecture: {uname}"
            )
        display.vvv(f"FASTAGENT: detected remote arch: {uname} -> {arch}", host=host)
        return arch

    def _ensure_agent_deployed(
        self,
        host: str,
        user: str | None,
        port: int | None,
        remote_path: str,
        arch: str,
    ) -> None:
        """Upload agent binary if not already present with the correct version."""
        # Check if the correct version is already deployed.
        rc, stdout, _ = self._run_ssh_command(
            host, user, port,
            f"{shlex.quote(remote_path)} --version 2>/dev/null || true",
        )
        if rc == 0 and AGENT_VERSION in stdout:
            display.vvv("FASTAGENT: agent already deployed", host=host)
            return

        # Find local binary (or download a prebuilt one).
        local_binary = self._find_local_binary(arch)
        if local_binary is None:
            raise AnsibleConnectionFailure(
                f"fastagent: cannot find local agent binary for linux-{arch} "
                f"and fastagent_download_url is empty. Build from source with "
                f"`make build` or set fastagent_local_agent_dir."
            )

        display.vvv(f"FASTAGENT: uploading {local_binary} -> {remote_path}", host=host)

        remote_dir = os.path.dirname(remote_path)
        self._run_ssh_command(host, user, port, f"mkdir -p {shlex.quote(remote_dir)}")

        # Upload via scp.
        scp_executable = self.get_option("scp_executable") or "scp"
        scp_cmd = [scp_executable]

        private_key = self.get_option("private_key")
        if private_key:
            scp_cmd.extend(["-i", private_key])
        if port:
            scp_cmd.extend(["-P", str(port)])

        scp_target = f"{host}:{remote_path}"
        if user:
            scp_target = f"{user}@{scp_target}"

        scp_cmd.extend([local_binary, scp_target])

        result = subprocess.run(scp_cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise AnsibleConnectionFailure(
                f"fastagent: scp upload failed: "
                f"{result.stderr.decode('utf-8', errors='replace')}"
            )

        # Make executable.
        self._run_ssh_command(
            host, user, port, f"chmod +x {shlex.quote(remote_path)}"
        )

        display.vvv("FASTAGENT: agent deployed successfully", host=host)

    def _find_local_binary(self, arch: str) -> str | None:
        """Find or download the local agent binary for the given architecture.

        Resolution order:
        1. fastagent_local_agent_dir (explicit override)
        2. ~/.ansible/fastagent/ (canonical cache — also where downloads land)
        3. tmp/ next to the repo root (for in-repo development builds)
        4. Download from fastagent_download_url if set
        """
        binary_name = f"fastagent-linux-{arch}"
        versioned_name = f"fastagent-{AGENT_VERSION}-linux-{arch}"

        # Check explicit config.
        local_dir = self.get_option("local_agent_dir")
        if local_dir:
            local_dir = os.path.expanduser(local_dir)
            for name in (versioned_name, binary_name):
                path = os.path.join(local_dir, name)
                if os.path.isfile(path):
                    return path

        # Check ~/.ansible/fastagent/ (canonical cache location).
        cache_dir = os.path.join(os.path.expanduser("~"), ".ansible", "fastagent")
        for name in (versioned_name, binary_name):
            path = os.path.join(cache_dir, name)
            if os.path.isfile(path):
                return path

        # Check tmp/ at the repo root (for in-repo development builds).
        # __file__ is plugins/connection/fastagent.py, so go up three levels.
        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        for subdir in ("tmp", "build"):
            path = os.path.join(repo_root, subdir, binary_name)
            if os.path.isfile(path):
                return path

        # Fall back to downloading a pre-built binary from the configured URL.
        return self._download_binary(arch, cache_dir, versioned_name)

    def _download_binary(
        self, arch: str, cache_dir: str, versioned_name: str
    ) -> str | None:
        """Download the agent binary from the configured release URL.

        Returns the path to the cached binary on success, None on failure or
        when auto-download is disabled.

        Uses curl in a subprocess instead of urllib to avoid SSL crashes in
        Ansible's forked worker processes (macOS fork + SSL is unsafe).
        """
        url_template = self.get_option("download_url")
        if not url_template:
            return None

        url = url_template.format(version=AGENT_VERSION, arch=arch)
        dest_path = os.path.join(cache_dir, versioned_name)

        display.v(
            f"FASTAGENT: downloading agent binary from {url} -> {dest_path}",
        )

        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            display.warning(
                f"fastagent: could not create cache dir {cache_dir}: {exc}"
            )
            return None

        # Write to a temp file in the same directory so the final rename is
        # atomic even if another process is racing us.
        tmp_fd, tmp_path = None, None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{versioned_name}.", dir=cache_dir
            )
            os.close(tmp_fd)
            tmp_fd = None

            result = subprocess.run(
                [
                    "curl",
                    "--silent", "--show-error",
                    "--fail",
                    "--location",
                    "--output", tmp_path,
                    "--user-agent", f"fastagent/{AGENT_VERSION}",
                    "--max-time", "120",
                    url,
                ],
                capture_output=True,
                timeout=130,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                raise AnsibleConnectionFailure(
                    f"fastagent: download of {url} failed (curl exit {result.returncode}): "
                    f"{stderr}"
                )

            # Sanity check: the binary should be at least 1 MB.
            file_size = os.path.getsize(tmp_path)
            if file_size < 1_000_000:
                raise AnsibleConnectionFailure(
                    f"fastagent: downloaded file is suspiciously small "
                    f"({file_size} bytes) from {url}"
                )

            os.chmod(tmp_path, 0o755)
            os.replace(tmp_path, dest_path)
            tmp_path = None
            display.v(f"FASTAGENT: cached agent binary at {dest_path}")
            return dest_path
        except AnsibleConnectionFailure:
            raise
        except Exception as exc:
            raise AnsibleConnectionFailure(
                f"fastagent: failed to download agent binary from {url}: {exc}. "
                f"Build from source with `make build` or set "
                f"fastagent_local_agent_dir to a directory containing "
                f"{versioned_name}."
            )
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
