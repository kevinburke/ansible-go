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
except Exception as exc:  # pragma: no cover
    _ANSIBLE_IMPORT_ERROR = exc


class _RecordingAgentClient:
    def __init__(self):
        self.write_kwargs: dict | None = None
        self.file_calls: list[dict] = []
        self.stat_results: list[dict] = []
        self.read_file_error: Exception | None = None

    def stat(self, path, follow, checksum, checksum_algorithm=None):
        if self.stat_results:
            return self.stat_results.pop(0)
        return {"exists": False, "isdir": False}

    def read_file(self, path):
        if self.read_file_error is not None:
            raise self.read_file_error
        return {"content": base64.b64encode(b"old").decode("ascii")}

    def write_file(self, **kwargs):
        self.write_kwargs = kwargs
        return {"changed": True, "checksum": ""}

    def file(self, **kwargs):
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

    def test_validate_delegates_to_builtin_action_plugin(self) -> None:
        # WriteFileParams has a validate field, but the Go agent does not
        # implement Ansible's validate command semantics. A copy task with
        # validate must use the builtin action plugin rather than silently
        # taking the fastagent write path.
        action = _make_action(
            task_args={
                "content": "candidate config\n",
                "dest": "/etc/service.conf",
                "validate": "/usr/sbin/service-check %s",
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
        self.assertEqual(delegated_calls[0]["task_vars"], {"inventory_hostname": "h"})
        self.assertEqual(result.get("dest"), "/etc/service.conf")
        self.assertIsNone(action._connection._agent_client.write_kwargs)

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
        from plugins.action.copy import _load_builtin_copy_action_class

        cls = _load_builtin_copy_action_class()
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
        self.assertNotIn("kevinburke", path, msg=path)
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


if __name__ == "__main__":
    unittest.main()
