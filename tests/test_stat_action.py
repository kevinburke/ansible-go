"""Unit tests for the stat action override (plugins/action/stat.py).

The expected values in the parsing tests are ansible.builtin.stat's
observed output for the same `file` / `lsattr` output (captured by putting
stand-in programs first on PATH via the task's `environment:`).
tests/test_stat_differential.py compares the whole plugin against the stock
module end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from plugins.action import stat as stat_action  # type: ignore[import-untyped]
    from plugins.module_utils.fastagent_client import FastAgentError
    _ANSIBLE_IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself (or its PyYAML dependency) is not
    # installed. Any other import failure, such as a syntax error in the
    # plugin, must fail the run rather than silently skip every test.
    if exc.name is None or exc.name.split(".")[0] not in ("ansible", "yaml"):
        raise
    _ANSIBLE_IMPORT_ERROR = exc


def _requires_ansible(cls):
    return unittest.skipIf(
        _ANSIBLE_IMPORT_ERROR is not None,
        f"ansible is required to run action plugin tests: {_ANSIBLE_IMPORT_ERROR}",
    )(cls)


# A Builtin Stat RPC result for a plain Linux file, as the agent sends it
# (false booleans and zero integers omitted).
RPC_FILE = {
    "exists": True,
    "path": "/tmp/probe/plain.txt",
    "isreg": True,
    "mode": "0644",
    "owner": "kevin",
    "group": "kevin",
    "uid": 501,
    "gid": 1000,
    "size": 6,
    "inode": 131479,
    "dev": 65025,
    "nlink": 1,
    "mtime": 1577963045,
    "mtime_nsec": 123456789,
    "atime": 1577963045,
    "atime_nsec": 123456789,
    "ctime": 1790626375,
    "ctime_nsec": 788496000,
    "rusr": True,
    "wusr": True,
    "rgrp": True,
    "roth": True,
    "readable": True,
    "writeable": True,
    "checksum": "f572d396fae9206628714fb2ce00f72e94f2258f",
    "platform": {"blocks": 8, "block_size": 4096, "device_type": 0},
    "file_cmd": {"rc": 0, "stdout": "/tmp/probe/plain.txt: text/plain; charset=us-ascii\n"},
    "lsattr_cmd": {"rc": 0, "stdout": "666209463 --------------e------- /tmp/probe/plain.txt\n"},
}

# ansible.builtin.stat's result for the same file on ansible-core 2.21.
STOCK_FILE = {
    "atime": 1577963045.1234567,
    "attr_flags": "e",
    "attributes": ["extents"],
    "block_size": 4096,
    "blocks": 8,
    "charset": "us-ascii",
    "checksum": "f572d396fae9206628714fb2ce00f72e94f2258f",
    "ctime": 1790626375.788496,
    "dev": 65025,
    "device_type": 0,
    "disk_usage_bytes": 4096,
    "executable": False,
    "exists": True,
    "gid": 1000,
    "gr_name": "kevin",
    "inode": 131479,
    "isblk": False,
    "ischr": False,
    "isdir": False,
    "isfifo": False,
    "isgid": False,
    "islnk": False,
    "isreg": True,
    "issock": False,
    "isuid": False,
    "mimetype": "text/plain",
    "mode": "0644",
    "mtime": 1577963045.1234567,
    "nlink": 1,
    "path": "/tmp/probe/plain.txt",
    "pw_name": "kevin",
    "readable": True,
    "rgrp": True,
    "roth": True,
    "rusr": True,
    "size": 6,
    "uid": 501,
    "version": "666209463",
    "wgrp": False,
    "woth": False,
    "writeable": True,
    "wusr": True,
    "xgrp": False,
    "xoth": False,
    "xusr": False,
}

DEFAULT_PARAMS = {
    "path": "/tmp/probe/plain.txt",
    "follow": False,
    "get_checksum": True,
    "get_mime": True,
    "get_attributes": True,
    "get_selinux_context": False,
    "checksum_algorithm": "sha1",
}


def _find_ansible_doc():
    sibling = os.path.join(os.path.dirname(sys.executable), "ansible-doc")
    if os.path.exists(sibling):
        return sibling
    return shutil.which("ansible-doc")


@_requires_ansible
class TestArgumentSpecMatchesStock(unittest.TestCase):
    def test_spec_matches_ansible_doc(self):
        """ARGUMENT_SPEC must list exactly what the installed stock module documents."""
        ansible_doc = _find_ansible_doc()
        if ansible_doc is None:
            self.skipTest("ansible-doc not found")
        out = subprocess.run(
            [ansible_doc, "--json", "ansible.builtin.stat"],
            check=True, capture_output=True, text=True,
        ).stdout
        options = json.loads(out)["ansible.builtin.stat"]["doc"]["options"]
        self.assertEqual(set(options), set(stat_action.ARGUMENT_SPEC))
        for name, doc in options.items():
            with self.subTest(option=name):
                spec = stat_action.ARGUMENT_SPEC[name]
                self.assertEqual(spec["type"], doc.get("type", "str"))
                self.assertEqual(spec.get("default"), doc.get("default"))
                self.assertEqual(sorted(spec.get("aliases", [])), sorted(doc.get("aliases", [])))
                self.assertEqual(spec.get("choices"), doc.get("choices"))
                self.assertEqual(spec.get("required", False), doc.get("required", False))


@_requires_ansible
class TestValidateArgs(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            stat_action.validate_args({"path": "/tmp/probe/plain.txt"}),
            DEFAULT_PARAMS,
        )

    def test_aliases_and_boolean_strings(self):
        params = stat_action.validate_args({
            "name": "~/x",
            "checksum_algo": "sha224",
            "mime": "no",
            "attr": "off",
            "follow": "yes",
            "get_checksum": 0,
        })
        self.assertEqual(params["path"], "~/x")  # expanded on the target, not here
        self.assertEqual(params["checksum_algorithm"], "sha224")
        self.assertIs(params["get_mime"], False)
        self.assertIs(params["get_attributes"], False)
        self.assertIs(params["follow"], True)
        self.assertIs(params["get_checksum"], False)

        for args, key, want in (
            ({"dest": "/p"}, "path", "/p"),
            ({"path": "/p", "mime_type": False}, "get_mime", False),
            ({"path": "/p", "mime-type": False}, "get_mime", False),
            ({"path": "/p", "attributes": False}, "get_attributes", False),
            ({"path": "/p", "checksum": "sha256"}, "checksum_algorithm", "sha256"),
        ):
            with self.subTest(args=args):
                self.assertEqual(stat_action.validate_args(args)[key], want)

    def test_stock_errors_and_warnings_are_left_to_stock(self):
        for args in (
            {},  # missing required path
            {"path": "/p", "bogus": 1},  # unsupported parameter
            {"path": "/p", "checksum_algorithm": "crc32"},
            {"path": "/p", "checksum_algorithm": "SHA256"},
            {"path": "/p", "follow": "maybe"},
            {"path": "/p", "name": "/q"},  # stock warns about the alias
            {"path": "/p", "get_mime": True, "mime": False},
            {"path": 12345},
            {"path": None},
            {"path": ""},
            {"path": "/a\x00b"},
        ):
            with self.subTest(args=args):
                with self.assertRaises(stat_action.Unsupported):
                    stat_action.validate_args(args)


@_requires_ansible
class TestParseMime(unittest.TestCase):
    P = "/tmp/probe/plain.txt"

    def test_observed_stock_parsing(self):
        cases = [
            (f"{self.P}: text/plain; charset=us-ascii\n", 0, "text/plain", "us-ascii"),
            # Only the text after the last ':' counts.
            (f"{self.P}: a:b; charset=c\n", 0, "b", "c"),
            ("/tmp/colon:name: text/plain; charset=us-ascii\n", 0, "text/plain", "us-ascii"),
            (f"{self.P}: text/plain; charset=x\nsecond: line; charset=y\n", 0, "line", "y"),
            ("text/plain; charset=us-ascii", 0, "unknown", "unknown"),
            (f"{self.P}: text/plain; charset=us-ascii\n", 1, "unknown", "unknown"),
            (f"{self.P}: text/plain; charset=us-ascii; extra\n", 0, "unknown", "unknown"),
            (f"{self.P}: text/plain; charset=a=b\n", 0, "text/plain", "a"),
            # The mime type survives a charset that does not parse.
            (f"{self.P}: text/plain; nocharset\n", 0, "text/plain", "unknown"),
            (f"{self.P}:text/plain;charset=x", 0, "text/plain", "x"),
            ("", 0, "unknown", "unknown"),
            (f"{self.P}: \t text/plain \t;  charset = x  \n", 0, "text/plain", "x"),
            # What `file` prints for a dangling symlink.
            (f"{self.P}: inode/symlink\n", 0, "unknown", "unknown"),
            (f"{self.P}: regular file, no read permission\n", 0, "unknown", "unknown"),
        ]
        for stdout, rc, mimetype, charset in cases:
            with self.subTest(stdout=stdout, rc=rc):
                self.assertEqual(
                    stat_action.parse_mime({"rc": rc, "stdout": stdout}),
                    {"mimetype": mimetype, "charset": charset},
                )

    def test_file_not_found(self):
        self.assertEqual(
            stat_action.parse_mime(None),
            {"mimetype": "unknown", "charset": "unknown"},
        )


@_requires_ansible
class TestParseAttributes(unittest.TestCase):
    P = "/tmp/probe/plain.txt"

    def test_observed_stock_parsing(self):
        cases = [
            (f"123 --i-- {self.P}\n", 0, "123", "i", ["immutable"]),
            (f"123 --i-- {self.P}\n", 1, None, "", []),
            ("", 0, None, "", []),
            # get_file_attributes sets version before failing on the flags.
            ("onlyone", 0, "onlyone", "", []),
            (f"77 ---------------- {self.P}\n", 0, "77", "", []),
            (f"5 -Z-q-i-a- {self.P}\n", 0, "5", "Zqia", ["compresseddirty", "immutable", "append"]),
            ("  9   s-u  \n", 0, "9", "su", ["zero", "undelete"]),
        ]
        for stdout, rc, version, flags, attributes in cases:
            with self.subTest(stdout=stdout, rc=rc):
                self.assertEqual(
                    stat_action.parse_attributes({"rc": rc, "stdout": stdout}, self.P),
                    {"version": version, "attr_flags": flags, "attributes": attributes},
                )

    def test_lsattr_not_found(self):
        self.assertEqual(
            stat_action.parse_attributes(None, self.P),
            {"version": None, "attr_flags": "", "attributes": []},
        )

    def test_command_mismatch_is_unsupported(self):
        # If ansible-core ever asks for a different lsattr invocation than
        # the agent ran, the captured output must not be reused.
        orig = stat_action.AnsibleModule.get_file_attributes

        def other_flags(self, path, include_version=True):
            return orig(self, path + "-other", include_version)

        with patch.object(stat_action.AnsibleModule, "get_file_attributes", other_flags):
            with self.assertRaises(stat_action.Unsupported):
                stat_action.parse_attributes({"rc": 0, "stdout": "1 -e- x"}, self.P)


@_requires_ansible
class TestBuildStat(unittest.TestCase):
    def build(self, rpc=None, version=(2, 21), **params):
        return stat_action.build_stat(
            dict(RPC_FILE if rpc is None else rpc), dict(DEFAULT_PARAMS, **params), version
        )

    def test_regular_file_matches_stock(self):
        self.assertEqual(self.build(), STOCK_FILE)

    def test_disk_usage_bytes_only_on_2_21_and_newer(self):
        self.assertNotIn("disk_usage_bytes", self.build(version=(2, 20)))
        self.assertEqual(self.build(version=(2, 22))["disk_usage_bytes"], 4096)

    def test_missing(self):
        self.assertEqual(
            self.build({"exists": False, "path": "/tmp/probe/nope"}),
            {"exists": False},
        )

    def test_mode_keeps_special_bits_and_four_digits(self):
        for agent, stock in (("04755", "4755"), ("01777", "1777"), ("02750", "2750"),
                             ("00", "0000"), ("0600", "0600"), ("07", "0007")):
            with self.subTest(agent=agent):
                self.assertEqual(self.build(dict(RPC_FILE, mode=agent))["mode"], stock)

    def test_unresolvable_owner_and_group_are_omitted(self):
        rpc = dict(RPC_FILE, owner="", group="", uid=12345, gid=23456)
        st = self.build(rpc)
        self.assertNotIn("pw_name", st)
        self.assertNotIn("gr_name", st)
        self.assertEqual((st["uid"], st["gid"]), (12345, 23456))

    def test_symlink(self):
        rpc = dict(RPC_FILE, isreg=False, islnk=True, mode="0777",
                   lnk_target="missing", lnk_source="/tmp/probe/missing")
        rpc.pop("checksum")
        st = self.build(rpc)
        self.assertEqual(st["lnk_target"], "missing")
        self.assertEqual(st["lnk_source"], "/tmp/probe/missing")
        self.assertNotIn("checksum", st)
        rpc.pop("lnk_target")
        with self.assertRaises(stat_action.Unsupported):
            self.build(rpc)

    def test_checksum_rules(self):
        self.assertNotIn("checksum", self.build(get_checksum=False))
        unreadable = dict(RPC_FILE, readable=False)
        unreadable.pop("checksum")
        self.assertNotIn("checksum", self.build(unreadable))
        # A readable regular file without a checksum means the agent and
        # this plugin disagree about when to compute it.
        no_checksum = dict(RPC_FILE)
        no_checksum.pop("checksum")
        with self.assertRaises(stat_action.Unsupported):
            self.build(no_checksum)

    def test_optional_groups(self):
        st = self.build(get_mime=False, get_attributes=False)
        for key in ("mimetype", "charset", "version", "attr_flags", "attributes"):
            self.assertNotIn(key, st)

    def test_darwin_platform_fields(self):
        rpc = dict(RPC_FILE, platform={
            "blocks": 8, "block_size": 4096, "device_type": 0, "flags": 0,
            "generation": 0, "birthtime_sec": 1790626654, "birthtime_nsec": 515003400,
        })
        st = self.build(rpc)
        self.assertEqual(st["birthtime"], 1790626654 + 515003400 * 1e-9)
        self.assertEqual((st["flags"], st["generation"]), (0, 0))

    def test_unknown_platform_is_unsupported(self):
        for platform in (None, {"blocks": 1, "st_new_field": 2}):
            with self.subTest(platform=platform):
                with self.assertRaises(stat_action.Unsupported):
                    self.build(dict(RPC_FILE, platform=platform))


class _FakeClient:
    def __init__(self, result=None, exc=None):
        self.result = RPC_FILE if result is None else result
        self.exc = exc
        self.calls = []

    def stat(self, path, **kwargs):
        self.calls.append(dict(kwargs, path=path))
        if self.exc is not None:
            raise self.exc
        return dict(self.result)


class _FakeShell:
    tmpdir = None

    def env_prefix(self, **kwargs):
        return ""


class _FakeConnection:
    transport = "fastagent"

    def __init__(self, client, become_user=None, become=None, transport="fastagent"):
        self._agent_client = client
        self._become_user = become_user
        self.become = become
        self.transport = transport
        self._shell = _FakeShell()

    def _connect(self):
        return self

    def get_become_user(self):
        return self._become_user


class _FakeTask:
    def __init__(self, args, environment=None, async_val=0, check_mode=False):
        self.args = args
        self.environment = environment
        self.async_val = async_val
        self.check_mode = check_mode
        self.action = "stat"


class _IdentityTemplar:
    def template(self, value):
        return value


def _make_action(connection, args, **task_kwargs):
    action = stat_action.ActionModule.__new__(stat_action.ActionModule)
    action._task = _FakeTask(args, **task_kwargs)
    action._connection = connection
    action._templar = _IdentityTemplar()
    action.builtin_calls = []

    def fake_execute_module(module_name=None, task_vars=None, wrap_async=False, **kw):
        action.builtin_calls.append({"module_name": module_name, "wrap_async": wrap_async})
        return {"changed": False, "stat": {"exists": True, "from": "builtin"}}

    action._execute_module = fake_execute_module
    action._remove_tmp_path = lambda path: None
    return action


@_requires_ansible
class TestRun(unittest.TestCase):
    ARGS = {"path": "/tmp/probe/plain.txt"}

    def assertFellBack(self, action, result):
        self.assertEqual(action.builtin_calls[-1]["module_name"], "ansible.builtin.stat")
        self.assertEqual(result["stat"], {"exists": True, "from": "builtin"})

    def test_fast_path(self):
        client = _FakeClient()
        action = _make_action(_FakeConnection(client), dict(self.ARGS))
        with patch.object(stat_action, "_ANSIBLE_VERSION", "2.21.4"):
            result = action.run(task_vars={})
        self.assertEqual(action.builtin_calls, [])
        self.assertEqual(result, {"changed": False, "stat": STOCK_FILE})
        self.assertEqual(client.calls, [{
            "path": "/tmp/probe/plain.txt", "follow": False, "checksum": True,
            "checksum_algorithm": "sha1", "builtin": True, "mime": True,
            "attributes": True, "env": {},
        }])

    def test_agent_handled_sudo_stays_on_fast_path(self):
        # The connection attaches sudo (become_user root) as a wrapper the
        # agent handles; that must not count as a become Ansible applies.
        client = _FakeClient()
        become = types.SimpleNamespace(fastagent_handles_become=True)
        action = _make_action(_FakeConnection(client, become=become), dict(self.ARGS))
        with patch.object(stat_action, "_ANSIBLE_VERSION", "2.21.4"):
            result = action.run(task_vars={})
        self.assertEqual(action.builtin_calls, [])
        self.assertEqual(result, {"changed": False, "stat": STOCK_FILE})

    def test_rpc_params_follow_task_args_and_environment(self):
        client = _FakeClient(dict(RPC_FILE, path="/home/u/x"))
        action = _make_action(
            _FakeConnection(client),
            {"name": "$HOME/x", "follow": True, "checksum": "md5", "mime": False,
             "get_attributes": "no"},
            environment=[{"A": "1"}, {"B": 2, "A": "3"}],
        )
        result = action.run(task_vars={})
        self.assertEqual(action.builtin_calls, [])
        self.assertEqual(result["stat"]["path"], "/home/u/x")
        self.assertEqual(client.calls, [{
            "path": "$HOME/x", "follow": True, "checksum": True,
            "checksum_algorithm": "md5", "builtin": True, "mime": False,
            "attributes": False, "env": {"A": "3", "B": "2"},
        }])

    def test_check_mode_answers_the_same(self):
        action = _make_action(_FakeConnection(_FakeClient()), dict(self.ARGS), check_mode=True)
        result = action.run(task_vars={})
        self.assertEqual(action.builtin_calls, [])
        self.assertIs(result["changed"], False)
        self.assertNotIn("diff", result)

    def test_stat_error_fails_like_stock(self):
        client = _FakeClient({"path": "/root/x", "strerror": "Permission denied"})
        action = _make_action(_FakeConnection(client), {"path": "/root/x"})
        self.assertEqual(
            action.run(task_vars={}),
            {"changed": False, "failed": True, "msg": "Permission denied"},
        )

    def test_missing_path(self):
        client = _FakeClient({"exists": False, "path": "/nope"})
        action = _make_action(_FakeConnection(client), {"path": "/nope"})
        self.assertEqual(action.run(task_vars={}), {"changed": False, "stat": {"exists": False}})

    def test_fallbacks(self):
        cases = {
            "ssh connection": dict(connection=dict(transport="ssh")),
            "non-root become_user": dict(connection=dict(become_user="app")),
            "unsupported become method": dict(connection=dict(become=object())),
            "async": dict(task=dict(async_val=10)),
            "selinux": dict(args={"path": "/p", "get_selinux_context": True}),
            "invalid args": dict(args={"path": "/p", "bogus": True}),
            "relative path": dict(rpc={"exists": True, "path": "rel"}),
            "relative missing path": dict(rpc={"exists": False, "path": ""}),
            "agent error": dict(exc=FastAgentError(1, "checksum failed")),
            "transport error": dict(exc=IOError("gone")),
            "invalid utf-8 from file": dict(rpc=dict(RPC_FILE, file_cmd={
                "rc": 0, "stdout": "/p: text/pl�in; charset=x"})),
            "unknown platform": dict(rpc=dict(RPC_FILE, platform=None)),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                client = _FakeClient(case.get("rpc"), case.get("exc"))
                conn = _FakeConnection(client, **case.get("connection", {}))
                action = _make_action(conn, case.get("args", dict(self.ARGS)), **case.get("task", {}))
                with patch.object(stat_action.display, "warning") as warning:
                    result = action.run(task_vars={})
                self.assertFellBack(action, result)
                self.assertEqual(warning.called, name == "transport error")
                if name == "async":
                    self.assertEqual(action.builtin_calls[-1]["wrap_async"], 10)

    def test_old_ansible_falls_back(self):
        action = _make_action(_FakeConnection(_FakeClient()), dict(self.ARGS))
        with patch.object(stat_action, "_ANSIBLE_VERSION", "2.19.3"):
            result = action.run(task_vars={})
        self.assertFellBack(action, result)

    def test_version_parsing(self):
        self.assertEqual(stat_action.ansible_version_tuple("2.21.4"), (2, 21))
        self.assertEqual(stat_action.ansible_version_tuple("2.22.0.dev0"), (2, 22))
        self.assertEqual(stat_action.ansible_version_tuple("garbage"), (0, 0))


if __name__ == "__main__":
    unittest.main()
