"""Differential tests: the fastagent stat override against ansible.builtin.stat.

Both halves build the same fixture tree (regular, empty, binary, setuid,
unreadable and oddly named files, directories, FIFOs, sockets, symlinks of
every kind, plus stand-in `file` and `lsattr` programs), run the same list of
stat tasks through stock and through fastagent, and require identical results.

TestLocalDifferential always runs when Go and ansible-playbook are available.
It starts the Go agent with --serve and drives plugins/action/stat.py against
it directly, and runs stock with `ansible-playbook -c local` on the same
machine, as the same user.

TestRemoteDifferential runs both through real connections against a test
host. It is skipped unless FASTAGENT_TEST_SSH_HOST is set:

    FASTAGENT_TEST_SSH_HOST     host to connect to (required)
    FASTAGENT_TEST_SSH_PORT     SSH port (default 22)
    FASTAGENT_TEST_SSH_USER     SSH user (default: ssh's default)
    FASTAGENT_TEST_SSH_KEY      private key file (optional)
    FASTAGENT_TEST_BECOME_USER  a non-root user for become_user fallback cases
                                (optional)

The host needs Python 3 and passwordless sudo, and must be disposable: the
test creates and removes a directory under /tmp and briefly sets the
immutable attribute on a file in it. The agent binary must already be
available the way the connection plugin finds it (see docs/testing.md).
"""

from __future__ import annotations

import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    import yaml

    from plugins.action import stat as stat_action  # type: ignore[import-untyped]
    from plugins.module_utils.fastagent_client import FastAgentClient
    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself (or its PyYAML dependency) is not
    # installed. Any other import failure, such as a syntax error in the
    # plugin, must fail the run rather than silently skip every test.
    if exc.name is None or exc.name.split(".")[0] not in ("ansible", "yaml"):
        raise
    _IMPORT_ERROR = exc


def build_fixture(root, privileged):
    """Create the fixture tree under root. Runs locally and on the test host,
    so it must be self-contained and use only the standard library."""
    import os
    import socket
    import subprocess
    import time

    def write(name, data, mode=0o644):
        path = os.path.join(root, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return path

    os.makedirs(os.path.join(root, "dir"))
    os.makedirs(os.path.join(root, "sticky"))
    os.chmod(os.path.join(root, "sticky"), 0o1777)
    os.makedirs(os.path.join(root, "fakebin"))
    write("plain.txt", b"hello\n")
    write("empty", b"")
    write("bin.dat", b"\x00\x01\x02\xff\xfebinary")
    write("utf8.txt", "utf8 é\n".encode())
    write("big.bin", bytes(range(256)) * 400)
    write("exec.sh", b"#!/bin/sh\n", 0o4755)
    write("noread", b"secret\n", 0o000)
    write("colon:name.txt", b"x\n")
    write("semi; name.txt", b"x\n")
    write("fake: textx; charset=y", b"x\n")
    write("ünïcode.txt", b"x\n")
    write("sp ace.txt", b"x\n")
    for name, var in (("file", "FAKE_FILE"), ("lsattr", "FAKE_LSATTR")):
        write(
            os.path.join("fakebin", name),
            ('#!/bin/sh\nprintf "%%s" "$%s_OUT"\nexit "${%s_RC:-0}"\n' % (var, var)).encode(),
            0o755,
        )
    os.symlink("plain.txt", os.path.join(root, "link_rel"))
    os.symlink(os.path.join(root, "plain.txt"), os.path.join(root, "link_abs"))
    os.symlink("missing-target", os.path.join(root, "dangling"))
    os.symlink("dir", os.path.join(root, "link_dir"))
    os.symlink("link_rel", os.path.join(root, "link_chain"))
    os.symlink("loop2", os.path.join(root, "loop1"))
    os.symlink("loop1", os.path.join(root, "loop2"))
    os.mkfifo(os.path.join(root, "fifo"))
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(os.path.join(root, "sock"))
    sock.close()
    if privileged:
        write("nobody_owned", b"x\n")
        write("immut", b"x\n")
    # Access times one hour ahead are newer than mtime and ctime and less
    # than a day old, so relatime never bumps them when either side reads
    # a file; atimes then compare equal. chown and chattr below leave
    # atime alone.
    future = int(time.time()) + 3600
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            os.utime(path, ns=(future * 10**9, st.st_mtime_ns), follow_symlinks=False)
    if privileged:
        subprocess.run(["sudo", "-n", "chown", "12345:23456",
                        os.path.join(root, "nobody_owned")], check=True)
        subprocess.run(["sudo", "-n", "chattr", "+i", os.path.join(root, "immut")], check=True)
        subprocess.run(["sudo", "-n", "sh", "-c",
                        'mkdir -m 700 "$1/rootonly" && echo x > "$1/rootonly/f" && '
                        'touch -a -d "@$2" "$1/rootonly/f"', "sh", root, str(future)],
                       check=True)


# Stat tasks to compare. `{root}` is replaced by the fixture directory.
# volatile: stat fields that legitimately change between the two runs.
# local / remote: False to skip the case in that half.
# fast: False for cases that must fall back to ansible.builtin.stat.
# privileged: needs the fixture's sudo-created files.
_P = "{root}/plain.txt"


def _fake_file(out, rc=0):
    return {"PATH": "{root}/fakebin:/usr/bin:/bin", "FAKE_FILE_OUT": out, "FAKE_FILE_RC": str(rc)}


def _fake_lsattr(out, rc=0):
    return {"PATH": "{root}/fakebin:/usr/bin:/bin", "FAKE_LSATTR_OUT": out,
            "FAKE_LSATTR_RC": str(rc)}


CASES = [
    {"name": "plain", "args": {"path": _P}},
    {"name": "missing", "args": {"path": "{root}/nope"}},
    {"name": "empty", "args": {"path": "{root}/empty"}},
    {"name": "binary", "args": {"path": "{root}/bin.dat"}},
    {"name": "utf8", "args": {"path": "{root}/utf8.txt"}},
    {"name": "big_sha384", "args": {"path": "{root}/big.bin", "checksum_algorithm": "sha384"}},
    {"name": "setuid", "args": {"path": "{root}/exec.sh"}},
    {"name": "noread", "args": {"path": "{root}/noread"}},
    {"name": "dir", "args": {"path": "{root}/dir"}},
    {"name": "dir_slash", "args": {"path": "{root}/dir/"}},
    {"name": "sticky", "args": {"path": "{root}/sticky"}},
    {"name": "fifo", "args": {"path": "{root}/fifo"}},
    {"name": "socket", "args": {"path": "{root}/sock"}},
    {"name": "devnull", "args": {"path": "/dev/null"}, "volatile": ["atime", "mtime", "ctime"]},
    {"name": "colon", "args": {"path": "{root}/colon:name.txt"}},
    {"name": "semicolon", "args": {"path": "{root}/semi; name.txt"}},
    {"name": "fake_mime_in_name", "args": {"path": "{root}/fake: textx; charset=y"}},
    {"name": "unicode", "args": {"path": "{root}/ünïcode.txt"}},
    {"name": "space", "args": {"path": "{root}/sp ace.txt"}},
    {"name": "dotdot", "args": {"path": "{root}/dir/../plain.txt"}},
    {"name": "link_rel", "args": {"path": "{root}/link_rel"}},
    {"name": "link_rel_follow", "args": {"path": "{root}/link_rel", "follow": True}},
    {"name": "link_abs", "args": {"path": "{root}/link_abs"}},
    {"name": "link_chain", "args": {"path": "{root}/link_chain"}},
    {"name": "link_chain_follow", "args": {"path": "{root}/link_chain", "follow": True}},
    {"name": "link_dir_follow", "args": {"path": "{root}/link_dir", "follow": True}},
    {"name": "link_dir_slash", "args": {"path": "{root}/link_dir/"}},
    {"name": "dangling", "args": {"path": "{root}/dangling"}},
    {"name": "dangling_follow", "args": {"path": "{root}/dangling", "follow": True}},
    {"name": "loop", "args": {"path": "{root}/loop1"}},
    {"name": "loop_follow", "args": {"path": "{root}/loop1", "follow": True}},
    {"name": "enotdir", "args": {"path": "{root}/plain.txt/x"}},
    {"name": "md5", "args": {"path": _P, "checksum_algorithm": "md5"}},
    {"name": "sha224", "args": {"path": _P, "checksum_algorithm": "sha224"}},
    {"name": "sha256", "args": {"path": _P, "checksum_algorithm": "sha256"}},
    {"name": "sha512_alias", "args": {"path": _P, "checksum": "sha512"}},
    {"name": "aliases", "args": {"name": _P, "checksum_algo": "sha224", "mime": "no", "attr": "no"}},
    {"name": "alias_dest", "args": {"dest": _P, "mime_type": False, "attributes": False}},
    {"name": "alias_mime_dash", "args": {"path": _P, "mime-type": False, "get_checksum": "no"}},
    {"name": "nothing_extra", "args": {"path": _P, "get_checksum": False, "get_mime": False,
                                       "get_attributes": False}},
    {"name": "tilde", "args": {"path": "~"}, "volatile": ["atime", "mtime", "ctime", "nlink",
                                                          "size", "blocks", "disk_usage_bytes"]},
    {"name": "env_var", "args": {"path": "$STAT_ROOT/plain.txt"},
     "environment": {"STAT_ROOT": "{root}"}},
    {"name": "brace_var", "args": {"path": "${STAT_ROOT}/dir"},
     "environment": {"STAT_ROOT": "{root}"}},
    {"name": "unset_var", "args": {"path": "{root}/$STAT_NO_SUCH_VAR/x"}},
    {"name": "no_tools_on_path", "args": {"path": _P}, "environment": {"PATH": "/nonexistent"}},
    {"name": "check_mode", "args": {"path": _P}, "check_mode": True, "diff": True},
    # `file` output parsing, through a stand-in `file`.
    {"name": "file_base", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: text/plain; charset=us-ascii\n")},
    {"name": "file_colon_in_mime", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: a:b; charset=c\n")},
    {"name": "file_multiline", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: text/plain; charset=x\nsecond: line; charset=y\n")},
    {"name": "file_rc1", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: text/plain; charset=us-ascii\n", 1)},
    {"name": "file_three_parts", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: text/plain; charset=a; b\n")},
    {"name": "file_double_equals", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: text/plain; charset=a=b\n")},
    {"name": "file_no_charset", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("{root}/plain.txt: text/plain; nocharset\n")},
    {"name": "file_no_colon", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("text/plain; charset=us-ascii")},
    {"name": "file_empty", "args": {"path": _P, "get_attributes": False},
     "environment": _fake_file("")},
    # `lsattr` output parsing, through a stand-in `lsattr`.
    {"name": "lsattr_base", "args": {"path": _P, "get_mime": False},
     "environment": _fake_lsattr("123 --i-- {root}/plain.txt\n")},
    {"name": "lsattr_rc1", "args": {"path": _P, "get_mime": False},
     "environment": _fake_lsattr("123 --i-- {root}/plain.txt\n", 1)},
    {"name": "lsattr_one_field", "args": {"path": _P, "get_mime": False},
     "environment": _fake_lsattr("onlyone")},
    {"name": "lsattr_unknown_flags", "args": {"path": _P, "get_mime": False},
     "environment": _fake_lsattr("5 -Z-q-i-a- {root}/plain.txt\n")},
    # Cases that fall back to stock; only meaningful through a connection.
    # A relative path makes the RPC, then falls back (rpc: True).
    {"name": "relative", "args": {"path": "."}, "local": False, "fast": False, "rpc": True,
     "volatile": ["atime", "mtime", "ctime", "nlink", "size", "blocks", "disk_usage_bytes"]},
    {"name": "selinux", "args": {"path": _P, "get_selinux_context": True}, "local": False,
     "fast": False},
    {"name": "bad_algorithm", "args": {"path": _P, "checksum_algorithm": "crc32"},
     "local": False, "fast": False},
    {"name": "unknown_arg", "args": {"path": _P, "bogus": 1}, "local": False, "fast": False},
    {"name": "no_path", "args": {}, "local": False, "fast": False},
    {"name": "path_and_alias", "args": {"path": _P, "name": "{root}/empty"}, "local": False,
     "fast": False},
    # Privileged fixture files; remote only.
    {"name": "unknown_owner", "args": {"path": "{root}/nobody_owned"}, "local": False,
     "privileged": True},
    {"name": "immutable", "args": {"path": "{root}/immut"}, "local": False, "privileged": True},
    {"name": "permission_denied", "args": {"path": "{root}/rootonly/f"}, "local": False,
     "privileged": True},
    {"name": "become_root", "args": {"path": "{root}/rootonly/f"}, "become": True,
     "local": False, "privileged": True},
    {"name": "become_root_tilde", "args": {"path": "~"}, "become": True, "local": False,
     "volatile": ["atime", "mtime", "ctime", "nlink", "size", "blocks", "disk_usage_bytes"]},
    {"name": "become_user", "args": {"path": _P}, "become": True,
     "become_user": "{become_user}", "local": False, "fast": False},
]


def _substitute(value, subs):
    if isinstance(value, str):
        for key, repl in subs.items():
            value = value.replace("{" + key + "}", repl)
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, subs) for k, v in value.items()}
    return value


def _materialize(case, subs):
    return {k: _substitute(v, subs) for k, v in case.items()}


def _playbook(cases, hosts, out_dir, module="ansible.builtin.stat"):
    tasks = []
    for i, case in enumerate(cases):
        task = {"name": case["name"], module: case["args"], "register": f"r{i}",
                "ignore_errors": True}
        for key in ("become", "become_user", "environment", "check_mode", "diff"):
            if key in case:
                task[key] = case[key]
        tasks.append(task)
    content = "{{ {%s} | to_json }}" % ", ".join(
        f"{json.dumps(c['name'])}: r{i}" for i, c in enumerate(cases)
    )
    tasks.append({
        "name": "dump results",
        "delegate_to": "localhost",
        "connection": "local",
        "become": False,
        "ansible.builtin.copy": {
            "content": content,
            "dest": os.path.join(out_dir, "{{ inventory_hostname }}.json"),
        },
    })
    return [{"hosts": hosts, "gather_facts": False, "tasks": tasks}]


def _ansible_bin(name):
    sibling = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(name)


def _run_playbook(workdir, playbook, env):
    path = os.path.join(workdir, "playbook.yml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(playbook, f, sort_keys=False)
    proc = subprocess.run(
        [_ansible_bin("ansible-playbook"), path],
        cwd=workdir, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"ansible-playbook failed ({proc.returncode}):\n"
            f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
        )


def _strip(result, volatile):
    result = dict(result)
    if isinstance(result.get("stat"), dict):
        result["stat"] = {k: v for k, v in result["stat"].items() if k not in volatile}
    return result


class _DiffMixin:
    maxDiff = None

    def assertSameResults(self, cases, stock, fast, keys=None):
        diffs = []
        for case in cases:
            name = case["name"]
            want = _strip(stock[name], case.get("volatile", ()))
            got = _strip(fast[name], case.get("volatile", ()))
            if keys is not None:
                want = {k: want[k] for k in keys if k in want}
                got = {k: got[k] for k in keys if k in got}
            if want != got:
                diffs.append(f"{name}:\n  stock:     {json.dumps(want, sort_keys=True)}\n"
                             f"  fastagent: {json.dumps(got, sort_keys=True)}")
        self.assertEqual(diffs, [], "fastagent differs from ansible.builtin.stat:\n"
                         + "\n".join(diffs))


def _local_agent_binary(tmpdir):
    go = shutil.which("go")
    if go is None:
        raise unittest.SkipTest("go is not installed")
    binary = os.path.join(tmpdir, "fastagent")
    subprocess.run([go, "build", "-trimpath", "-o", binary, "./cmd/fastagent"],
                   cwd=REPO_ROOT, check=True, capture_output=True)
    return binary


class _LocalShell:
    tmpdir = None

    def env_prefix(self, **kwargs):
        return ""


class _LocalConnection:
    transport = "fastagent"
    become = None

    def __init__(self, client):
        self._agent_client = client
        self._shell = _LocalShell()

    def _connect(self):
        return self

    def get_become_user(self):
        return None


class _LocalTask:
    def __init__(self, case):
        self.args = case["args"]
        self.environment = case.get("environment")
        self.async_val = 0
        self.check_mode = case.get("check_mode", False)
        self.action = "stat"


class _IdentityTemplar:
    def template(self, value):
        return value


# AF_UNIX socket paths are limited to 104 bytes on macOS (108 on Linux), and
# the fixture binds <root>/sock.
_MAX_SOCKET_PATH = 100


def _short_fixture_dir():
    """Create a fixture directory whose <root>/sock path fits AF_UNIX.

    macOS's $TMPDIR is too long for that, and some CI sandboxes make /tmp
    read-only, so try the system temp dir, /tmp, then the repo's tmp/, and
    use the first that is short enough and writable.
    """
    repo_tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp")
    tried = []
    for base in (tempfile.gettempdir(), "/tmp", repo_tmp):
        # mkdtemp adds "fa-stat-" plus 8 random characters.
        if len(os.path.join(base, "fa-stat-XXXXXXXX", "sock")) > _MAX_SOCKET_PATH:
            tried.append(f"{base}: path too long")
            continue
        try:
            os.makedirs(base, exist_ok=True)
            return tempfile.mkdtemp(prefix="fa-stat-", dir=base)
        except OSError as e:
            tried.append(f"{base}: {e}")
    raise RuntimeError("no usable fixture directory: " + "; ".join(tried))


@unittest.skipIf(_IMPORT_ERROR is not None, f"ansible is required: {_IMPORT_ERROR}")
class TestLocalDifferential(_DiffMixin, unittest.TestCase):
    """The plugin plus a local agent against stock, on this machine."""

    def test_matches_stock(self):
        if _ansible_bin("ansible-playbook") is None:
            self.skipTest("ansible-playbook not found")
        with tempfile.TemporaryDirectory(prefix="fastagent-stat-diff-") as tmp:
            root = _short_fixture_dir()
            try:
                self._run(tmp, root)
            finally:
                subprocess.run(["chmod", "-R", "u+rwx", root], check=False)
                shutil.rmtree(root, ignore_errors=True)

    def _run(self, tmp, root):
        binary = _local_agent_binary(tmp)
        build_fixture(root, privileged=False)
        cases = [_materialize(c, {"root": root}) for c in CASES if c.get("local", True)]

        # Keep Ansible's state out of ~/.ansible, which CI sandboxes may
        # make read-only.
        cfg = os.path.join(tmp, "ansible.cfg")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("[defaults]\n"
                    f"remote_tmp = {os.path.join(tmp, 'remote-tmp')}\n"
                    f"local_tmp = {os.path.join(tmp, 'local-tmp')}\n")
        with open(os.path.join(tmp, "inventory"), "w", encoding="utf-8") as f:
            f.write(f"localhost ansible_connection=local "
                    f"ansible_python_interpreter={sys.executable}\n")
        env = dict(os.environ, ANSIBLE_CONFIG=cfg,
                   ANSIBLE_HOME=os.path.join(tmp, "ansible-home"),
                   ANSIBLE_INVENTORY=os.path.join(tmp, "inventory"))
        _run_playbook(tmp, _playbook(cases, "localhost", tmp), env)
        with open(os.path.join(tmp, "localhost.json"), encoding="utf-8") as f:
            stock = json.load(f)

        proc = subprocess.Popen([binary, "--serve"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            client = FastAgentClient(proc.stdin, proc.stdout)
            fast = {case["name"]: self._run_action(client, case) for case in cases}
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()

        # The controller adds `failed`/`exception` to registered results;
        # compare what the plugin itself decides.
        self.assertSameResults(cases, stock, fast, keys=("changed", "stat", "msg"))
        for case in cases:
            self.assertEqual(stock[case["name"]].get("failed", False),
                             fast[case["name"]].get("failed", False), case["name"])

    def _run_action(self, client, case):
        action = stat_action.ActionModule.__new__(stat_action.ActionModule)
        action._task = _LocalTask(case)
        action._connection = _LocalConnection(client)
        action._templar = _IdentityTemplar()

        def no_fallback(*args, **kwargs):
            raise AssertionError(f"{case['name']}: fell back to ansible.builtin.stat")

        action._execute_module = no_fallback
        return action.run(task_vars={})


@unittest.skipIf(_IMPORT_ERROR is not None, f"ansible is required: {_IMPORT_ERROR}")
@unittest.skipUnless(os.environ.get("FASTAGENT_TEST_SSH_HOST"),
                     "FASTAGENT_TEST_SSH_HOST is not set")
class TestRemoteDifferential(_DiffMixin, unittest.TestCase):
    """fastagent and plain SSH connections to the same host."""

    def setUp(self):
        self.host = os.environ["FASTAGENT_TEST_SSH_HOST"]
        self.port = os.environ.get("FASTAGENT_TEST_SSH_PORT", "22")
        self.user = os.environ.get("FASTAGENT_TEST_SSH_USER")
        self.key = os.environ.get("FASTAGENT_TEST_SSH_KEY")
        self.become_user = os.environ.get("FASTAGENT_TEST_BECOME_USER")
        self.ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                         "-o", "LogLevel=ERROR", "-o", "BatchMode=yes"]
        if self.key:
            self.ssh_opts += ["-o", "IdentitiesOnly=yes", "-i", self.key]

    def _ssh(self, command, stdin=None):
        argv = ["ssh", *self.ssh_opts, "-p", self.port]
        if self.user:
            argv += ["-l", self.user]
        argv += [self.host, command]
        return subprocess.run(argv, input=stdin, capture_output=True, text=True,
                              timeout=120, check=True)

    def test_matches_stock(self):
        if _ansible_bin("ansible-playbook") is None:
            self.skipTest("ansible-playbook not found")
        root = f"/tmp/fastagent-stat-diff-{uuid.uuid4().hex[:12]}"
        script = (inspect.getsource(build_fixture)
                  + f"\nbuild_fixture({root!r}, privileged=True)\n")
        self._ssh("python3 -", stdin=script)
        try:
            with tempfile.TemporaryDirectory(prefix="fastagent-stat-diff-") as tmp:
                self._run(tmp, root)
        finally:
            self._ssh(f"sudo -n chattr -i {shlex.quote(root)}/immut; "
                      f"sudo -n rm -rf {shlex.quote(root)}")

    def _run(self, tmp, root):
        coll = os.path.join(tmp, "collections", "ansible_collections", "kevinburke")
        os.makedirs(coll)
        os.symlink(REPO_ROOT, os.path.join(coll, "fastagent"))
        with open(os.path.join(tmp, "ansible.cfg"), "w", encoding="utf-8") as f:
            f.write(
                "[defaults]\n"
                f"inventory = {tmp}/inventory\n"
                f"collections_path = {tmp}/collections\n"
                f"action_plugins = {coll}/fastagent/plugins/action\n"
                "host_key_checking = False\n"
                "[ssh_connection]\n"
                "pipelining = True\n"
                f"ssh_args = {' '.join(self.ssh_opts)}\n"
            )
        host_vars = f"ansible_host={self.host} ansible_port={self.port}"
        if self.user:
            host_vars += f" ansible_user={self.user}"
        if self.key:
            host_vars += f" ansible_ssh_private_key_file={self.key}"
        with open(os.path.join(tmp, "inventory"), "w", encoding="utf-8") as f:
            f.write(
                f"fastagent {host_vars} ansible_connection=kevinburke.fastagent.fastagent\n"
                f"stock {host_vars} ansible_connection=ssh\n"
                "[all:vars]\nansible_python_interpreter=/usr/bin/python3\n"
            )

        subs = {"root": root, "become_user": self.become_user or ""}
        cases = [_materialize(c, subs) for c in CASES
                 if c.get("remote", True) and (self.become_user or "become_user" not in c)]
        trace = os.path.join(tmp, "trace.tsv")
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                   FASTAGENT_TRACE=trace)
        # Unqualified `stat` so the fastagent host goes through the
        # override; on the ssh host it is the stock module either way.
        _run_playbook(tmp, _playbook(cases, "all", tmp, module="stat"), env)
        results = {}
        for host in ("stock", "fastagent"):
            with open(os.path.join(tmp, f"{host}.json"), encoding="utf-8") as f:
                results[host] = json.load(f)
        self.assertSameResults(cases, results["stock"], results["fastagent"])

        # The fast path must actually have answered the fast cases.
        with open(trace, encoding="utf-8") as f:
            stat_rpcs = [line.split("\t")[3].rstrip("\n") for line in f
                         if line.split("\t")[1] == "Stat"]
        fast_cases = [c for c in cases if c.get("fast", True)]
        missing = [c["name"] for c in fast_cases if c["args"].get("path",
                   c["args"].get("name", c["args"].get("dest"))) not in stat_rpcs]
        self.assertEqual(missing, [], "these cases did not use the Stat RPC")
        rpc_cases = [c for c in cases if c.get("rpc", c.get("fast", True))]
        self.assertEqual(len(stat_rpcs), len(rpc_cases),
                         "fallback cases must not use the Stat RPC")


if __name__ == "__main__":
    unittest.main()
