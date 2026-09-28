"""Differential test: the stat fast path against ansible.builtin.stat.

Runs the installed ansible-core's stat module and the fastagent stat
action (backed by a real agent process) against the same fixtures and
asserts the returned `stat` dicts are identical. Unit tests elsewhere pin
individual behaviors; this one catches any field the fast path adds,
drops or formats differently.

Both sides run on the local machine, so whatever `file` and `lsattr`
exist here are used by both.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_UTILS = os.path.join(REPO_ROOT, "plugins", "module_utils")
for p in (REPO_ROOT, MODULE_UTILS):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from ansible.plugins.action import ActionBase  # type: ignore[import-untyped]
    from plugins.action.stat import ActionModule  # type: ignore[import-untyped]
    from fastagent_client_test import AgentSession  # type: ignore[import-not-found]
    _ANSIBLE_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc


class _Connection:
    transport = "fastagent"

    def __init__(self, client):
        self._agent_client = client

    def _connect(self):
        return self

    def get_become_user(self):
        return None


class _IdentityTemplar:
    def template(self, value):
        return value


class _Task:
    def __init__(self, args, environment=None):
        self.args = args
        self.async_val = 0
        self.environment = environment or []


def _fast_stat(client, args, environment=None):
    action = ActionModule.__new__(ActionModule)
    action._task = _Task(args, environment)
    action._connection = _Connection(client)
    action._templar = _IdentityTemplar()

    def _execute_module(**kwargs):
        raise AssertionError(f"fast path fell back to the builtin module for {args}")

    action._execute_module = _execute_module
    with patch.object(ActionBase, "run", return_value={}):
        return action.run(task_vars={})


def _stock_stat(args, env=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"ANSIBLE_MODULE_ARGS": args}, f)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ansible.modules.stat", f.name],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        os.unlink(f.name)
    out = json.loads(proc.stdout)
    out.pop("invocation", None)
    return out


@contextlib.contextmanager
def _environ(**updates):
    old = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _make_fixtures(root: str) -> dict[str, str]:
    """Create one of each file type stat distinguishes; return name->path."""
    paths = {}

    def put(name, data=b"", mode=None):
        path = os.path.join(root, name)
        with open(path, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(path, mode)
        paths[name] = path
        return path

    put("text.txt", b"hello world\n")
    put("empty", b"")
    put("binary.bin", bytes(range(256)) * 8)
    put("script.sh", b"#!/bin/sh\necho hi\n", 0o755)
    put("small-mode", b"x", 0o007)
    put("setuid", b"x", 0o4755)
    put("unreadable", b"secret", 0o000)
    # `file` prints "<path>: <type>; charset=<enc>"; stock splits on the
    # last colon, so a colon in the name must not confuse the parser.
    put("colon: name.txt", b"plain text\n")
    put("utf8.txt", "café ☃\n".encode())
    put("latin1.txt", "café\n".encode("latin-1"))
    put("json.json", b'{"a": 1}\n')

    d = os.path.join(root, "dir")
    os.mkdir(d)
    paths["dir"] = d

    os.symlink("text.txt", os.path.join(root, "link-rel"))
    paths["link-rel"] = os.path.join(root, "link-rel")
    os.symlink(os.path.join(root, "dir"), os.path.join(root, "link-dir"))
    paths["link-dir"] = os.path.join(root, "link-dir")
    os.symlink("missing-target", os.path.join(root, "link-broken"))
    paths["link-broken"] = os.path.join(root, "link-broken")
    os.symlink("link-rel", os.path.join(root, "link-chain"))
    paths["link-chain"] = os.path.join(root, "link-chain")

    # realpath edge cases: loops, `..` after a symlinked directory, and a
    # dangling link inside a symlinked directory.
    os.symlink("link-loop-b", os.path.join(root, "link-loop-a"))
    os.symlink("link-loop-a", os.path.join(root, "link-loop-b"))
    paths["link-loop"] = os.path.join(root, "link-loop-a")
    os.symlink("../text.txt", os.path.join(d, "up"))
    paths["link-via-dir"] = os.path.join(root, "link-dir", "up")
    os.symlink("nowhere/deeper", os.path.join(d, "dangling"))
    paths["link-dangling-via-dir"] = os.path.join(root, "link-dir", "dangling")

    fifo = os.path.join(root, "fifo")
    os.mkfifo(fifo)
    paths["fifo"] = fifo

    paths["missing"] = os.path.join(root, "does-not-exist")
    paths["missing-parent"] = os.path.join(root, "text.txt", "child")

    _pin_atimes(paths.values())
    return paths


def _pin_atimes(paths):
    """Move atime after mtime so reads by either side (checksums, `file`)
    do not update atime under relatime and make the two runs disagree."""
    future = time.time() + 3600
    for path in paths:
        # Symlinks too: Linux updates a link's own atime on readlink.
        if os.path.lexists(path):
            st = os.lstat(path)
            os.utime(path, (future, st.st_mtime), follow_symlinks=False)


_OPTION_SETS = [
    {},
    {"follow": True},
    {"get_checksum": False},
    {"checksum_algorithm": "sha256"},
    {"get_mime": False},
    {"get_attributes": False},
    {"get_mime": False, "get_attributes": False},
]


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    f"ansible is required to run action plugin tests: {_ANSIBLE_IMPORT_ERROR}",
)
class TestStatMatchesBuiltin(unittest.TestCase):
    maxDiff = None

    def _compare(self, client, args, env=None, environment=None):
        stock = _stock_stat(args, env=env)
        fast = _fast_stat(client, args, environment)
        self.assertEqual(stock.get("failed", False), fast.get("failed", False),
                         msg=f"{args}\nstock={stock}\nfast={fast}")
        self.assertEqual(stock.get("msg"), fast.get("msg"), msg=str(args))
        s, f = stock.get("stat") or {}, fast.get("stat") or {}
        # One line per differing key keeps CI logs (which show only the
        # tail of the output) readable.
        diffs = {k: (s.get(k, "<absent>"), f.get(k, "<absent>"))
                 for k in sorted(set(s) | set(f)) if s.get(k, object()) != f.get(k, object())}
        self.assertFalse(diffs, msg=f"stock vs fast for {args}: {diffs}")
        self.assertEqual(stock.get("changed"), fast.get("changed"))

    def test_fixtures_and_options(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fastagent-stat-") as root, \
                AgentSession() as client:
            root = os.path.realpath(root)
            paths = _make_fixtures(root)
            for name, path in sorted(paths.items()):
                for opts in _OPTION_SETS:
                    with self.subTest(fixture=name, **opts):
                        self._compare(client, {"path": path, **opts})

    def test_path_expansion(self) -> None:
        # type='path' expands ~ and $VARS on the remote before stat runs.
        with tempfile.TemporaryDirectory(prefix="fastagent-home-") as home:
            home = os.path.realpath(home)
            with open(os.path.join(home, "f.txt"), "w") as f:
                f.write("x\n")
            _pin_atimes([os.path.join(home, "f.txt")])
            with _environ(HOME=home, FASTAGENT_TEST_DIR=home), \
                    AgentSession() as client:
                for path in ("~/f.txt", "$FASTAGENT_TEST_DIR/f.txt",
                             "${FASTAGENT_TEST_DIR}/f.txt", "$UNSET_FASTAGENT_VAR/f.txt"):
                    with self.subTest(path=path):
                        self._compare(client, {"path": path})

    def test_task_environment_reaches_path_lookup(self) -> None:
        # With PATH pointing only at a directory without `file` or
        # `lsattr`, stock reports mimetype "unknown"; so must we.
        with tempfile.TemporaryDirectory(prefix="fastagent-env-") as root:
            root = os.path.realpath(root)
            target = os.path.join(root, "f.txt")
            with open(target, "w") as f:
                f.write("x\n")
            _pin_atimes([target])
            empty_bin = os.path.join(root, "bin")
            os.mkdir(empty_bin)
            env = dict(os.environ, PATH=empty_bin)
            with AgentSession() as client:
                stock = _stock_stat({"path": target}, env=env)
                fast = _fast_stat(client, {"path": target},
                                  environment=[{"PATH": empty_bin}])
            self.assertEqual(stock["stat"]["mimetype"], "unknown")
            self.assertEqual(stock["stat"], fast["stat"])

    def test_attributes_path_was_exercised(self) -> None:
        # The fixture comparisons pass trivially when this host has no
        # `lsattr`, or its filesystem does not support attributes, since
        # both sides then report the defaults. Say so instead of passing.
        if shutil.which("lsattr") is None:
            self.skipTest("lsattr is not installed; attributes were only "
                          "compared as 'lsattr not found'")
        with tempfile.TemporaryDirectory(prefix="fastagent-attr-") as root:
            target = os.path.join(root, "f.txt")
            with open(target, "w") as f:
                f.write("x\n")
            _pin_atimes([target])
            stock = _stock_stat({"path": target})
            if stock["stat"]["version"] is None:
                self.skipTest(f"lsattr failed on {root}'s filesystem; "
                              "attributes were only compared as defaults")
            with AgentSession() as client:
                fast = _fast_stat(client, {"path": target})
        self.assertEqual(stock["stat"], fast["stat"])


if __name__ == "__main__":
    unittest.main()
