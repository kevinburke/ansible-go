"""Release version discovery must be local and fail before any recipe runs."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTest(unittest.TestCase):
    def test_make_checks_all_versions_without_running_go(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'plugins/connection').mkdir(parents=True)
            (root / 'meta').mkdir()
            (root / 'meta/runtime.yml').write_text('requires_ansible: ">=2.12.0"\n')
            (root / 'README.md').write_text('Release fixture\n')
            (root / 'Makefile').write_text((ROOT / 'Makefile').read_text())
            checker = root / 'scripts/check-versions.sh'
            checker.write_bytes((ROOT / 'scripts/check-versions.sh').read_bytes())
            checker.chmod(0o755)
            (root / 'fastagent.go').write_text('package fastagent\nconst Version = "1.2.3"\n')
            (root / 'galaxy.yml').write_text('version: 1.2.3\n')
            plugin = root / 'plugins/connection/fastagent.py'
            plugin.write_text('AGENT_VERSION = "1.2.3"\n')
            fakebin = root / 'bin'
            fakebin.mkdir()
            fake_go = fakebin / 'go'
            fake_go.write_text('#!/bin/sh\necho unexpected-go-invocation >&2\nexit 99\n')
            fake_go.chmod(0o755)
            env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ['PATH'])

            def make():
                return subprocess.run(['make', '--no-print-directory', '--dry-run', 'release'],
                                      cwd=root, env=env, capture_output=True, text=True)

            result = make()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('unexpected-go-invocation', result.stderr)
            self.assertIn('kevinburke-fastagent-1.2.3.tar.gz', result.stdout)

            plugin.write_text('AGENT_VERSION = "1.2.4"\n')
            result = make()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('1.2.4', result.stderr)
            self.assertNotIn('go build', result.stdout)
            self.assertNotIn('unexpected-go-invocation', result.stderr)


if __name__ == '__main__':
    unittest.main()
