"""Differential tests: file state=touch on the fast path against stock.

Covers the "pre-create a log file" pattern (access_time and
modification_time set to preserve) and the "now" keyword, in normal and check
mode. Each case gets its own file; existing files start with mode 0640 and
both times set to OLD. Stock runs with `ansible-playbook -c local`, and the
fast path drives plugins/action/file.py against a local agent started with
--serve. The test requires the same `changed`, and the same file afterwards:
whether it exists, its mode, and whether each time is still OLD.

Runs when Go and ansible-playbook are available.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from ansible.plugins.action import ActionBase

    from plugins.action import file as file_action  # type: ignore[import-untyped]
    from plugins.module_utils.fastagent_client import FastAgentClient
    from tests.test_copy_validate_differential import (
        _ansible_bin,
        _local_agent_binary,
        _playbook,
        _run_playbook,
    )
    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself (or its PyYAML dependency) is not
    # installed. Any other import failure, such as a syntax error in the
    # plugin, must fail the run rather than silently skip every test.
    if exc.name is None or exc.name.split(".")[0] not in ("ansible", "yaml"):
        raise
    _IMPORT_ERROR = exc

OLD = 1577934245  # 2020-01-02 03:04:05 UTC

PRESERVE = {"access_time": "preserve", "modification_time": "preserve"}

# name, whether the file exists beforehand, extra task args, check mode.
CASES = [
    ("preserve missing", False, dict(PRESERVE, mode="0640"), False),
    ("preserve same mode", True, dict(PRESERVE, mode="0640"), False),
    ("preserve same owner", True, dict(PRESERVE, owner=getpass.getuser()), False),
    ("preserve new mode", True, dict(PRESERVE, mode="0600"), False),
    ("preserve no attrs", True, dict(PRESERVE), False),
    ("check preserve missing", False, dict(PRESERVE, mode="0640"), True),
    ("check preserve same mode", True, dict(PRESERVE, mode="0640"), True),
    ("check preserve new mode", True, dict(PRESERVE, mode="0600"), True),
    ("atime now mtime preserve", True, {"access_time": "now",
                                        "modification_time": "preserve"}, False),
    ("default times", True, {}, False),
    ("check default times", True, {}, True),
]


def _materialize(root):
    cases = []
    for i, (name, exists, extra, check) in enumerate(CASES):
        path = os.path.join(root, f"case{i}.log")
        if exists:
            with open(path, "w", encoding="utf-8") as f:
                f.write("keep me\n")
            os.chmod(path, 0o640)
            os.utime(path, (OLD, OLD))
        case = {"name": name, "args": dict(extra, path=path, state="touch")}
        if check:
            case["check_mode"] = True
        cases.append(case)
    return cases


def _file_state(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"exists": False}
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return {
        "exists": True,
        "mode": oct(st.st_mode & 0o7777),
        "atime_kept": int(st.st_atime) == OLD,
        "mtime_kept": int(st.st_mtime) == OLD,
        "content": content,
    }


class _Connection:
    transport = "fastagent"

    def __init__(self, client):
        self._agent_client = client

    def _connect(self):
        return self

    def get_become_user(self):
        return None


class _Task:
    def __init__(self, case):
        self.args = case["args"]
        self.async_val = 0


class _PlayContext:
    def __init__(self, case):
        self.check_mode = case.get("check_mode", False)
        self.diff = False


class _Fallback(Exception):
    pass


@unittest.skipIf(_IMPORT_ERROR is not None, f"ansible is required: {_IMPORT_ERROR}")
class TestLocalTouchDifferential(unittest.TestCase):
    maxDiff = None

    def test_matches_stock(self):
        if _ansible_bin("ansible-playbook") is None:
            self.skipTest("ansible-playbook not found")
        with tempfile.TemporaryDirectory(prefix="fastagent-touch-diff-") as tmp:
            self._run(tmp)

    def _run(self, tmp):
        binary = _local_agent_binary(tmp)
        stock_root, fast_root = os.path.join(tmp, "stock"), os.path.join(tmp, "fast")
        os.makedirs(stock_root)
        os.makedirs(fast_root)
        stock_cases = _materialize(stock_root)
        fast_cases = _materialize(fast_root)

        # Keep Ansible's state out of ~/.ansible, which CI sandboxes may
        # make read-only.
        cfg = os.path.join(tmp, "ansible.cfg")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("[defaults]\n"
                    f"remote_tmp = {os.path.join(tmp, 'remote-tmp')}\n"
                    f"local_tmp = {os.path.join(tmp, 'local-tmp')}\n")
        with open(os.path.join(tmp, "inventory"), "w", encoding="utf-8") as f:
            f.write(f"localhost ansible_connection=local ansible_python_interpreter={sys.executable}\n")
        env = dict(os.environ, ANSIBLE_CONFIG=cfg, ANSIBLE_HOME=os.path.join(tmp, "ansible-home"),
                   ANSIBLE_INVENTORY=os.path.join(tmp, "inventory"))
        _run_playbook(tmp, _playbook(stock_cases, "localhost", tmp, "ansible.builtin.file"), env)
        with open(os.path.join(tmp, "localhost.json"), encoding="utf-8") as f:
            stock_results = json.load(f)

        proc = subprocess.Popen([binary, "--serve"], cwd=tmp, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            client = FastAgentClient(proc.stdin, proc.stdout)
            fast_results = {c["name"]: self._run_action(client, c) for c in fast_cases}
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()

        for stock_case, fast_case in zip(stock_cases, fast_cases):
            name = stock_case["name"]
            with self.subTest(name):
                fast = fast_results[name]
                self.assertIsNot(fast, _Fallback, "fell back to ansible.builtin.file")
                stock = stock_results[name]
                self.assertFalse(stock.get("failed"), msg=stock)
                self.assertFalse(fast.get("failed"), msg=fast)
                self.assertEqual(fast.get("changed"), stock.get("changed"))
                self.assertEqual(_file_state(fast_case["args"]["path"]),
                                 _file_state(stock_case["args"]["path"]))

    def _run_action(self, client, case):
        action = file_action.ActionModule.__new__(file_action.ActionModule)
        action._task = _Task(case)
        action._connection = _Connection(client)
        action._play_context = _PlayContext(case)

        def fallback(*args, **kwargs):
            raise _Fallback()

        action._execute_module = fallback
        # A fresh dict per call, as ActionBase.run returns.
        with patch.object(ActionBase, "run", side_effect=lambda *a, **k: {}):
            try:
                return action.run(task_vars={})
            except _Fallback:
                return _Fallback


if __name__ == "__main__":
    unittest.main()
