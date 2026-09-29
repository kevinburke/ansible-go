"""Regression coverage for the copy action override's vault handling.

Prior to 0.6.1, the copy fast path read `src:` files directly with
`open(source, "rb")`, so a vault-encrypted source landed on the remote
as raw `$ANSIBLE_VAULT;1.1;AES256` ciphertext instead of the decrypted
secret. The fix routes the resolved source through
`self._loader.get_real_file(source, decrypt=True)` — the same API
ansible's builtin copy action uses — so encrypted sources are
decrypted into a temp file before being read.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from ansible.plugins.action import ActionBase  # type: ignore[import-untyped]
    from plugins.action.copy import ActionModule  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself is not installed. Any other import
    # failure, such as a syntax error in the plugin, must fail the run
    # rather than silently skip every test.
    if exc.name is None or exc.name.split(".")[0] != "ansible":
        raise
    _ANSIBLE_IMPORT_ERROR = exc


class _RecordingAgentClient:
    def __init__(self):
        # Every RPC, as (method, path), in order.
        self.calls: list[tuple[str, str]] = []
        # The last WriteFile that wrote (not one answered with dest_is_dir).
        self.write_kwargs: dict | None = None
        self.write_result: dict = {"changed": True, "checksum": ""}
        self.file_calls: list[dict] = []
        self.stat_results: list[dict] = []
        # Remote directories, which WriteFile with report_dir reports as
        # the agent does.
        self.dirs: set[str] = set()
        self.read_file_error: Exception | None = None

    def stat(self, path, follow, checksum, checksum_algorithm=None):
        self.calls.append(("Stat", path))
        if self.stat_results:
            return self.stat_results.pop(0)
        return {"exists": False, "isdir": False}

    def read_file(self, path):
        self.calls.append(("ReadFile", path))
        if self.read_file_error is not None:
            raise self.read_file_error
        return {"content": base64.b64encode(b"old").decode("ascii")}

    def write_file(self, **kwargs):
        self.calls.append(("WriteFile", kwargs["dest"]))
        if kwargs["dest"] in self.dirs:
            if not kwargs.get("report_dir"):
                raise AssertionError(f"WriteFile to directory {kwargs['dest']}")
            return {"changed": False, "dest": kwargs["dest"], "dest_is_dir": True}
        self.write_kwargs = kwargs
        return self.write_result

    def file(self, **kwargs):
        self.calls.append(("File", kwargs["path"]))
        self.file_calls.append(kwargs)
        return {"changed": True}


class _FakeConnection:
    transport = "fastagent"

    def __init__(self):
        self._agent_client = _RecordingAgentClient()

    def _connect(self):
        return self

    def get_become_user(self):
        return None


class _RecordingLoader:
    """Stand-in for ansible's DataLoader.

    Captures get_real_file calls so tests can assert decrypt=True was
    passed, and returns a caller-supplied replacement path so tests can
    verify the copy action reads *that* file (the decrypted one),
    not the original.
    """

    def __init__(self, resolved_path):
        self._resolved_path = resolved_path
        self.calls: list[tuple[str, bool]] = []

    def get_real_file(self, file_path, decrypt=True):
        self.calls.append((file_path, decrypt))
        return self._resolved_path


class _FakeTask:
    def __init__(self, args):
        self.args = args
        self.async_val = 0


class _FakePlayContext:
    check_mode = False
    diff = False


def _make_action(*, task_args, loader):
    action = ActionModule.__new__(ActionModule)
    action._task = _FakeTask(task_args)
    action._connection = _FakeConnection()
    action._play_context = _FakePlayContext()
    action._loader = loader
    action._supports_async = False
    action._supports_check_mode = True
    # _find_needle resolves src against the playbook's files/ search path.
    # For the test the src we pass in is already the absolute path we want
    # the action to stat/read, so short-circuit the search.
    action._find_needle = lambda _dirname, needle: needle
    return action


@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestCopyActionVaultDecrypt(unittest.TestCase):
    def test_vault_source_is_decrypted_before_remote_write(self) -> None:
        # Simulates the on-disk state: `source` looks like a vault file
        # (has the ANSIBLE_VAULT header), and the DataLoader resolves it
        # to a separate temp file containing the decrypted plaintext.
        with tempfile.TemporaryDirectory() as tmp:
            ciphertext_path = os.path.join(tmp, "RATGDO_KEY")
            with open(ciphertext_path, "wb") as f:
                f.write(b"$ANSIBLE_VAULT;1.1;AES256\n6165...\n")

            plaintext_path = os.path.join(tmp, "RATGDO_KEY.decrypted")
            plaintext_bytes = b"supersecretkeymaterial"
            with open(plaintext_path, "wb") as f:
                f.write(plaintext_bytes)

            loader = _RecordingLoader(resolved_path=plaintext_path)
            action = _make_action(
                task_args={
                    "src": ciphertext_path,
                    "dest": "/etc/garage-control-env/RATGDO_KEY",
                    "mode": "0640",
                },
                loader=loader,
            )

            with patch.object(ActionBase, "run", return_value={}):
                result = action.run(task_vars={})

            self.assertFalse(result.get("failed"), msg=result)

            # The loader's decrypt path must have been used (not a raw
            # `open()` on the ciphertext).
            self.assertEqual(loader.calls, [(ciphertext_path, True)])

            # What actually got shipped to the remote must be the
            # decrypted plaintext, not the vault header.
            write_kwargs = action._connection._agent_client.write_kwargs
            self.assertIsNotNone(write_kwargs)
            shipped = base64.b64decode(write_kwargs["content"])
            self.assertEqual(shipped, plaintext_bytes)
            self.assertNotIn(b"ANSIBLE_VAULT", shipped)

    def test_directory_src_delegates_to_builtin_action_plugin(self) -> None:
        # Regression for two layered bugs:
        #
        #   * 0.6.2 and earlier: a directory `src:` fell through to
        #     `_execute_module("ansible.builtin.copy")`, which runs the
        #     *module* on the remote with our controller-side path —
        #     "Source /Users/.../migrations/ not found".
        #
        #   * 0.6.3: we switched the fallback to
        #     `action_loader.get("ansible.legacy.copy")`. That works in
        #     stock ansible (legacy → builtin), but caracal-server's
        #     `ansible.cfg` puts this plugin on the legacy `action_plugins`
        #     path to shadow unqualified `copy:`. Under that config,
        #     "ansible.legacy.copy" resolves *back into us* and the
        #     fallback recurses forever — "maximum recursion depth exceeded".
        #
        # The fix imports the builtin copy action by module path and
        # instantiates it directly, so neither shadowing nor loader
        # aliasing can redirect the fallback back at ourselves.
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = os.path.join(tmp, "migrations")
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, "0001_init.sql"), "wb") as f:
                f.write(b"-- init\n")

            delegated_calls: list[dict] = []

            class _FakeBuiltin:
                def __init__(self, **kwargs):
                    self.init_kwargs = kwargs

                def _execute_module(self, **_kwargs):
                    return {"changed": False}

                def run(self, task_vars):
                    delegated_calls.append(
                        {"task_vars": task_vars, "init_kwargs": self.init_kwargs}
                    )
                    return {"changed": True, "dest": "/remote/migrations/"}

            # Any action_loader access in the fallback is by definition
            # the 0.6.3 bug: legacy-path shadowing can alias it to us.
            class _PoisonedActionLoader:
                def get(self, name, **kwargs):
                    raise AssertionError(
                        f"fallback touched action_loader.get({name!r}); "
                        "this path recurses when fastagent is on the legacy "
                        "action_plugins list (see 0.6.3 regression)."
                    )

            class _FakeSharedLoaderObj:
                action_loader = _PoisonedActionLoader()

            loader = _RecordingLoader(resolved_path=src_dir)
            action = _make_action(
                task_args={"src": src_dir, "dest": "/remote/migrations/"},
                loader=loader,
            )
            action._shared_loader_obj = _FakeSharedLoaderObj()
            action._templar = object()
            action._execute_module = lambda **kwargs: self.fail(
                "fallback called _execute_module instead of the action "
                f"plugin: {kwargs}"
            )

            with patch(
                "plugins.action.copy._BUILTIN_COPY_ACTION_CLASS",
                _FakeBuiltin,
            ), patch.object(ActionBase, "run", return_value={}):
                result = action.run(task_vars={"inventory_hostname": "h"})

            self.assertEqual(len(delegated_calls), 1)
            init_kwargs = delegated_calls[0]["init_kwargs"]
            for required in (
                "task",
                "connection",
                "play_context",
                "loader",
                "templar",
                "shared_loader_obj",
            ):
                self.assertIn(required, init_kwargs)
            self.assertEqual(
                delegated_calls[0]["task_vars"], {"inventory_hostname": "h"}
            )
            self.assertEqual(result.get("dest"), "/remote/migrations/")

    def test_fallback_survives_legacy_path_shadowing(self) -> None:
        # Hard recursion guard: simulate caracal-server's config by making
        # action_loader.get("ansible.legacy.copy") resolve back to *this*
        # ActionModule. Before 0.6.4 that meant the fallback called us again,
        # hit the same `remote_src` branch, and recursed until CPython raised
        # RecursionError. After the fix the fallback ignores the loader
        # entirely.
        action = _make_action(
            task_args={"src": "/whatever", "dest": "/remote", "remote_src": True},
            loader=_RecordingLoader(resolved_path="/whatever"),
        )

        class _ShadowedActionLoader:
            """Simulates legacy-path shadowing: unqualified and legacy both
            route back to the fastagent override."""

            def __init__(self, cls):
                self._cls = cls

            def get(self, name, **kwargs):
                return self._cls(**kwargs)

        class _FakeSharedLoaderObj:
            def __init__(self, cls):
                self.action_loader = _ShadowedActionLoader(cls)

        action._shared_loader_obj = _FakeSharedLoaderObj(ActionModule)
        action._templar = object()

        delegated_calls: list[int] = []

        class _FakeBuiltin:
            def __init__(self, **_kwargs):
                pass

            def _execute_module(self, **_kwargs):
                return {"changed": False}

            def run(self, task_vars):
                delegated_calls.append(1)
                return {"changed": False, "dest": "/remote"}

        with patch(
            "plugins.action.copy._BUILTIN_COPY_ACTION_CLASS", _FakeBuiltin
        ), patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        # If the fallback still went through action_loader, it would have
        # re-entered `ActionModule.run` and recursed until RecursionError.
        self.assertEqual(len(delegated_calls), 1)
        self.assertEqual(result.get("dest"), "/remote")

    def _run_with_fake_builtin(self, action, task_vars=None):
        """Run the action with the builtin copy stubbed; returns (result,
        number of delegations to the builtin)."""
        action._shared_loader_obj = object()
        action._templar = object()
        delegated_calls: list[dict] = []

        class _FakeBuiltin:
            def __init__(self, **kwargs):
                pass

            def _execute_module(self, **_kwargs):
                return {"changed": False}

            def run(self, task_vars):
                delegated_calls.append(task_vars)
                return {"changed": True, "from": "builtin"}

        with patch(
            "plugins.action.copy._BUILTIN_COPY_ACTION_CLASS", _FakeBuiltin
        ), patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars=task_vars or {"inventory_hostname": "h"})
        return result, len(delegated_calls)

    def _validate_action(self, validate, write_result=None):
        action = _make_action(
            task_args={
                "content": "candidate config\n",
                "dest": "/etc/service.conf",
                "validate": validate,
            },
            loader=_RecordingLoader(resolved_path="/unused"),
        )
        action._task_environment = lambda: {"FOO": "bar"}
        if write_result is not None:
            client = action._connection._agent_client
            client.write_file = lambda **kw: (setattr(client, "write_kwargs", kw), write_result)[1]
        return action

    def test_validate_is_sent_to_agent(self) -> None:
        action = self._validate_action("/usr/sbin/check --file=%s 'a b' $HOME")
        result, delegated = self._run_with_fake_builtin(action)
        self.assertEqual(delegated, 0)
        self.assertTrue(result["changed"])
        kwargs = action._connection._agent_client.write_kwargs
        spec = kwargs["validate"]
        ph = spec["placeholder"]
        self.assertRegex(ph, r"^@@FASTAGENT_VALIDATE_[0-9a-f]{32}@@$")
        # $HOME stays for the agent to expand, as run_command does remotely.
        self.assertEqual(spec["argv"], ["/usr/sbin/check", f"--file={ph}", "a b", "$HOME"])
        self.assertEqual(kwargs["env"], {"FOO": "bar"})

    def test_validate_forms_stock_rejects_fall_back(self) -> None:
        # Stock fails each of these with its own message; let it.
        for validate in (
            "/usr/sbin/check",            # no %s
            "/usr/sbin/check %s %s",      # not enough arguments for format string
            "/usr/sbin/check %d %s",      # a directive %s can't satisfy
            "/usr/sbin/check %%s",        # %s that formatting consumes
            "/usr/sbin/check 'abc %s",    # unbalanced quote
            "/usr/sbin/check %(x)s %s",   # mapping key
            ["/usr/sbin/check", "%s"],    # not a string
        ):
            with self.subTest(validate=validate):
                action = self._validate_action(validate)
                result, delegated = self._run_with_fake_builtin(action)
                self.assertEqual(delegated, 1)
                self.assertEqual(result, {"changed": True, "from": "builtin"})
                self.assertIsNone(action._connection._agent_client.write_kwargs)

    def test_empty_validate_means_no_validation(self) -> None:
        action = self._validate_action("")
        _, delegated = self._run_with_fake_builtin(action)
        self.assertEqual(delegated, 0)
        self.assertIsNone(action._connection._agent_client.write_kwargs["validate"])

    def test_validate_failure_matches_stock_result(self) -> None:
        action = self._validate_action("/usr/sbin/check %s", write_result={
            "changed": False, "dest": "/etc/service.conf", "checksum": "x",
            "validate_failed": {"path": "/tmp/v/.source", "rc": 3,
                                "stdout": "out 1\nout 2\n", "stderr": "bad\n"},
        })
        result, _ = self._run_with_fake_builtin(action)
        self.assertEqual(result, {
            "changed": False,
            "failed": True,
            "msg": "failed to validate",
            "checksum": hashlib.sha1(b"candidate config\n").hexdigest(),
            "exit_status": 3,
            "stdout": "out 1\nout 2\n",
            "stdout_lines": ["out 1", "out 2"],
            "stderr": "bad\n",
            "stderr_lines": ["bad"],
        })

    def test_validate_start_failure_matches_stock_result(self) -> None:
        action = self._validate_action("/nonexistent/check -q %s", write_result={
            "changed": False, "dest": "/etc/service.conf", "checksum": "x",
            "validate_failed": {"path": "/tmp/v/.source", "rc": 0, "stdout": "", "stderr": "",
                                "start_errno": 2, "start_error": "fork/exec: no such file"},
        })
        result, _ = self._run_with_fake_builtin(action)
        self.assertEqual(result, {
            "changed": False,
            "failed": True,
            "msg": "Error executing command.",
            "checksum": hashlib.sha1(b"candidate config\n").hexdigest(),
            "rc": 2,
            "cmd": "/nonexistent/check -q /tmp/v/.source",
            "stdout": "",
            "stdout_lines": [],
            "stderr": "",
            "stderr_lines": [],
        })

    def test_builtin_copy_action_class_loads_on_this_ansible(self) -> None:
        # Integration guard: the fallback's whole premise is that we can
        # load ansible-core's real builtin copy action by file path,
        # bypassing any sys.modules aliasing. If that function silently
        # breaks against a future ansible-core (module moved, renamed,
        # etc.) the whole fallback path crashes on first use — as it did
        # in 0.6.4-pre when `from ansible.plugins.action.copy import
        # ActionModule` resolved back into our own partially-loaded
        # module. Exercise the loader directly here so CI catches it
        # before any real play does.
        from plugins.module_utils.builtin_action import load_builtin_action_class

        cls = load_builtin_action_class("copy")
        self.assertTrue(
            issubclass(cls, ActionBase),
            msg=f"loaded class {cls!r} is not an ActionBase",
        )
        # And it must not be *this* class — if sys.modules shadowing had
        # fooled the loader, we'd have picked up ourselves again. Read
        # the loaded module's file via a method's __code__ (inspect
        # utilities mis-classify dynamically-loaded modules on some
        # CPython versions).
        self.assertIsNot(cls, ActionModule)
        path = cls.run.__code__.co_filename
        import ansible
        self.assertEqual(
            os.path.realpath(path),
            os.path.realpath(os.path.join(os.path.dirname(ansible.__file__),
                                         "plugins", "action", "copy.py")),
        )
        self.assertTrue(
            path.endswith(os.path.join("plugins", "action", "copy.py")),
            msg=path,
        )

    def test_builtin_internal_legacy_calls_are_rewritten_to_builtin(self) -> None:
        # The builtin copy action plugin calls `_execute_module(
        # module_name="ansible.legacy.stat"/"copy"/"file", ...)` internally.
        # Under caracal-server's ansible.cfg (which puts fastagent's
        # modules on the legacy `library` path to shadow unqualified
        # `copy:`), those names resolve to our shim modules that refuse
        # direct invocation. We shield the builtin instance by rewriting
        # its `_execute_module` so legacy namespace calls target
        # `ansible.builtin.*` instead, which bypasses the legacy path.
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = os.path.join(tmp, "migrations")
            os.mkdir(src_dir)

            recorded_module_names: list[str] = []

            class _FakeBuiltin:
                def __init__(self, **_kwargs):
                    pass

                def _execute_module(self, **kwargs):
                    recorded_module_names.append(kwargs.get("module_name"))
                    return {"changed": False}

                def run(self, task_vars):
                    # Simulate the three internal calls ansible-core's
                    # copy action makes.
                    self._execute_module(module_name="ansible.legacy.stat")
                    self._execute_module(module_name="ansible.legacy.copy")
                    self._execute_module(module_name="ansible.legacy.file")
                    # And one unrelated call that must pass through.
                    self._execute_module(module_name="ansible.builtin.setup")
                    return {"changed": True, "dest": "/remote/migrations/"}

            loader = _RecordingLoader(resolved_path=src_dir)
            action = _make_action(
                task_args={"src": src_dir, "dest": "/remote/migrations/"},
                loader=loader,
            )
            action._shared_loader_obj = object()
            action._templar = object()

            with patch(
                "plugins.action.copy._BUILTIN_COPY_ACTION_CLASS", _FakeBuiltin
            ), patch.object(ActionBase, "run", return_value={}):
                action.run(task_vars={})

            self.assertEqual(
                recorded_module_names,
                [
                    "ansible.builtin.stat",
                    "ansible.builtin.copy",
                    "ansible.builtin.file",
                    "ansible.builtin.setup",
                ],
            )

    def test_unencrypted_source_uses_loader_path_unchanged(self) -> None:
        # For unencrypted files, get_real_file returns the original
        # path — this asserts the action still reads & ships their
        # bytes verbatim after the fix.
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "plain.txt")
            payload = b"not a secret, just data"
            with open(source, "wb") as f:
                f.write(payload)

            loader = _RecordingLoader(resolved_path=source)
            action = _make_action(
                task_args={"src": source, "dest": "/tmp/plain.txt"},
                loader=loader,
            )

            with patch.object(ActionBase, "run", return_value={}):
                result = action.run(task_vars={})

            self.assertFalse(result.get("failed"), msg=result)
            self.assertEqual(loader.calls, [(source, True)])
            write_kwargs = action._connection._agent_client.write_kwargs
            self.assertEqual(base64.b64decode(write_kwargs["content"]), payload)

    def test_decrypt_false_is_passed_to_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "vault.txt")
            with open(source, "wb") as f:
                f.write(b"$ANSIBLE_VAULT;1.1;AES256\n6165...\n")

            loader = _RecordingLoader(resolved_path=source)
            action = _make_action(
                task_args={
                    "src": source,
                    "dest": "/tmp/vault.txt",
                    "decrypt": False,
                },
                loader=loader,
            )

            with patch.object(ActionBase, "run", return_value={}):
                result = action.run(task_vars={})

            self.assertFalse(result.get("failed"), msg=result)
            self.assertEqual(loader.calls, [(source, False)])

    def test_force_false_existing_destination_does_not_write(self) -> None:
        action = _make_action(
            task_args={
                "content": "new content",
                "dest": "/tmp/existing.txt",
                "force": False,
                "mode": "0600",
            },
            loader=_RecordingLoader(resolved_path="/unused"),
        )
        client = action._connection._agent_client
        client.stat_results = [
            {
                "exists": True,
                "isdir": False,
                "checksum": "different",
            }
        ]

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertFalse(result.get("failed"), msg=result)
        self.assertFalse(result.get("changed"))
        self.assertEqual(result.get("dest"), "/tmp/existing.txt")
        self.assertIsNone(client.write_kwargs)
        self.assertEqual(client.file_calls, [])

    def test_content_to_existing_directory_fails_without_write(self) -> None:
        action = _make_action(
            task_args={"content": "payload", "dest": "/tmp/destdir"},
            loader=_RecordingLoader(resolved_path="/unused"),
        )
        client = action._connection._agent_client
        client.dirs = {"/tmp/destdir"}
        client.stat_results = [{"exists": True, "isdir": True}]

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result.get("failed"), msg=result)
        self.assertEqual(result.get("msg"), "can not use content with a dir as dest")
        self.assertIsNone(client.write_kwargs)

    def test_content_to_trailing_slash_fails_before_stat(self) -> None:
        action = _make_action(
            task_args={"content": "payload", "dest": "/tmp/destdir/"},
            loader=_RecordingLoader(resolved_path="/unused"),
        )

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result.get("failed"), msg=result)
        self.assertEqual(result.get("msg"), "can not use content with a dir as dest")
        self.assertIsNone(action._connection._agent_client.write_kwargs)

    def test_selinux_args_delegate_to_builtin_action_plugin(self) -> None:
        action = _make_action(
            task_args={
                "content": "payload",
                "dest": "/etc/service.conf",
                "setype": "etc_t",
            },
            loader=_RecordingLoader(resolved_path="/unused"),
        )
        action._shared_loader_obj = object()
        action._templar = object()

        delegated_calls: list[dict] = []

        class _FakeBuiltin:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs

            def _execute_module(self, **_kwargs):
                return {"changed": False}

            def run(self, task_vars):
                delegated_calls.append(
                    {"task_vars": task_vars, "init_kwargs": self.init_kwargs}
                )
                return {"changed": True, "dest": "/etc/service.conf"}

        with patch(
            "plugins.action.copy._BUILTIN_COPY_ACTION_CLASS", _FakeBuiltin
        ), patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={"inventory_hostname": "h"})

        self.assertEqual(len(delegated_calls), 1)
        self.assertEqual(result.get("dest"), "/etc/service.conf")
        self.assertIsNone(action._connection._agent_client.write_kwargs)

    def test_diff_read_error_fails_before_write(self) -> None:
        action = _make_action(
            task_args={"content": "new", "dest": "/tmp/existing.txt"},
            loader=_RecordingLoader(resolved_path="/unused"),
        )
        action._play_context = type(
            "DiffPlayContext",
            (),
            {"check_mode": False, "diff": True},
        )()
        client = action._connection._agent_client
        client.stat_results = [
            {
                "exists": True,
                "isdir": False,
                "checksum": "different",
            }
        ]
        client.read_file_error = OSError("permission denied")

        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})

        self.assertTrue(result.get("failed"), msg=result)
        self.assertIn("fastagent read for diff failed", result.get("msg", ""))
        self.assertIsNone(client.write_kwargs)



@unittest.skipIf(
    _ANSIBLE_IMPORT_ERROR is not None,
    "ansible is required to run action plugin tests",
)
class TestCopyActionRoundTrips(unittest.TestCase):
    """The RPCs each copy (and so each template) sends.

    WriteFile compares checksums itself and, when the content already
    matches, only applies owner/group/mode, so the common case is one RPC.
    A Stat goes first only when the action must decide before writing.
    """

    def _run(self, task_args, *, check_mode=False, diff=False, client_setup=None):
        action = _make_action(
            task_args=task_args,
            loader=_RecordingLoader(resolved_path="/unused"),
        )
        action._play_context = type(
            "PlayContext", (), {"check_mode": check_mode, "diff": diff}
        )()
        client = action._connection._agent_client
        if client_setup is not None:
            client_setup(client)
        with patch.object(ActionBase, "run", return_value={}):
            result = action.run(task_vars={})
        return result, client

    def test_unchanged_content_is_one_write_file(self) -> None:
        data = b"rendered template\n"

        def setup(client):
            client.write_result = {
                "changed": False,
                "dest": "/etc/app.conf",
                "checksum": hashlib.sha256(data).hexdigest(),
            }

        result, client = self._run(
            {"content": data.decode(), "dest": "/etc/app.conf",
             "owner": "root", "group": "root", "mode": "0644"},
            client_setup=setup,
        )
        self.assertEqual(client.calls, [("WriteFile", "/etc/app.conf")])
        self.assertFalse(result.get("failed"), msg=result)
        self.assertFalse(result["changed"])
        self.assertEqual(result["dest"], "/etc/app.conf")
        self.assertEqual(result["checksum"], hashlib.sha256(data).hexdigest())
        kwargs = client.write_kwargs
        self.assertTrue(kwargs["report_dir"])
        self.assertEqual(
            (kwargs["owner"], kwargs["group"], kwargs["mode"]),
            ("root", "root", "0644"),
        )

    def test_attribute_fix_on_unchanged_content_reports_changed(self) -> None:
        def setup(client):
            client.write_result = {"changed": True, "checksum": "c"}

        result, client = self._run(
            {"content": "x", "dest": "/etc/app.conf", "mode": "0600"},
            client_setup=setup,
        )
        self.assertEqual(client.calls, [("WriteFile", "/etc/app.conf")])
        self.assertTrue(result["changed"])

    def test_directory_dest_resolves_basename_with_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "app.conf")
            with open(source, "wb") as f:
                f.write(b"payload")

            def setup(client):
                client.dirs = {"/etc/app"}
                client.stat_results = [
                    {"exists": True, "isdir": True},
                    {"exists": False},
                ]

            action = _make_action(
                task_args={"src": source, "dest": "/etc/app"},
                loader=_RecordingLoader(resolved_path=source),
            )
            client = action._connection._agent_client
            setup(client)
            with patch.object(ActionBase, "run", return_value={}):
                result = action.run(task_vars={})

        self.assertFalse(result.get("failed"), msg=result)
        self.assertEqual(result["dest"], "/etc/app/app.conf")
        self.assertEqual(client.calls, [
            ("WriteFile", "/etc/app"),
            ("Stat", "/etc/app"),
            ("Stat", "/etc/app/app.conf"),
            ("WriteFile", "/etc/app/app.conf"),
        ])
        self.assertEqual(base64.b64decode(client.write_kwargs["content"]), b"payload")
        self.assertFalse(client.write_kwargs.get("report_dir"))

    def test_decisions_before_writing_stat_first(self) -> None:
        # Check mode and diff must not write before knowing the current
        # state; force=no must not compare contents; large content would
        # cost more to ship unchanged than the extra Stat round trip.
        from plugins.action import copy as copy_action

        large = "x" * (copy_action._WRITE_WITHOUT_STAT_MAX_BYTES + 1)
        cases = {
            "check_mode": ({"content": "x", "dest": "/d"}, {"check_mode": True}),
            "diff": ({"content": "x", "dest": "/d"}, {"diff": True}),
            "force_no": ({"content": "x", "dest": "/d", "force": False}, {}),
            "large": ({"content": large, "dest": "/d"}, {}),
        }
        for name, (task_args, mode) in cases.items():
            with self.subTest(name):
                _, client = self._run(task_args, **mode)
                self.assertEqual(client.calls[0], ("Stat", "/d"))
                if client.write_kwargs is not None:
                    self.assertFalse(client.write_kwargs.get("report_dir"))

    def test_largest_content_without_stat(self) -> None:
        from plugins.action import copy as copy_action

        content = "x" * copy_action._WRITE_WITHOUT_STAT_MAX_BYTES
        _, client = self._run({"content": content, "dest": "/d"})
        self.assertEqual(client.calls, [("WriteFile", "/d")])


if __name__ == "__main__":
    unittest.main()
