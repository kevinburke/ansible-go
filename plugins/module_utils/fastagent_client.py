"""Shared JSON-RPC client for communicating with the fastagent Go binary.

The client speaks newline-delimited JSON over stdin/stdout of a subprocess
(typically an SSH session running the agent).
"""

from __future__ import annotations

import json
import os
import secrets
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


# The agent refused a request whose once token it had already received,
# without running it (ErrCodeDuplicate in fastagent.go).
ERR_CODE_DUPLICATE = -32002

# A deferred Hello goes out in the same write as the first request only when
# that request's line is at most this long. The probe timeout covers the
# write, and a line this size fits in the local socket and ssh buffers, so
# the write never waits on the network. A longer request waits for the Hello
# answer before it is sent.
PIPELINE_MAX_BYTES = 1 << 20


# Set on the become wrapper the fastagent connection attaches for a method
# the agent implements itself (sudo). See set_become_plugin in
# plugins/connection/fastagent.py.
AGENT_HANDLES_BECOME_ATTR = "fastagent_handles_become"


def ansible_applies_become(connection) -> bool:
    """Report whether Ansible, not the agent, must apply this task's become.

    The fastagent connection always exposes become on `connection.become` so
    ansible-core's ActionBase sees an unprivileged become_user and grants it
    access to uploaded files. For sudo the attached plugin is a wrapper the
    agent handles; only another become method needs Ansible's own module
    path. Action overrides must use this rather than testing
    `connection.become is not None`, which is true for sudo too.
    """
    become = getattr(connection, "become", None)
    return become is not None and not getattr(become, AGENT_HANDLES_BECOME_ATTR, False)


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

    See defer_hello for the handshake the connection plugin uses on its fast
    path, which saves a round trip per task.
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
        self._broken = False
        # Set by defer_hello until the first call sends the Hello.
        self._deferred_hello: str | None = None
        self._on_verified = None
        self._recover = None

    def defer_hello(self, version: str, on_verified, recover) -> None:
        """Send the Hello handshake with the first request, not on its own.

        The Hello goes out in the same write as the first request, which
        carries a random "once" token, and the client reads the Hello's
        answer before the request's. This saves the Hello's round trip,
        which Ansible pays once per task because each task opens a new
        connection.

        on_verified() runs once a matching Hello answer arrives, before the
        request's answer is read; the connection plugin clears the probe
        timeout there. If the Hello gets no valid answer, or a version
        mismatch, recover() must set up a new connection whose daemon has
        answered a Hello, and return that connection's (stdin, stdout). The
        client sends the request again on it, with the same token. Nothing
        about the first connection says whether its copy of the request
        arrived: a stale socket delivers nothing, but a connection lost in
        flight may deliver it, now or later. The daemon runs a token at
        most once, so the second copy runs only if the first did not, and
        otherwise fails loudly as a duplicate.
        """
        self._deferred_hello = version
        self._on_verified = on_verified
        self._recover = recover

    def streams(self):
        """Return the (stdin, stdout) pair this client talks over."""
        return self._stdin, self._stdout

    @property
    def broken(self) -> bool:
        """True once a call failed mid-request.

        The stream may hold a late response to that request, so no later
        request can be sent on it. The caller needs a new stream.
        """
        return self._broken

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
            if self._deferred_hello is not None:
                return self._call_with_hello(request)

            line = self._encode(request)
            if self._broken:
                raise IOError("fastagent: RPC stream is unusable after an earlier failure")
            return self._roundtrip(request, line)

    @staticmethod
    def _encode(request: dict) -> bytes:
        return (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

    def _roundtrip(self, request: dict, line: bytes, write: bool = True) -> dict:
        """Send line (unless write is False) and read request's answer.

        Any failure marks the stream broken: it may hold a late answer.
        """
        method = request["method"]
        start_ns = _time.monotonic_ns() if _TRACE_PATH else 0
        try:
            if write:
                self._stdin.write(line)
                self._stdin.flush()
            response = self._read_response(request["id"])
        except BaseException as exc:
            self._broken = True
            if not isinstance(exc, Exception):
                raise
            raise IOError(f"fastagent: execution outcome unknown; request was not replayed: {exc}") from exc
        finally:
            if _TRACE_PATH:
                _trace(method, _time.monotonic_ns() - start_ns, _trace_hint(method, request["params"]))

        if "error" in response and response["error"] is not None:
            err = response["error"]
            raise FastAgentError(err.get("code", 1), err.get("message", "unknown error"))

        return response.get("result", {})

    def _read_response(self, req_id: int) -> dict:
        response_line = self._stdout.readline()
        if not response_line:
            raise IOError("no response (agent process may have exited)")
        response = json.loads(response_line)
        if not isinstance(response, dict) or type(response.get("id")) is not int or response["id"] != req_id:
            raise IOError("response id mismatch or invalid response")
        if response.get("error") is not None and (
            not isinstance(response["error"], dict)
            or type(response["error"].get("code")) is not int
            or not isinstance(response["error"].get("message"), str)
        ):
            raise IOError("invalid agent error response")
        if response.get("error") is None and not isinstance(response.get("result"), dict):
            raise IOError("missing or invalid agent result")
        return response

    def _call_with_hello(self, request: dict) -> dict:
        """Send the deferred Hello and request together; see defer_hello."""
        version = self._deferred_hello
        on_verified, recover = self._on_verified, self._recover

        # The Hello takes the request's id and the request the next one, so
        # the ids on the stream still increase in the order sent.
        hello = {"id": request["id"], "method": "Hello", "params": {"version": version}}
        request["id"] = self._next_id
        self._next_id += 1
        request["once"] = secrets.token_hex(16)
        # Encode before giving up the deferred Hello: params that do not
        # serialize fail here with nothing sent, and the next call must
        # still send the Hello.
        line = self._encode(request)
        pipelined = len(line) <= PIPELINE_MAX_BYTES
        self._deferred_hello = self._on_verified = self._recover = None

        start_ns = _time.monotonic_ns() if _TRACE_PATH else 0
        try:
            self._stdin.write(self._encode(hello) + (line if pipelined else b""))
            self._stdin.flush()
            response = self._read_response(hello["id"])
            if response.get("error") is not None:
                raise IOError(f"Hello failed: {response['error'].get('message')}")
            daemon_version = response["result"].get("version", "")
            if daemon_version != version:
                raise FastAgentVersionMismatch(version, daemon_version)
        except Exception as exc:
            if not pipelined:
                # Nothing but the Hello was sent; this is the old
                # connect-time probe failing.
                self._recover_stream(recover, exc, sent_request=False)
                return self._roundtrip(request, line)
            self._recover_stream(recover, exc, sent_request=True)
            # A second copy of the request. The daemon runs it only if
            # the first copy never arrived.
            try:
                return self._roundtrip(request, line)
            except FastAgentError as err:
                if err.code == ERR_CODE_DUPLICATE:
                    raise IOError(
                        f"fastagent: execution outcome unknown; the connection "
                        f"failed ({exc}) after the request was sent, and the "
                        f"agent received that copy, so it was not run again: "
                        f"{err.message}"
                    ) from err
                raise
        except BaseException:
            self._broken = True
            raise
        finally:
            if _TRACE_PATH:
                _trace("Hello", _time.monotonic_ns() - start_ns, "deferred")

        if on_verified is not None:
            on_verified()
        return self._roundtrip(request, line, write=not pipelined)

    def _recover_stream(self, recover, exc: Exception, sent_request: bool) -> None:
        """Replace the stream after a deferred Hello failed; see defer_hello."""
        if recover is None:
            self._broken = True
            raise IOError(f"fastagent: Hello failed and no reconnect is available: {exc}") from exc
        try:
            self._stdin, self._stdout = recover()
        except Exception as rexc:
            self._broken = True
            if sent_request:
                raise IOError(
                    f"fastagent: execution outcome unknown; request was not "
                    f"replayed: the connection failed ({exc}) after the "
                    f"request was sent, and reconnecting failed: {rexc}"
                ) from rexc
            raise
        self._broken = False

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
        builtin: bool = False,
        mime: bool = False,
        attributes: bool = False,
        env: dict | None = None,
    ) -> dict:
        """Stat a file on the remote host.

        Does NOT support `become_user`: stat runs as the agent's uid
        (typically root), which could leak metadata the become_user
        couldn't otherwise see. Callers that need become-user stat
        semantics must fall back to the builtin stat module.

        builtin=True asks for ansible.builtin.stat semantics (see
        StatParams.Builtin in fastagent.go); mime, attributes and env
        only apply then.
        """
        params = {
            "path": path,
            "follow": follow,
            "checksum": checksum,
        }
        if checksum_algorithm is not None:
            params["checksum_algorithm"] = checksum_algorithm
        if builtin:
            params["builtin"] = True
            params["mime"] = mime
            params["attributes"] = attributes
            if env:
                params["env"] = env
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
        validate: dict | None = None,
        env: dict | None = None,
        report_dir: bool = False,
    ) -> dict:
        """Write a file to the remote host.

        When dest already holds this content, nothing is written, backed
        up or validated; only owner/group/mode are applied, and "changed"
        says whether they were.

        Args:
            dest: destination path
            content: base64-encoded file content
            owner: file owner
            group: file group
            mode: file mode (octal string, e.g. "0644")
            backup: create a backup of the existing file
            unsafe_writes: write directly instead of atomic rename
            checksum: expected checksum of existing file (skip if matches)
            validate: {"argv": [...], "placeholder": str}; run against a
                copy of the new content before it replaces dest. On
                failure dest is left alone and the result has
                "validate_failed" (see WriteValidate in fastagent.go).
            env: the task's environment, for the validate command
            report_dir: when dest is a directory, return
                {"dest_is_dir": True} instead of raising. Either way
                nothing is written.
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
        if validate is not None:
            params["validate"] = validate
        if env:
            params["env"] = env
        if report_dir:
            params["report_dir"] = True
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
        mtime: str | None = None,
        atime: str | None = None,
    ) -> dict:
        """Manage file/directory/link state.

        mtime and atime are "now" or "preserve" and only apply to
        state=touch; the agent treats None as "now".
        """
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
        if mtime is not None:
            params["mtime"] = mtime
        if atime is not None:
            params["atime"] = atime
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
        no_block: bool = False,
    ) -> dict:
        """Manage system services."""
        params: dict = {"name": name, "manager": manager}
        if state is not None:
            params["state"] = state
        if enabled is not None:
            params["enabled"] = enabled
        if no_block:
            params["no_block"] = True
        return self.call("Service", params)
