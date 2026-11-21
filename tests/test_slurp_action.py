"""Verify slurp routing and compare real agent reads with Ansible's module."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from tests.test_stat_action import _FakeConnection, _FakeTask, _requires_ansible
from tests.test_stat_differential import _local_agent_binary

try:
    from plugins.action import slurp
    from plugins.module_utils.fastagent_client import FastAgentClient, FastAgentError
except ModuleNotFoundError as exc:
    if exc.name.split('.')[0] not in ('ansible', 'yaml'):
        raise


def action_for(client, args, **kwargs):
    action = slurp.ActionModule.__new__(slurp.ActionModule)
    action._task = _FakeTask(args)
    action._connection = _FakeConnection(client, **kwargs)
    action._execute_module = Mock(return_value={'changed': False, 'from_builtin': True})
    action._remove_tmp_path = Mock()
    return action


@_requires_ansible
class SlurpActionTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.read_file.return_value = {'content': 'eA==', 'size': 1}

    def test_reads_in_check_mode_and_accepts_path_alias(self):
        for key in ('src', 'path'):
            action = action_for(self.client, {key: '/tmp/file'})
            action._task.check_mode = True
            self.assertEqual(action.run(), {'changed': False, 'content': 'eA==',
                                          'source': '/tmp/file', 'encoding': 'base64'})
            action._execute_module.assert_not_called()
        self.assertEqual(self.client.read_file.call_count, 2)

    def test_unsupported_args_never_read_through_agent(self):
        for args in ({}, {'src': None}, {'src': 12}, {'src': 'relative'},
                     {'src': '~/file'}, {'src': '/tmp/$FILE'}, {'src': '/tmp/\0file'},
                     {'src': '/tmp/file', 'unknown': True},
                     {'src': '/tmp/file', 'path': '/tmp/other'},
                     {'src': '/tmp/file', 'armor': False},
                     {'src': '/tmp/file', 'armor': 'invalid'}):
            with self.subTest(args=args):
                action = action_for(self.client, args)
                self.assertTrue(action.run()['from_builtin'])
                self.assertEqual(action._execute_module.call_args.kwargs['module_name'],
                                 'ansible.builtin.slurp')
        self.client.read_file.assert_not_called()

    def test_become_and_transport_fallback(self):
        for kwargs in ({'become_user': 'nobody'}, {'become': object()}, {'transport': 'ssh'}):
            action = action_for(self.client, {'src': '/tmp/file'}, **kwargs)
            self.assertTrue(action.run()['from_builtin'])
        self.client.read_file.assert_not_called()
        action = action_for(self.client, {'src': '/tmp/file'},
                            become=types.SimpleNamespace(fastagent_handles_become=True))
        self.assertEqual(action.run()['content'], 'eA==')

    def test_async_and_old_ansible_fallback(self):
        action = action_for(self.client, {'src': '/tmp/file'})
        action._task.async_val = 10
        self.assertTrue(action.run()['from_builtin'])
        self.assertEqual(action._execute_module.call_args.kwargs['wrap_async'], 10)
        action._remove_tmp_path.assert_not_called()
        action._task.async_val = 0
        with patch.object(slurp, '_ANSIBLE_VERSION', '2.19.1'):
            self.assertTrue(action.run()['from_builtin'])
        with patch.object(slurp, '_ANSIBLE_VERSION', '2.20.0'):
            action._task.args['armor'] = True
            self.assertTrue(action.run()['from_builtin'])
        self.client.read_file.assert_not_called()

    def test_agent_read_errors_preserve_stock_diagnostics(self):
        self.client.read_file.side_effect = FastAgentError(-32000, 'permission denied')
        action = action_for(self.client, {'src': '/root/private'})
        self.assertTrue(action.run()['from_builtin'])

    def test_bad_agent_response_is_reported_and_falls_back(self):
        for rpc in ({}, {'content': '', 'size': 1}, {'content': None, 'size': 0}):
            self.client.read_file.return_value = rpc
            action = action_for(self.client, {'src': '/tmp/file'})
            with patch.object(slurp.display, 'warning') as warning:
                self.assertTrue(action.run()['from_builtin'])
                warning.assert_called_once()

    def test_real_agent_matches_stock_for_binary_empty_and_symlink_reads(self):
        with tempfile.TemporaryDirectory(prefix='fastagent-slurp-') as tmp:
            binary = _local_agent_binary(tmp)
            root = Path(tmp)
            for name, content in [('plain', b'hello\n'), ('empty', b''),
                                  ('binary', bytes(range(256)) * 400),
                                  ('sp ace-é', 'hello é\n'.encode())]:
                (root / name).write_bytes(content)
            (root / 'link').symlink_to('binary')
            proc = subprocess.Popen([binary, '--serve'], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                client = FastAgentClient(proc.stdin, proc.stdout)
                for name in ('plain', 'empty', 'binary', 'sp ace-é', 'link'):
                    with self.subTest(name=name):
                        args = {'src': str(root / name)}
                        stock = subprocess.run(
                            [sys.executable, '-m', 'ansible.modules.slurp'],
                            input=json.dumps({'ANSIBLE_MODULE_ARGS': args}),
                            capture_output=True, text=True, check=True)
                        stock = json.loads(stock.stdout)
                        action = action_for(client, args)
                        fast = action.run()
                        action._execute_module.assert_not_called()
                        for field in ('content', 'source', 'encoding'):
                            self.assertEqual(fast[field], stock[field])
                        self.assertFalse(fast['changed'])
                # Read failures also exercise the real RPC's error shape.
                for name in ('missing', '.'):
                    action = action_for(client, {'src': str(root / name)})
                    self.assertTrue(action.run()['from_builtin'])
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)
                proc.stdout.close()
