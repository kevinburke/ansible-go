from __future__ import annotations

import ast
import os
import unittest
from typing import List, Optional, Tuple


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTION_PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins", "action")


def _string_value(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class TestActionPluginFallbacks(unittest.TestCase):
    def test_no_executable_legacy_fallback_calls(self) -> None:
        offenders: List[str] = []

        for name in sorted(os.listdir(ACTION_PLUGIN_DIR)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(ACTION_PLUGIN_DIR, name)
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                values: List[Tuple[str, ast.AST]] = []
                values.extend((arg, arg) for arg in node.args)
                values.extend((kw.arg or "", kw.value) for kw in node.keywords)

                for label, value_node in values:
                    value = _string_value(value_node)
                    if value is None or "ansible.legacy." not in value:
                        continue
                    relpath = os.path.relpath(path, REPO_ROOT)
                    offenders.append(
                        f"{relpath}:{value_node.lineno} {label}={value!r}"
                    )

        self.assertEqual(
            offenders,
            [],
            "fallback calls must use ansible.builtin.*: " + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
