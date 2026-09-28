"""Guard against action overrides that read `connection.become` directly.

The fastagent connection attaches sudo to `connection.become` as a wrapper
the agent handles, so ansible-core sees the become_user. An override that
tests `connection.become is not None` treats every sudo task as a become
method Ansible must apply, and silently drops to the slow path. Overrides
must call ansible_applies_become() from module_utils/fastagent_client.py.
"""

from __future__ import annotations

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTION_DIR = os.path.join(REPO_ROOT, "plugins", "action")

# `self._connection.become` or `getattr(self._connection, "become"...)`.
_DIRECT_BECOME_READ = re.compile(
    r"""_connection\.become\b|getattr\(\s*self\._connection\s*,\s*["']become["']"""
)


class TestBecomeChecks(unittest.TestCase):
    def test_overrides_use_ansible_applies_become(self):
        offenders = []
        for name in sorted(os.listdir(ACTION_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ACTION_DIR, name), encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if _DIRECT_BECOME_READ.search(line):
                        offenders.append(f"plugins/action/{name}:{lineno}")
        self.assertEqual(
            offenders, [],
            "use ansible_applies_become(self._connection) instead of reading "
            f"connection.become: {offenders}",
        )

    def test_pattern_catches_the_old_checks(self):
        for line in (
            'if getattr(self._connection, "become", None) is not None:',
            "if self._connection.become is not None:",
        ):
            self.assertRegex(line, _DIRECT_BECOME_READ)


if __name__ == "__main__":
    unittest.main()
