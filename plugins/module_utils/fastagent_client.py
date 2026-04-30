"""Shared JSON-RPC client for communicating with the fastagent Go binary.

The client speaks newline-delimited JSON over stdin/stdout of a subprocess
(typically an SSH session running the agent).
"""

from __future__ import annotations

import json
import os
import threading
import time as _time

# When FASTAGENT_TRACE is set, each RPC is appended to this file as TSV:
#   timestamp_ns \t method \t duration_ms \t hint
# Set to an empty string to disable.
_TRACE_PATH = os.environ.get("FASTAGENT_TRACE") or ""
_TRACE_LOCK = threading.Lock()


def _trace_hint(method: str, params: dict | None) -> str:
    """Extract a short identifying string from params for trace logs."""
    if not params:
        return ""
    if method == "Exec":
        cmd = params.get("cmd_string") or " ".join(params.get("argv") or [])
        return cmd[:160].replace("\t", " ").replace("\n", " ")
    if method in ("Stat", "ReadFile", "File"):
        return str(params.get("path", ""))[:160]
    if method == "WriteFile":
        return str(params.get("dest", ""))[:160]
    if method == "Package":
        names = params.get("names") or []
        return (",".join(names) if isinstance(names, list) else str(names))[:160]
    if method == "Service":
        return str(params.get("name", ""))[:160]
    return ""


def _trace(method: str, duration_ns: int, hint: str) -> None:
    if not _TRACE_PATH:
        return
    line = f"{_time.time_ns()}\t{method}\t{duration_ns / 1_000_000:.3f}\t{hint}\n"
    try:
        with _TRACE_LOCK, open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        # Never let tracing break the deploy.
        pass


class FastAgentError(Exception):
    """Raised when the agent returns an error response."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"fastagent error {code}: {message}")


class FastAgentVersionMismatch(Exception):
    """Raised when the daemon's reported version differs from the controller's.

    Go's JSON decoder silently drops unknown fields, so an older daemon
    would accept RPCs that include new fields (e.g. BecomeUser added in
    0.5.5) and just ignore them — leading to silent wrong behavior on
    the remote side. Treat any version skew as a hard error so the
    caller tears down and re-bootstraps with the matching daemon.
    """

    def __init__(self, expected: str, actual: str):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"fastagent daemon version mismatch: expected {expected}, got {actual}"
        )


class FastAgentClient:
    """JSON-RPC client for fastagent.

    Communicates over the stdin/stdout of a subprocess. Thread-safe via a lock
    on the request ID counter and I/O.
    """

    def __init__(self, stdin, stdout):
        """Initialize with file-like objects for the agent's stdin and stdout.

        Args:
            stdin: writable file-like object (agent's stdin)
            stdout: readable file-like object (agent's stdout)
        """
        self._stdin = stdin
        self._stdout = stdout
        self._next_id = 1
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and return the result.

        Args:
            method: the RPC method name (e.g. "Hello", "Exec", "Stat")
            params: method parameters

        Returns:
            The result dict from the agent.

        Raises:
            FastAgentError: if the agent returns an error response.
            IOError: if communication with the agent fails.
        """
        with self._lock:
            req_id = self._next_id
            self._next_id += 1

            request = {
                "id": req_id,
                "method": method,
                "params": params or {},
            }

            line = json.dumps(request, separators=(",", ":")) + "\n"
            start_ns = _time.monotonic_ns() if _TRACE_PATH else 0
            self._stdin.write(line.encode("utf-8"))
            self._stdin.flush()

            response_line = self._stdout.readline()
            if not response_line:
                raise IOError(
                    "fastagent: no response (agent process may have exited)"
                )

            if _TRACE_PATH:
                _trace(method, _time.monotonic_ns() - start_ns, _trace_hint(method, params))

            response = json.loads(response_line)

            if response.get("id") != req_id:
                raise IOError(
                    f"fastagent: response id mismatch: "
                    f"expected {req_id}, got {response.get('id')}"
                )

            if "error" in response and response["error"] is not None:
                err = response["error"]
                raise FastAgentError(err.get("code", 1), err.get("message", "unknown error"))

            return response.get("result", {})

    def hello(self, version: str = "0.1.0") -> dict:
        """Send Hello handshake and verify the daemon's version matches.

        Raises FastAgentVersionMismatch if the daemon reports a different
        version. The caller is expected to tear down the connection and
        bootstrap a fresh daemon at the matching version.
        """
        result = self.call("Hello", {"version": version})
        daemon_version = result.get("version", "")
        if daemon_version != version:
            raise FastAgentVersionMismatch(version, daemon_version)
        return result

    def exec(
        self,
        argv: list[str] | None = None,
        cmd_string: str | None = None,
        use_shell: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout: int | None = None,
        creates: str | None = None,
        removes: str | None = None,
        stdin_add_newline: bool = True,
        strip_empty_ends: bool = True,
        become_user: str | None = None,
    ) -> dict:
        """Execute a command on the remote host.

        If become_user is set, the agent wraps the invocation with
        `sudo -H -n -u <become_user> --` so it runs as that user. This
        requires the agent to be running as root, which is the case
        whenever Ansible's `become: true` is in effect.
        """
        params = {"use_shell": use_shell}
        if argv is not None:
            params["argv"] = argv
        if cmd_string is not None:
            params["cmd_string"] = cmd_string
        if cwd is not None:
            params["cwd"] = cwd
        if env is not None:
            params["env"] = env
        if stdin is not None:
            params["stdin"] = stdin
        if timeout is not None:
            params["timeout"] = timeout
        if creates is not None:
            params["creates"] = creates
        if removes is not None:
            params["removes"] = removes
        params["stdin_add_newline"] = stdin_add_newline
        params["strip_empty_ends"] = strip_empty_ends
        if become_user is not None:
            params["become_user"] = become_user
        return self.call("Exec", params)

    def stat(
        self,
        path: str,
        follow: bool = False,
        checksum: bool = False,
        checksum_algorithm: str | None = None,
    ) -> dict:
        """Stat a file on the remote host.

        Does NOT support `become_user`: stat runs as the agent's uid
        (typically root), which could leak metadata the become_user
        couldn't otherwise see. Callers that need become-user stat
        semantics must fall back to the builtin stat module.
        """
        params = {
            "path": path,
            "follow": follow,
            "checksum": checksum,
        }
        if checksum_algorithm is not None:
            params["checksum_algorithm"] = checksum_algorithm
        return self.call("Stat", params)

    def read_file(self, path: str) -> dict:
        """Read a file from the remote host (content is base64-encoded).

        Does NOT support become_user; same rationale as `stat`.
        """
        return self.call("ReadFile", {"path": path})

    def write_file(
        self,
        dest: str,
        content: str,
        owner: str | None = None,
        group: str | None = None,
        mode: str | None = None,
        backup: bool = False,
        unsafe_writes: bool = False,
        checksum: str | None = None,
    ) -> dict:
        """Write a file to the remote host.

        Args:
            dest: destination path
            content: base64-encoded file content
            owner: file owner
            group: file group
            mode: file mode (octal string, e.g. "0644")
            backup: create a backup of the existing file
            unsafe_writes: write directly instead of atomic rename
            checksum: expected checksum of existing file (skip if matches)
        """
        params: dict = {"dest": dest, "content": content}
        if owner is not None:
            params["owner"] = owner
        if group is not None:
            params["group"] = group
        if mode is not None:
            params["mode"] = mode
        if backup:
            params["backup"] = True
        if unsafe_writes:
            params["unsafe_writes"] = True
        if checksum is not None:
            params["checksum"] = checksum
        return self.call("WriteFile", params)

    def file(
        self,
        path: str,
        state: str,
        owner: str | None = None,
        group: str | None = None,
        mode: str | None = None,
        recurse: bool = False,
        follow: bool = True,
        src: str | None = None,
    ) -> dict:
        """Manage file/directory/link state."""
        params: dict = {"path": path, "state": state}
        if owner is not None:
            params["owner"] = owner
        if group is not None:
            params["group"] = group
        if mode is not None:
            params["mode"] = mode
        if recurse:
            params["recurse"] = True
        if not follow:
            params["follow"] = False
        if src is not None:
            params["src"] = src
        return self.call("File", params)

    def package(
        self,
        manager: str,
        names: list[str],
        state: str = "present",
    ) -> dict:
        """Manage OS packages."""
        return self.call("Package", {
            "manager": manager,
            "names": names,
            "state": state,
        })

    def service(
        self,
        name: str,
        manager: str = "systemd",
        state: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """Manage system services."""
        params: dict = {"name": name, "manager": manager}
        if state is not None:
            params["state"] = state
        if enabled is not None:
            params["enabled"] = enabled
        return self.call("Service", params)
