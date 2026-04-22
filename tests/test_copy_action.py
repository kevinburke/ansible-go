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

    def stat(self, path, follow, checksum):
        return {"exists": False, "isdir": False}

    def write_file(self, **kwargs):
        self.write_kwargs = kwargs
        return {"changed": True, "checksum": ""}


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


if __name__ == "__main__":
    unittest.main()
