"""Guard against tests that silently skip when a plugin fails to import.

Test modules guard their imports so the suite can run without ansible-core
installed. A guard that catches every exception also turns a syntax error
or a broken import in the plugin under test into "skipped", and CI stays
green. Only a missing ansible-core may be turned into a skip.
"""

from __future__ import annotations

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A module-level `except` that catches broadly and stashes the exception
# for a later skipIf.
_BROAD_IMPORT_GUARD = re.compile(
    r"^except\s+(Exception|BaseException|ImportError)\b[^\n]*:\s*(#[^\n]*)?\n"
    r"\s+\w*IMPORT_ERROR\w*\s*=",
    re.MULTILINE,
)


def _test_files():
    for top in ("tests", "plugins"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py") and ("test_" in name or name.endswith("_test.py")):
                    yield os.path.join(dirpath, name)


class TestImportSkips(unittest.TestCase):
    def test_no_broad_import_guards(self):
        offenders = []
        for path in _test_files():
            with open(path, encoding="utf-8") as f:
                if _BROAD_IMPORT_GUARD.search(f.read()):
                    offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(
            offenders, [],
            "catch ModuleNotFoundError for ansible only, and re-raise "
            f"anything else: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
