"""plugins/modules/systemd.py must decide like upstream Ansible's systemd_service.

testdata/systemd_state_actions.json is the shared table; service_test.go
checks the Go agent against the same file.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from ansible.modules import systemd_service  # type: ignore[import-untyped]
    from plugins.modules.systemd import state_action  # type: ignore[import-untyped]
    _ANSIBLE_IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover
    # Skip only when ansible-core itself is not installed; any other import
    # failure must fail the run.
    if exc.name is None or exc.name.split(".")[0] != "ansible":
        raise
    _ANSIBLE_IMPORT_ERROR = exc

with open(os.path.join(REPO_ROOT, "testdata", "systemd_state_actions.json")) as f:
    CASES = json.load(f)["cases"]


def upstream_action(state, active):
    """The branch in systemd_service.main() that picks the systemctl verb,
    using upstream's own is_running_service/is_deactivating_service."""
    status = {"ActiveState": active}
    running = systemd_service.is_running_service(status)
    if state == "started":
        return None if running else "start"
    if state == "stopped":
        if running or systemd_service.is_deactivating_service(status):
            return "stop"
        return None
    return state[:-2] if running else "start"


@unittest.skipIf(_ANSIBLE_IMPORT_ERROR is not None, f"ansible-core not installed: {_ANSIBLE_IMPORT_ERROR}")
class TestSystemdModuleStateAction(unittest.TestCase):
    def test_table_is_not_empty(self):
        self.assertGreater(len(CASES), 0)

    def test_module_matches_table(self):
        for c in CASES:
            with self.subTest(state=c["state"], active=c["active"]):
                self.assertEqual(state_action(c["state"], c["active"]) or "", c["action"])

    def test_table_matches_upstream(self):
        for c in CASES:
            with self.subTest(state=c["state"], active=c["active"]):
                self.assertEqual(upstream_action(c["state"], c["active"]) or "", c["action"])

    def test_unknown_state_raises(self):
        with self.assertRaises(ValueError):
            state_action("bogus", "active")


if __name__ == "__main__":
    unittest.main()
