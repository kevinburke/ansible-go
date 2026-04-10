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
        default: "~/.ansible/fastagent/fastagent-{version}-{os}-{arch}"
        vars:
            - name: fastagent_agent_path
    local_agent_dir:
        description: >
            Local directory containing pre-built agent binaries.
            Expected layout: fastagent-linux-amd64, fastagent-linux-arm64, etc.
        vars:
            - name: fastagent_local_agent_dir
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
"""

import base64
import collections
import hashlib
import os
import shlex
import subprocess
import sys
import threading
import typing as t

from ansible.errors import AnsibleConnectionFailure, AnsibleFileNotFound
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display
from ansible.module_utils.common.text.converters import to_bytes, to_text

# The fastagent_client module lives in module_utils/ next to connection_plugins/.
# Ansible's module_utils config only injects paths for remote module execution,
# not for controller-side plugin imports, so we add it to sys.path ourselves.
_module_utils_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "module_utils",
)
if _module_utils_dir not in sys.path:
    sys.path.insert(0, _module_utils_dir)

from fastagent_client import FastAgentClient, FastAgentError  # noqa: E402

display = Display()

# Agent version must match the Go constant.
AGENT_VERSION = "0.2.0"

class Connection(ConnectionBase):
    """fastagent connection plugin."""

    transport = "fastagent"
    has_pipelining = False
    supports_persistence = False

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self._ssh_process: subprocess.Popen | None = None
        self._agent_client: FastAgentClient | None = None
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=100)
        self._stderr_thread: threading.Thread | None = None

    def _connect(self) -> Connection:
        if self._connected:
            return self

        host = self.get_option("host")
        user = self.get_option("remote_user")
        port = self.get_option("port")

        # Determine become settings.
        become = self._play_context.become
        become_method = self._play_context.become_method
        become_user = "root"
        use_become = False
        if become:
            if become_method == "sudo" or become_method is None:
                become_user = self._play_context.become_user or "root"
                use_become = True
            else:
                display.warning(
                    f"fastagent: unsupported become_method {become_method!r}, "
                    f"falling back to direct execution"
                )

        # Socket path on the remote host, keyed by become user.
        socket_path = f"/tmp/fastagent-{become_user}.sock" if use_become else "/tmp/fastagent.sock"

        # Fast path: try connecting to an existing daemon directly. This
        # skips arch detection, deployment check, and daemon startup — just
        # one SSH command (the bridge). This is the common case after the
        # first task has bootstrapped the daemon.
        if self._try_connect_bridge(host, user, port, socket_path, use_become, become_user):
            return self

        display.vvv(f"FASTAGENT: fast path failed, doing full bootstrap", host=host)

        # Full bootstrap: detect arch, deploy binary, start daemon, connect.
        remote_arch = self._detect_remote_arch(host, user, port)
        agent_path_template = self.get_option("agent_path")
        remote_agent_path = agent_path_template.format(
            version=AGENT_VERSION,
            os="linux",
            arch=remote_arch,
        )

        # Expand ~ to the remote user's home directory.
        if remote_agent_path.startswith("~/"):
            rc, home, _ = self._run_ssh_command(host, user, port, "echo $HOME")
            if rc == 0 and home.strip():
                remote_agent_path = home.strip() + remote_agent_path[1:]

        # Upload agent binary if needed.
        self._ensure_agent_deployed(host, user, port, remote_agent_path, remote_arch)

        agent_bin = shlex.quote(remote_agent_path)

        # Kill any old daemon (version mismatch or stale).
        kill_cmd = (
            f"pkill -f 'fastagent --daemon' 2>/dev/null;"
            f" rm -f {shlex.quote(socket_path)} {shlex.quote(socket_path + '.pid')}"
        )
        if use_become:
            kill_cmd = f"sudo sh -c {shlex.quote(kill_cmd)}"
        self._run_ssh_command(host, user, port, kill_cmd)

        # Start the daemon.
        debug_flag = " --debug" if display.verbosity >= 3 else ""
        daemon_cmd = f"{agent_bin} --daemon --socket {shlex.quote(socket_path)}{debug_flag}"
        if use_become:
            daemon_cmd = f"sudo -u {shlex.quote(become_user)} {daemon_cmd}"

        log_path = socket_path + ".log"
        start_cmd = (
            f"setsid {daemon_cmd} </dev/null >>{shlex.quote(log_path)} 2>&1 &"
            f" for i in 1 2 3 4 5; do"
            f"   test -S {shlex.quote(socket_path)} && exit 0;"
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
        display.vvv(f"FASTAGENT: daemon started at {socket_path}", host=host)

        # Now connect via bridge (pass known binary path to avoid glob).
        if not self._try_connect_bridge(host, user, port, socket_path, use_become, become_user, agent_bin=agent_bin):
            raise AnsibleConnectionFailure(
                "fastagent: failed to connect to daemon after starting it"
            )

        return self

    def _try_connect_bridge(
        self,
        host: str,
        user: str | None,
        port: int | None,
        socket_path: str,
        use_become: bool,
        become_user: str,
        agent_bin: str | None = None,
    ) -> bool:
        """Try to connect to an existing daemon. Returns True on success."""
        if agent_bin is None:
            # Fast path: find any fastagent binary using a glob. The shell
            # expands $HOME and the glob, so no arch detection SSH needed.
            agent_path_template = self.get_option("agent_path")
            # Replace {version}, {os}, {arch} with the known version and wildcards.
            glob_path = agent_path_template.format(
                version=AGENT_VERSION, os="linux", arch="*",
            )
            if glob_path.startswith("~/"):
                glob_path = glob_path.replace("~/", "$HOME/", 1)
            # Use shell: find the first matching binary.
            agent_bin_expr = f"$(ls -1 {glob_path} 2>/dev/null | head -1)"
        else:
            agent_bin_expr = agent_bin

        connect_cmd = f"{agent_bin_expr} --connect --socket {shlex.quote(socket_path)}"
        if use_become:
            connect_cmd = f"sudo -u {shlex.quote(become_user)} {connect_cmd}"

        ssh_cmd = self._build_ssh_command(host, user, port, connect_cmd)
        display.vvv(f"FASTAGENT: trying fast connect to {host}", host=host)

        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stderr_lines: collections.deque[str] = collections.deque(maxlen=100)
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(proc.stderr, host),
            daemon=True,
        )
        stderr_thread.start()

        client = FastAgentClient(proc.stdin, proc.stdout)

        # Try Hello with a short timeout. If the daemon isn't running or the
        # binary doesn't exist, this will fail quickly.
        try:
            result = client.hello(AGENT_VERSION)
            remote_version = result.get("version", "")
            display.vvv(
                f"FASTAGENT: connected to daemon version={remote_version}",
                host=host,
            )

            if remote_version != AGENT_VERSION:
                display.vvv(
                    f"FASTAGENT: version mismatch (want {AGENT_VERSION}, "
                    f"got {remote_version})",
                    host=host,
                )
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.terminate()
                proc.wait(timeout=5)
                return False

        except Exception as e:
            display.vvv(f"FASTAGENT: fast connect failed: {e}", host=host)
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return False

        # Success — adopt this connection.
        self._ssh_process = proc
        self._agent_client = client
        self._stderr_lines = stderr_lines
        self._stderr_thread = stderr_thread
        self._connected = True
        return True

    def exec_command(
        self,
        cmd: str,
        in_data: bytes | None = None,
        sudoable: bool = True,
    ) -> tuple[int, bytes, bytes]:
        super().exec_command(cmd, in_data=in_data, sudoable=sudoable)

        # The daemon runs as root. For commands that should NOT run as root
        # (sudoable=False, or become not configured), drop privileges to the
        # connecting user. This prevents things like temp dir creation from
        # being owned by root in the user's home directory.
        actual_cmd = cmd
        become = self._play_context.become
        remote_user = self.get_option("remote_user")
        if remote_user and (not sudoable or not become):
            actual_cmd = f"runuser -u {shlex.quote(remote_user)} -- sh -c {shlex.quote(cmd)}"

        stdin_data = None
        if in_data is not None:
            stdin_data = in_data.decode("utf-8", errors="surrogateescape")

        try:
            result = self._agent_client.exec(
                cmd_string=actual_cmd,
                use_shell=True,
                stdin=stdin_data,
            )
        except IOError as e:
            agent_stderr = self._get_agent_stderr()
            msg = f"fastagent exec_command failed: {e}"
            if agent_stderr:
                msg += f"\nagent stderr:\n{agent_stderr}"
            return (1, b"", to_bytes(msg))
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
            agent_stderr = self._get_agent_stderr()
            msg = f"fastagent put_file failed: {e}"
            if agent_stderr:
                msg += f"\nagent stderr:\n{agent_stderr}"
            raise AnsibleConnectionFailure(msg)

    def fetch_file(self, in_path: str, out_path: str) -> None:
        super().fetch_file(in_path, out_path)

        display.vvv(f"FASTAGENT: fetch_file {in_path} -> {out_path}", host=self.get_option("host"))

        try:
            result = self._agent_client.read_file(in_path)
        except (FastAgentError, IOError) as e:
            agent_stderr = self._get_agent_stderr()
            msg = f"fastagent fetch_file failed: {e}"
            if agent_stderr:
                msg += f"\nagent stderr:\n{agent_stderr}"
            raise AnsibleConnectionFailure(msg)

        data = base64.b64decode(result["content"])
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir)
        with open(out_path, "wb") as f:
            f.write(data)

    def close(self) -> None:
        # Close the SSH bridge session. The daemon stays alive on the remote
        # host — next task will reconnect to it cheaply.
        if self._ssh_process is not None:
            display.vvv("FASTAGENT: closing bridge (daemon stays alive)", host=self.get_option("host"))
            try:
                self._ssh_process.stdin.close()
            except Exception:
                pass
            try:
                self._ssh_process.terminate()
                self._ssh_process.wait(timeout=5)
            except Exception:
                try:
                    self._ssh_process.kill()
                except Exception:
                    pass
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2)
                self._stderr_thread = None
            self._ssh_process = None
            self._agent_client = None
        self._connected = False

    def reset(self) -> None:
        self.close()
        self._connect()

    # --- internal helpers ---

    def _read_stderr(self, stderr_pipe, host: str) -> None:
        """Background thread: read agent stderr line-by-line.

        Each line is stored in a bounded deque (last 100 lines) and forwarded
        to Ansible's display at -vvv verbosity. This prevents the pipe buffer
        from filling up and surfaces agent log messages and crash output.
        """
        try:
            for raw_line in stderr_pipe:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                self._stderr_lines.append(line)
                display.vvv(f"FASTAGENT [{host}]: {line}", host=host)
        except Exception:
            pass

    def _get_agent_stderr(self) -> str:
        """Return recent agent stderr lines, for inclusion in error messages."""
        if not self._stderr_lines:
            return ""
        return "\n".join(self._stderr_lines)

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

        # Find local binary.
        local_binary = self._find_local_binary(arch)
        if local_binary is None:
            raise AnsibleConnectionFailure(
                f"fastagent: cannot find local agent binary for linux-{arch}. "
                f"Build it with: make build"
            )

        display.vvv(f"FASTAGENT: uploading {local_binary} -> {remote_path}", host=host)

        # Ensure remote directory exists.
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
        self._run_ssh_command(host, user, port, f"chmod +x {shlex.quote(remote_path)}")

        display.vvv("FASTAGENT: agent deployed successfully", host=host)

    def _find_local_binary(self, arch: str) -> str | None:
        """Find the local agent binary for the given architecture."""
        binary_name = f"fastagent-linux-{arch}"
        versioned_name = f"fastagent-{AGENT_VERSION}-linux-{arch}"

        # Check explicit config.
        local_dir = self.get_option("local_agent_dir")
        if local_dir:
            for name in (versioned_name, binary_name):
                path = os.path.join(local_dir, name)
                if os.path.isfile(path):
                    return path

        # Check ~/.ansible/fastagent/ (where `make deploy` puts them).
        home_dir = os.path.join(os.path.expanduser("~"), ".ansible", "fastagent")
        for name in (versioned_name, binary_name):
            path = os.path.join(home_dir, name)
            if os.path.isfile(path):
                return path

        # Check tmp/ relative to the plugin's directory (build output).
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for subdir in ("tmp", "build", "."):
            path = os.path.join(plugin_dir, subdir, binary_name)
            if os.path.isfile(path):
                return path

        return None
