"""Differential tests: copy/template `validate` on the fast path against stock.

Each case gets its own directory holding the destination's starting state.
The validator is a probe script that records its arguments and the mode and
content of each file argument, then exits with a status given on its command
line. Stock and fastagent run the same cases in separate trees, and the test
requires the same task result (for the keys validation decides), the same
probe record, and the same files afterwards.

TestLocalDifferential always runs when Go and ansible-playbook are available.
It starts the Go agent with --serve and drives plugins/action/copy.py against
it directly, and runs stock with `ansible-playbook -c local`.

TestRemoteDifferential runs both through real connections against a test
host, with the same environment variables as tests/test_stat_differential.py
(FASTAGENT_TEST_SSH_HOST, _PORT, _USER, _KEY, FASTAGENT_TEST_BECOME_USER).
The agent binary on the host must match this checkout: the connection plugin
only redeploys when the version string changes, so after changing Go code,
build it, remove the host's copy, and point FASTAGENT_TEST_AGENT_DIR at the
directory holding the build (for example tmp/, with
fastagent-linux-<arch> in it). Otherwise the plugin uploads the release
binary from ~/.ansible/fastagent/.

What stock does was established by running these cases against
ansible.builtin.copy; no ansible-core source was consulted. Known
differences outside validation (the checksum algorithm, the extra stat keys
stock returns, `diff` headers, creating missing parent directories) are
excluded by comparing only VALIDATE_KEYS and are documented in
docs/compatibility.md.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    import yaml
    from ansible.plugins.action import ActionBase

    from plugins.action import copy as copy_action  # type: ignore[import-untyped]
    from plugins.module_utils.fastagent_client import FastAgentClient
    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself (or its PyYAML dependency) is not
    # installed. Any other import failure, such as a syntax error in the
    # plugin, must fail the run rather than silently skip every test.
    if exc.name is None or exc.name.split(".")[0] not in ("ansible", "yaml"):
        raise
    _IMPORT_ERROR = exc

# The result keys validation decides. `failed` is compared separately
# because a registered stock result always carries it.
VALIDATE_KEYS = ("changed", "msg", "exit_status", "rc", "cmd", "stdout", "stderr",
                 "stdout_lines", "stderr_lines")

PROBE = """#!/bin/sh
# probe LOG RC ARG...: record the working directory, each ARG, and the mode
# and content of each ARG that is a file, to LOG; print a line on stdout
# and stderr; exit RC.
log=$1; rc=$2; shift 2
echo "cwd=$(pwd)" >> "$log"
for a in "$@"; do
  echo "arg=$a" >> "$log"
  if [ -f "$a" ]; then
    echo "mode=$(ls -l "$a" | cut -c1-10)" >> "$log"
    echo "content=$(cat "$a")" >> "$log"
  fi
done
echo "probe stdout"
echo "probe stderr" >&2
exit "$rc"
"""


def _probe(d, rc=0, extra=""):
    return f"{{bin}}/probe {d}/log {rc} {extra}%s".replace("{bin}", "{root}/bin")


# dir is the case's own directory; files is its starting state.
OLD = {"f": ("old\n", 0o644)}
CASES = [
    dict(name="pass", args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}")}, files=OLD),
    dict(name="fail", args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}", 3)}, files=OLD),
    dict(name="fail_backup", files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "backup": True, "validate": _probe("{dir}", 3)}),
    dict(name="unchanged_not_validated", files=OLD,
         args={"content": "old\n", "dest": "{dir}/f", "validate": _probe("{dir}", 3)}),
    dict(name="mode_only_not_validated", files=OLD,
         args={"content": "old\n", "dest": "{dir}/f", "mode": "0600", "validate": _probe("{dir}", 3)}),
    dict(name="new_file", args={"content": "new\n", "dest": "{dir}/g", "validate": _probe("{dir}")}),
    dict(name="new_file_fail", args={"content": "new\n", "dest": "{dir}/g", "validate": _probe("{dir}", 1)}),
    dict(name="check_mode", check_mode=True, files=OLD, write=False,
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}", 3)}),
    dict(name="diff_fail", diff=True, files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}", 3)}),
    dict(name="validator_sees_mode", files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "mode": "0640", "validate": _probe("{dir}")}),
    dict(name="keeps_extension", args={"content": "new\n", "dest": "{dir}/x.tar.gz", "validate": _probe("{dir}")}),
    dict(name="keeps_existing_mode", files={"f": ("old\n", 0o751)},
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}")}),
    dict(name="quoting_and_expansion", files=OLD, environment={"FA_VALIDATE_VAR": "expanded"},
         args={"content": "new\n", "dest": "{dir}/f",
               "validate": _probe("{dir}", 0, "'a b' \"c d\" $FA_VALIDATE_VAR --file=")}),
    dict(name="shell_metacharacters_are_arguments", files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}", 0, "| true ; ") }),
    dict(name="path_lookup_uses_task_environment", files=OLD,
         environment={"PATH": "{root}/bin:/usr/bin:/bin"},
         args={"content": "new\n", "dest": "{dir}/f", "validate": "probe {dir}/log 0 %s"}),
    dict(name="validator_not_found", files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "validate": "/nonexistent/validator %s"}),
    dict(name="validator_killed", files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "validate": "/bin/sh -c 'kill -9 $$' %s"}),
    dict(name="src_file", files=OLD, src_content="from src\n",
         args={"src": "{dir}.src", "dest": "{dir}/f", "validate": _probe("{dir}")}),
    dict(name="empty_validate", files=OLD, args={"content": "new\n", "dest": "{dir}/f", "validate": ""}),
    # Forms stock rejects with errors of its own: the fast path hands them
    # to ansible.builtin.copy.
    dict(name="no_placeholder", files=OLD, fallback=True,
         args={"content": "new\n", "dest": "{dir}/f", "validate": "{root}/bin/probe {dir}/log 0"}),
    dict(name="two_placeholders", files=OLD, fallback=True,
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}", 0, "%s ")}),
    dict(name="escaped_placeholder", files=OLD, fallback=True,
         args={"content": "new\n", "dest": "{dir}/f", "validate": "{root}/bin/probe {dir}/log 0 %%s"}),
    dict(name="unbalanced_quote", files=OLD, fallback=True,
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}", 0, "'abc ")}),
]

# Cases that need a real remote host with sudo.
REMOTE_CASES = [
    dict(name="become_root_keeps_owner", become=True, files=OLD,
         args={"content": "new\n", "dest": "{dir}/f", "validate": _probe("{dir}")}),
    dict(name="become_new_file", become=True,
         args={"content": "new\n", "dest": "{dir}/g", "validate": _probe("{dir}")}),
    dict(name="become_owner_mode_applied_before_validation", become=True, files=OLD, needs_user=True,
         args={"content": "new\n", "dest": "{dir}/f", "owner": "{become_user}", "group": "{become_user}",
               "mode": "0600", "validate": _probe("{dir}")}),
    dict(name="template", files=OLD, template="rendered {{ 1 + 1 }}\n",
         args={"src": "{dir}.j2", "dest": "{dir}/f", "validate": _probe("{dir}")}),
    dict(name="template_fail", files=OLD, template="rendered {{ 1 + 1 }}\n",
         args={"src": "{dir}.j2", "dest": "{dir}/f", "validate": _probe("{dir}", 3)}),
]


def _sub(value, subs):
    if isinstance(value, str):
        for key, repl in subs.items():
            value = value.replace("{" + key + "}", repl)
        return value
    if isinstance(value, dict):
        return {k: _sub(v, subs) for k, v in value.items()}
    return value


def _case_dir(root, case):
    return os.path.join(root, "cases", case["name"])


def _materialize(case, root, become_user=""):
    subs = {"dir": _case_dir(root, case), "root": root, "become_user": become_user}
    out = dict(case)
    out["args"] = _sub(case["args"], subs)
    if "environment" in case:
        out["environment"] = _sub(case["environment"], subs)
    return out


def _fixture_script(root, cases):
    """Shell script that builds root: bin/probe and each case's files."""
    lines = ["set -e", f"mkdir -p {shlex.quote(root)}/bin",
             f"cat > {shlex.quote(root)}/bin/probe <<'PROBE'\n{PROBE}PROBE",
             f"chmod 755 {shlex.quote(root)}/bin/probe"]
    for case in cases:
        d = _case_dir(root, case)
        lines.append(f"mkdir -p {shlex.quote(d)}")
        for name, (content, mode) in case.get("files", {}).items():
            path = shlex.quote(os.path.join(d, name))
            lines.append(f"printf %s {shlex.quote(content)} > {path}; chmod {mode:o} {path}")
        if "src_content" in case:
            lines.append(f"printf %s {shlex.quote(case['src_content'])} > {shlex.quote(d + '.src')}")
        if "template" in case:
            lines.append(f"printf %s {shlex.quote(case['template'])} > {shlex.quote(d + '.j2')}")
    return "\n".join(lines) + "\n"


# Summarize each case directory: every file's mode and content (the probe
# log verbatim), with backup names normalized.
_STATE_SCRIPT = r"""
import json, os, re, stat, sys
root = sys.argv[1]
out = {}
for name in sorted(os.listdir(os.path.join(root, "cases"))):
    d = os.path.join(root, "cases", name)
    entries = {}
    for dirpath, dirnames, filenames in os.walk(d):
        for f in sorted(filenames):
            p = os.path.join(dirpath, f)
            rel = re.sub(r"\.\d+\.\d{4}-\d\d-\d\d@\d\d:\d\d:\d\d~$|\.\d{14}~$", ".<BACKUP>~",
                         os.path.relpath(p, d))
            st = os.lstat(p)
            try:
                with open(p, errors="replace") as fh:
                    content = fh.read()
            except OSError as e:
                content = "<unreadable: %s>" % e.strerror
            entries[rel] = {"mode": oct(stat.S_IMODE(st.st_mode)), "content": content}
    out[name] = entries
print(json.dumps(out))
"""

_TMP_SOURCE = re.compile(r"/\S*?/(\.source[^\s/]*)")


def _normalize(value, root):
    """Replace the tree root and the validated temporary file's directory."""
    if isinstance(value, str):
        return _TMP_SOURCE.sub(r"<TMP>/\1", value.replace(root, "<ROOT>"))
    if isinstance(value, list):
        return [_normalize(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v, root) for k, v in value.items()}
    return value


def _ansible_bin(name):
    sibling = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(name)


def _playbook(cases, hosts, out_dir, module):
    tasks = []
    for i, case in enumerate(cases):
        mod = "template" if "template" in case else module
        task = {"name": case["name"], mod: case["args"], "register": f"r{i}", "ignore_errors": True}
        for key in ("become", "environment", "check_mode", "diff"):
            if key in case:
                task[key] = case[key]
        tasks.append(task)
    content = "{{ {%s} | to_json }}" % ", ".join(
        f"{json.dumps(c['name'])}: r{i}" for i, c in enumerate(cases))
    tasks.append({
        "name": "dump results", "delegate_to": "localhost", "connection": "local",
        "become": False,
        "ansible.builtin.copy": {"content": content,
                                 "dest": os.path.join(out_dir, "{{ inventory_hostname }}.json")},
    })
    return [{"hosts": hosts, "gather_facts": False, "tasks": tasks}]


def _run_playbook(workdir, playbook, env):
    path = os.path.join(workdir, "playbook.yml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(playbook, f, sort_keys=False)
    proc = subprocess.run([_ansible_bin("ansible-playbook"), path], cwd=workdir, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise AssertionError(f"ansible-playbook failed ({proc.returncode}):\n"
                             f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}")


def _probe_view(case, files):
    """Drop the validated file's mode from the probe log unless the task set
    one. Without `mode`, stock validates the file as Ansible uploaded it
    (0700 for content, 0744 for a template: artifacts of the upload, not of
    validation); with `mode`, both apply it before validating.
    """
    if "mode" in case["args"] or "log" not in files:
        return files
    log = files["log"]
    kept = [line for line in log["content"].splitlines(True) if not line.startswith("mode=")]
    return dict(files, log=dict(log, content="".join(kept)))


class _DiffMixin:
    maxDiff = None

    def assertSame(self, cases, stock, fast, stock_root, fast_root):
        """stock/fast: {"results": {name: result}, "state": {name: files}}."""
        diffs = []
        for case in cases:
            name = case["name"]
            if case.get("fallback"):
                continue
            want = {k: v for k, v in stock["results"][name].items() if k in VALIDATE_KEYS}
            got = {k: v for k, v in fast["results"][name].items() if k in VALIDATE_KEYS}
            want["failed"] = stock["results"][name].get("failed", False)
            got["failed"] = fast["results"][name].get("failed", False)
            want = _normalize({"result": want, "files": _probe_view(case, stock["state"][name])}, stock_root)
            got = _normalize({"result": got, "files": _probe_view(case, fast["state"][name])}, fast_root)
            if want != got:
                diffs.append(f"{name}:\n  stock:     {json.dumps(want, sort_keys=True)}\n"
                             f"  fastagent: {json.dumps(got, sort_keys=True)}")
        self.assertEqual(diffs, [], "fastagent differs from ansible.builtin.copy:\n" + "\n".join(diffs))


def _local_agent_binary(tmpdir):
    go = shutil.which("go")
    if go is None:
        raise unittest.SkipTest("go is not installed")
    binary = os.path.join(tmpdir, "fastagent")
    subprocess.run([go, "build", "-trimpath", "-o", binary, "./cmd/fastagent"],
                   cwd=REPO_ROOT, check=True, capture_output=True)
    return binary


class _LocalConnection:
    transport = "fastagent"
    become = None

    def __init__(self, client):
        self._agent_client = client

    def _connect(self):
        return self

    def get_become_user(self):
        return None


class _LocalTask:
    def __init__(self, case):
        self.args = case["args"]
        self.async_val = 0


class _LocalPlayContext:
    def __init__(self, case):
        self.check_mode = case.get("check_mode", False)
        self.diff = case.get("diff", False)


class _LocalLoader:
    def get_real_file(self, path, decrypt=True):
        return path


class _Fallback(Exception):
    pass


def _local_state(root):
    proc = subprocess.run([sys.executable, "-c", _STATE_SCRIPT, root],
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


@unittest.skipIf(_IMPORT_ERROR is not None, f"ansible is required: {_IMPORT_ERROR}")
class TestLocalDifferential(_DiffMixin, unittest.TestCase):
    """The plugin plus a local agent against stock, on this machine."""

    def test_matches_stock(self):
        if _ansible_bin("ansible-playbook") is None:
            self.skipTest("ansible-playbook not found")
        with tempfile.TemporaryDirectory(prefix="fastagent-validate-diff-") as tmp:
            self._run(tmp)

    def _run(self, tmp):
        binary = _local_agent_binary(tmp)
        stock_root, fast_root = os.path.join(tmp, "stock"), os.path.join(tmp, "fast")
        stock_cases = [_materialize(c, stock_root) for c in CASES]
        fast_cases = [_materialize(c, fast_root) for c in CASES]
        for root, cases in ((stock_root, stock_cases), (fast_root, fast_cases)):
            subprocess.run(["sh", "-c", _fixture_script(root, cases)], check=True)

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
        _run_playbook(tmp, _playbook(stock_cases, "localhost", tmp, "ansible.builtin.copy"), env)
        with open(os.path.join(tmp, "localhost.json"), encoding="utf-8") as f:
            stock = {"results": json.load(f), "state": _local_state(stock_root)}

        # Stock's module runs in the playbook's directory here; on a real
        # host both it and the daemon start in the SSH user's home.
        proc = subprocess.Popen([binary, "--serve"], cwd=tmp, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            client = FastAgentClient(proc.stdin, proc.stdout)
            results = {case["name"]: self._run_action(client, case) for case in fast_cases}
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
        fast = {"results": results, "state": _local_state(fast_root)}

        for case in fast_cases:
            self.assertEqual(results[case["name"]] is _Fallback, bool(case.get("fallback")),
                             f"{case['name']}: fallback to ansible.builtin.copy")
        self.assertSame(fast_cases, stock, fast, stock_root, fast_root)

    def _run_action(self, client, case):
        action = copy_action.ActionModule.__new__(copy_action.ActionModule)
        action._task = _LocalTask(case)
        action._connection = _LocalConnection(client)
        action._play_context = _LocalPlayContext(case)
        action._loader = _LocalLoader()
        action._find_needle = lambda _dirname, needle: needle
        action._task_environment = lambda: dict(case.get("environment", {}))

        def fallback(*args, **kwargs):
            raise _Fallback()

        action._run_builtin_copy = fallback
        # A fresh dict per call, as ActionBase.run returns.
        with patch.object(ActionBase, "run", side_effect=lambda *a, **k: {}):
            try:
                return action.run(task_vars={})
            except _Fallback:
                return _Fallback


@unittest.skipIf(_IMPORT_ERROR is not None, f"ansible is required: {_IMPORT_ERROR}")
@unittest.skipUnless(os.environ.get("FASTAGENT_TEST_SSH_HOST"), "FASTAGENT_TEST_SSH_HOST is not set")
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
        base = f"/tmp/fastagent-validate-diff-{uuid.uuid4().hex[:12]}"
        try:
            with tempfile.TemporaryDirectory(prefix="fastagent-validate-diff-") as tmp:
                self._run(tmp, base)
        finally:
            self._ssh(f"sudo -n rm -rf {shlex.quote(base)}")

    def _run(self, tmp, base):
        coll = os.path.join(tmp, "collections", "ansible_collections", "kevinburke")
        os.makedirs(coll)
        os.symlink(REPO_ROOT, os.path.join(coll, "fastagent"))
        with open(os.path.join(tmp, "ansible.cfg"), "w", encoding="utf-8") as f:
            f.write("[defaults]\n"
                    f"inventory = {tmp}/inventory\n"
                    f"collections_path = {tmp}/collections\n"
                    f"action_plugins = {coll}/fastagent/plugins/action\n"
                    "host_key_checking = False\n"
                    "[ssh_connection]\n"
                    "pipelining = True\n"
                    f"ssh_args = {' '.join(self.ssh_opts)}\n")
        host_vars = f"ansible_host={self.host} ansible_port={self.port}"
        if self.user:
            host_vars += f" ansible_user={self.user}"
        if self.key:
            host_vars += f" ansible_ssh_private_key_file={self.key}"
        agent_dir = os.environ.get("FASTAGENT_TEST_AGENT_DIR")
        if agent_dir:
            host_vars += f" fastagent_local_agent_dir={os.path.abspath(agent_dir)}"
        roots = {"stock": f"{base}/stock", "fastagent": f"{base}/fastagent"}
        with open(os.path.join(tmp, "inventory"), "w", encoding="utf-8") as f:
            f.write(f"fastagent {host_vars} ansible_connection=kevinburke.fastagent.fastagent"
                    f" case_root={roots['fastagent']}\n"
                    f"stock {host_vars} ansible_connection=ssh case_root={roots['stock']}\n"
                    "[all:vars]\nansible_python_interpreter=/usr/bin/python3\n")

        cases = [c for c in CASES + REMOTE_CASES if self.become_user or not c.get("needs_user")]
        # One task list for both hosts: each host's paths come from its
        # case_root variable.
        cases = [_materialize(c, "{{ case_root }}", self.become_user or "") for c in cases]
        for root in roots.values():
            self._ssh("sh -s", stdin=_fixture_script(root, [_materialize(c, root) for c in cases]))
        # copy's src (without remote_src) and template's src are read on the
        # controller, so the fixture script's copies on the host are not
        # the ones used. Write them here, shared by both hosts.
        for c in cases:
            for key in ("template", "src_content"):
                if key in c:
                    src = c["args"]["src"].replace("{{ case_root }}", tmp)
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    with open(src, "w", encoding="utf-8") as f:
                        f.write(c[key])
                    c["args"] = dict(c["args"], src=src)

        trace = os.path.join(tmp, "trace.tsv")
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"), FASTAGENT_TRACE=trace)
        # Unqualified `copy` so the fastagent host goes through the override.
        _run_playbook(tmp, _playbook(cases, "all", tmp, "copy"), env)
        runs = {}
        for host, root in roots.items():
            with open(os.path.join(tmp, f"{host}.json"), encoding="utf-8") as f:
                results = json.load(f)
            state = json.loads(self._ssh(f"sudo -n python3 - {shlex.quote(root)}",
                                         stdin=_STATE_SCRIPT).stdout)
            runs[host] = {"results": results, "state": state}
        # A missing controller-side source fails the same way on both hosts,
        # so the comparison alone would pass without testing anything.
        for host, run in runs.items():
            for name, result in run["results"].items():
                self.assertNotIn("Could not find or access", str(result.get("msg", "")),
                                 f"{host} {name}: a source file was not set up")
        self.assertSame(cases, runs["stock"], runs["fastagent"], roots["stock"], roots["fastagent"])

        # Every case that writes on the fast path must have used WriteFile:
        # a silent fallback to the builtin would pass the comparison above.
        with open(trace, encoding="utf-8") as f:
            written = {line.split("\t")[3].rstrip("\n") for line in f
                       if line.split("\t")[1] == "WriteFile"}
        expect = [c for c in cases if not c.get("fallback") and c.get("write", True)
                  and "not_validated" not in c["name"]]
        missing = [c["name"] for c in expect
                   if c["args"]["dest"].replace("{{ case_root }}", roots["fastagent"]) not in written]
        self.assertEqual(missing, [], "these cases did not use the WriteFile RPC")


if __name__ == "__main__":
    unittest.main()
