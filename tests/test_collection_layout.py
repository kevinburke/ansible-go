"""Validates that the Ansible collection packages and loads correctly.

These tests exercise the end-to-end install path a third-party user takes:

    1. `ansible-galaxy collection build` produces a valid tarball
    2. `ansible-galaxy collection install` extracts it to a collections path
    3. `ansible-doc -t connection kevinburke.fastagent.fastagent` parses the
       plugin's DOCUMENTATION block (which in turn exercises the module_utils
       import path via `from ansible_collections.kevinburke.fastagent...`)
    4. The module_utils can be imported through the collection namespace
    5. The tarball does not leak unrelated files (Go source, CI configs, etc.)

The tests rebuild the tarball from scratch each run, so they will catch
galaxy.yml mistakes (missing build_ignore entries, bad import paths, etc.)
without depending on any pre-built artifact.

Run from the repo root:

    python3 -m unittest tests.test_collection_layout -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTION_NAMESPACE = "kevinburke"
COLLECTION_NAME = "fastagent"
CONNECTION_FQCN = "kevinburke.fastagent.fastagent"


def _require(binary: str) -> str:
    """Return the absolute path to a required binary, or skip the test."""
    path = shutil.which(binary)
    if path is None:
        raise unittest.SkipTest(f"{binary} not on PATH")
    return path


class _BuiltCollection:
    """Context manager that builds and installs the collection in a temp dir.

    Yields the path to the installed collections directory (suitable for
    ANSIBLE_COLLECTIONS_PATH) and the path to the built tarball.
    """

    def __init__(self):
        self._tmp = None
        self.install_path = None
        self.tarball_path = None

    def __enter__(self):
        _require("ansible-galaxy")
        self._tmp = tempfile.TemporaryDirectory(prefix="fastagent-collection-test-")
        tmp = self._tmp.name

        build_out = os.path.join(tmp, "build")
        os.makedirs(build_out)
        subprocess.run(
            ["ansible-galaxy", "collection", "build",
             "--force", "--output-path", build_out, REPO_ROOT],
            check=True,
            capture_output=True,
        )

        entries = os.listdir(build_out)
        tarballs = [e for e in entries if e.endswith(".tar.gz")]
        if len(tarballs) != 1:
            raise AssertionError(
                f"expected exactly one tarball in {build_out}, got {entries}"
            )
        self.tarball_path = os.path.join(build_out, tarballs[0])

        self.install_path = os.path.join(tmp, "collections")
        os.makedirs(self.install_path)
        subprocess.run(
            ["ansible-galaxy", "collection", "install",
             "--force", "--collections-path", self.install_path,
             self.tarball_path],
            check=True,
            capture_output=True,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._tmp is not None:
            self._tmp.cleanup()
        return False


class TestTarballContents(unittest.TestCase):
    """Assertions about what ends up in the built tarball."""

    @classmethod
    def setUpClass(cls):
        _require("ansible-galaxy")
        cls._ctx = _BuiltCollection()
        cls._ctx.__enter__()
        with tarfile.open(cls._ctx.tarball_path) as tf:
            cls.members = sorted(tf.getnames())

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_manifest_present(self):
        self.assertIn("MANIFEST.json", self.members)
        self.assertIn("FILES.json", self.members)

    def test_runtime_yml_present(self):
        """meta/runtime.yml is required by Ansible Galaxy import."""
        self.assertIn("meta/runtime.yml", self.members)

    def test_connection_plugin_present(self):
        self.assertIn("plugins/connection/fastagent.py", self.members)

    def test_action_plugins_present(self):
        for name in ("command", "copy", "file", "stat", "apt", "systemd"):
            self.assertIn(f"plugins/action/{name}.py", self.members)

    def test_library_modules_present(self):
        for name in ("apt", "dnf", "systemd"):
            self.assertIn(f"plugins/modules/{name}.py", self.members)

    def test_module_utils_present(self):
        self.assertIn(
            "plugins/module_utils/fastagent_client.py", self.members
        )

    def test_no_go_sources_leaked(self):
        """Go source and build artifacts must not ship in the collection."""
        forbidden_prefixes = ("cmd/", "tmp/", "worktrees/", "docs/", ".buildkite/", "scripts/")
        forbidden_exts = (".go", "go.mod", "go.sum")
        leaked = [
            m for m in self.members
            if m.startswith(forbidden_prefixes)
            or any(m.endswith(ext) for ext in forbidden_exts)
        ]
        self.assertEqual(leaked, [], f"unexpected files in tarball: {leaked}")

    def test_test_file_excluded(self):
        """The Python integration test file is not part of the shipped collection."""
        self.assertNotIn(
            "plugins/module_utils/fastagent_client_test.py", self.members
        )

    def test_makefile_excluded(self):
        self.assertNotIn("Makefile", self.members)

    def test_tests_dir_excluded(self):
        """The tests/ directory is for repo CI, not shipped collection."""
        leaked = [m for m in self.members if m.startswith("tests/")]
        self.assertEqual(leaked, [], f"tests/ leaked into tarball: {leaked}")


class TestInstalledCollection(unittest.TestCase):
    """Exercise the collection the way a third-party user would after install."""

    @classmethod
    def setUpClass(cls):
        _require("ansible-galaxy")
        cls._ctx = _BuiltCollection()
        cls._ctx.__enter__()
        cls.install_path = cls._ctx.install_path
        cls.collection_dir = os.path.join(
            cls.install_path, "ansible_collections",
            COLLECTION_NAMESPACE, COLLECTION_NAME,
        )

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_collection_directory_laid_out(self):
        self.assertTrue(os.path.isdir(self.collection_dir),
                        f"not a dir: {self.collection_dir}")
        self.assertTrue(os.path.isfile(
            os.path.join(self.collection_dir, "plugins", "connection",
                         "fastagent.py")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.collection_dir, "plugins", "module_utils",
                         "fastagent_client.py")))

    def test_ansible_doc_parses_connection_plugin(self):
        """Runs ansible-doc, which imports the plugin, parses DOCUMENTATION,
        and (critically) resolves the module_utils import in the plugin."""
        _require("ansible-doc")
        env = os.environ.copy()
        env["ANSIBLE_COLLECTIONS_PATH"] = self.install_path
        result = subprocess.run(
            ["ansible-doc", "-t", "connection", CONNECTION_FQCN],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"ansible-doc failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )
        # Sanity-check that documented options are present.
        self.assertIn("download_url", result.stdout)
        self.assertIn("agent_path", result.stdout)

    def test_module_utils_importable(self):
        """Verify the collection-qualified import path resolves.

        This is exactly how plugins/connection/fastagent.py imports
        FastAgentClient. If the import chain is broken, ansible-doc would
        also fail — but this test gives us a focused error message on the
        import itself.
        """
        env = os.environ.copy()
        # Prepend the collections install path so
        # `ansible_collections.kevinburke.fastagent...` resolves.
        env["PYTHONPATH"] = os.pathsep.join(
            [self.install_path, env.get("PYTHONPATH", "")]
        )
        code = (
            "from ansible_collections.kevinburke.fastagent"
            ".plugins.module_utils.fastagent_client "
            "import FastAgentClient, FastAgentError; "
            "assert FastAgentClient is not None; "
            "assert FastAgentError is not None; "
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"import failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
