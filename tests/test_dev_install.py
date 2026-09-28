"""scripts/dev-install.sh stamps one development version everywhere.

The version names the agent binary on the controller and remote host, the
remote daemon's socket, and what Hello checks. If the Go agent, the
connection plugin and the collection metadata disagreed, or a build reused
the release version, a playbook could silently run an older agent. The
script runs in a throwaway clone so its snapshot of "the working tree" is
under the test's control.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ansible_bin(name):
    sibling = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(name)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        check=True, capture_output=True, text=True,
    ).stdout


@unittest.skipIf(shutil.which("go") is None, "go not found")
@unittest.skipIf(_ansible_bin("ansible-galaxy") is None, "ansible-galaxy not found")
class TestDevInstall(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="fastagent-dev-install-")
        self.addCleanup(shutil.rmtree, tmp)
        # A clone of HEAD with this working tree copied over it and
        # committed, so the clone starts clean.
        self.repo = os.path.join(tmp, "repo")
        subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--no-local",
                        REPO_ROOT, self.repo], check=True)
        files = _git(REPO_ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        for name in filter(None, files.split("\0")):
            src = os.path.join(REPO_ROOT, name)
            if not os.path.isfile(src):
                continue  # deleted in the working tree
            os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
            shutil.copy2(src, os.path.join(self.repo, name))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "working tree")
        self.dest = os.path.join(tmp, "dest")
        self.agents = os.path.join(tmp, "agents")
        with open(os.path.join(REPO_ROOT, "fastagent.go"), encoding="utf-8") as f:
            self.base = re.search(r'^const Version = "(.*)"$', f.read(), re.M).group(1)

    def _install(self):
        env = dict(os.environ, FASTAGENT_DEV_DEST=self.dest, FASTAGENT_DEV_AGENT_DIR=self.agents,
                   FASTAGENT_DEV_ARCHES="amd64", ANSIBLE_GALAXY=_ansible_bin("ansible-galaxy"))
        proc = subprocess.run([os.path.join(self.repo, "scripts", "dev-install.sh")], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return re.match(r"fastagent (\S+)\n", proc.stdout).group(1)

    def test_stamps_one_dev_version_everywhere(self):
        commit = _git(self.repo, "rev-parse", "--short=12", "HEAD").strip()
        version = self._install()
        self.assertEqual(version, f"{self.base}-dev.g{commit}")

        installed = os.path.join(self.dest, "collections", "ansible_collections", "kevinburke", "fastagent")
        with open(os.path.join(installed, "MANIFEST.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["collection_info"]["version"], version)
        with open(os.path.join(installed, "plugins", "connection", "fastagent.py"), encoding="utf-8") as f:
            self.assertIn(f'\nAGENT_VERSION = "{version}"\n', f.read())
        with open(os.path.join(self.agents, f"fastagent-{version}-linux-amd64"), "rb") as f:
            self.assertIn(version.encode(), f.read())
        self.assertEqual(os.listdir(self.agents), [f"fastagent-{version}-linux-amd64"])

        # The source tree itself still has the release version.
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")

        with open(os.path.join(self.dest, "env.sh"), encoding="utf-8") as f:
            env_sh = f.read()
        self.assertIn(f"export ANSIBLE_COLLECTIONS_PATH='{self.dest}/collections:", env_sh)
        self.assertIn(f"export ANSIBLE_ACTION_PLUGINS='{installed}/plugins/action'", env_sh)
        out = subprocess.run([os.path.join(self.dest, "run"), "sh", "-c", "echo $ANSIBLE_ACTION_PLUGINS"],
                             check=True, capture_output=True, text=True).stdout
        self.assertEqual(out, f"{installed}/plugins/action\n")

    def test_working_tree_changes_get_their_own_version(self):
        clean = self._install()
        with open(os.path.join(self.repo, "untracked.go"), "w", encoding="utf-8") as f:
            f.write("package fastagent\n")
        dirty = self._install()
        self.assertRegex(dirty, rf"^{re.escape(clean)}\.w[0-9a-f]{{10}}$")
        with open(os.path.join(self.repo, "untracked.go"), "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertNotEqual(self._install(), dirty)


if __name__ == "__main__":
    unittest.main()
