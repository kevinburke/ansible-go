"""An empty auto-created Galaxy token file must not satisfy release preflight."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_galaxy_token', ROOT / 'scripts/check-galaxy-token.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ReleaseCredentialsTest(unittest.TestCase):
    def test_empty_malformed_and_missing_credentials_fail_without_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token.yml'
            with patch.object(checker.C, 'GALAXY_TOKEN_PATH', str(path)), \
                    patch.dict(os.environ, {'ANSIBLE_GALAXY_TOKEN': ''}):
                with self.assertRaises(SystemExit):
                    checker.check_token()
                for content in ('', 'null\n', '{}\n', 'token: null\n', 'token: 123\n',
                                'token: " "\n', 'secret-not-a-mapping\n', 'token: [secret-not-to-print'):
                    path.write_text(content)
                    with self.subTest(content=content), self.assertRaises(SystemExit) as error:
                        checker.check_token()
                    self.assertNotIn('secret-', str(error.exception))

    def test_file_and_environment_tokens_pass_without_printing_credentials(self):
        token = 'test-credential-never-print'
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token.yml'
            with patch.object(checker.C, 'GALAXY_TOKEN_PATH', str(path)), \
                    patch.dict(os.environ, {'ANSIBLE_GALAXY_TOKEN': token}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                checker.check_token()  # The environment works without a token file.
                os.environ['ANSIBLE_GALAXY_TOKEN'] = ''
                path.write_text('token: ' + token + '\n')
                checker.check_token()
                os.environ['ANSIBLE_GALAXY_TOKEN'] = ' ' + token
                with self.assertRaises(SystemExit):
                    checker.check_token()
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
