from __future__ import annotations

import os
import re
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(relpath: str) -> str:
    with open(os.path.join(REPO_ROOT, relpath), "r", encoding="utf-8") as fh:
        return fh.read()


class TestCompatibilityDocs(unittest.TestCase):
    def test_compatibility_matrix_lists_every_accelerated_module(self) -> None:
        text = _read("docs/compatibility.md")
        rows = {
            row.split("|")[1].strip()
            for row in text.splitlines()
            if row.startswith("| `")
        }

        self.assertEqual(
            rows,
            {
                "`command`, `shell`",
                "`stat`",
                "`copy`, `template`",
                "`file`",
                "`apt`, `package`, `dnf`",
                "`systemd`, `service`",
            },
        )

    def test_readme_and_testing_docs_reference_compatibility_matrix(self) -> None:
        for relpath in ("README.md", "docs/testing.md"):
            with self.subTest(relpath=relpath):
                self.assertIn("docs/compatibility.md", _read(relpath))

    def test_documented_ansible_baseline_is_consistent(self) -> None:
        relpaths = ("README.md", "docs/testing.md", "docs/compatibility.md")
        versions = {}
        for relpath in relpaths:
            match = re.search(r"ansible-core ([0-9]+\.[0-9]+\.[0-9]+)", _read(relpath))
            self.assertIsNotNone(match, f"{relpath} does not name ansible-core")
            versions[relpath] = match.group(1)

        self.assertEqual(
            set(versions.values()),
            {"2.20.4"},
            f"ansible-core baseline drifted across docs: {versions}",
        )


if __name__ == "__main__":
    unittest.main()
