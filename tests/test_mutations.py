"""Typed writer contract without OS access or an elevated service."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.mutations import MacMutationWriter, MutationUnconfirmed


class MutationWriterTests(unittest.TestCase):
    def setUp(self):
        self.target = {'service_id': 'SERVICE', 'name': 'Wi-Fi', 'interface': 'en0'}
        self.action = {'field': 'dns', 'platform_target': dict(self.target), 'desired': ['1.1.1.1']}
        self.transport = Mock(return_value={'id': 'a' * 32, 'state': 'configured', 'actual': ['1.1.1.1'],
                                           'applied': True, 'network_verified': False})
        self.resolve = Mock(return_value=dict(self.target))
        self.writer = MacMutationWriter(self.transport, self.resolve)

    def write(self, **kwargs):
        return self.writer.write(self.action, ['8.8.8.8'], ['1.1.1.1'],
            **{'operation_id': 'a' * 32, 'proposal_hash': 'b' * 64, **kwargs})

    def test_target_resolves_once_and_cannot_substitute_identity(self):
        self.assertEqual(self.writer.target('Wi-Fi', 'en0'), self.target)
        self.resolve.assert_called_once_with('Wi-Fi', 'en0')
        for value in (None, {}, {**self.target, 'name': 'Ethernet'}, {**self.target, 'interface': 'en9'},
                      {**self.target, 'service_id': ''}, {**self.target, 'extra': 'not accepted'}):
            self.resolve.return_value = value
            with self.assertRaises(ValueError):
                self.writer.target('Wi-Fi', 'en0')
        self.transport.assert_not_called()

    def test_fixed_request_and_defensive_copies(self):
        action = copy.deepcopy(self.action)
        receipt = self.write(restore_of='c' * 32)
        request = self.transport.call_args.args[0]
        self.assertEqual(request, {'version': 1, 'id': 'a' * 32, 'proposal': 'b' * 64, 'target': self.target,
            'field': 'dns', 'expected': ['8.8.8.8'], 'desired': ['1.1.1.1'], 'endpoint': {}, 'restore_of': 'c' * 32})
        request['target']['name'] = 'mutated copy'
        receipt['actual'].clear()
        self.assertEqual(self.action, action)
        self.assertEqual(self.transport.return_value['actual'], ['1.1.1.1'])

    def test_bad_ids_never_call_transport(self):
        for values in ({'operation_id': ''}, {'operation_id': '../outside'}, {'proposal_hash': None},
                       {'proposal_hash': 'a' * 63}, {'restore_of': []}, {'restore_of': 'bad'}):
            with self.assertRaises(ValueError):
                self.write(**values)
        self.transport.assert_not_called()

    def test_timeout_is_unconfirmed_and_never_retried(self):
        self.transport.side_effect = TimeoutError('private OS details')
        with self.assertRaises(MutationUnconfirmed) as caught:
            self.write()
        self.transport.assert_called_once()
        self.assertEqual(caught.exception.receipt, {'id': 'a' * 32, 'state': 'unconfirmed'})
        self.assertNotIn('private', str(caught.exception))

    def test_only_exact_configuration_receipt_is_accepted(self):
        good = copy.deepcopy(self.transport.return_value)
        values = [None, [], {**good, 'id': 'd' * 32}, {**good, 'state': 'needs_verification'},
                  {**good, 'actual': ['8.8.8.8']}, {**good, 'applied': 1},
                  {**good, 'network_verified': True}, {**good, 'extra': 'unexpected'}]
        for value in values:
            self.transport.return_value = value
            with self.assertRaises(MutationUnconfirmed):
                self.write()

    def test_error_projection_does_not_reflect_private_strings_or_objects(self):
        for error in ('private password', {'private': 'password'}, ['private']):
            self.transport.return_value = {'error': error}
            with self.assertRaises(MutationUnconfirmed) as caught:
                self.write()
            self.assertEqual(caught.exception.receipt['reason'], 'operation_unconfirmed')
            self.assertNotIn('private', str(caught.exception.receipt))
        self.transport.return_value = {'error': 'configuration_changed'}
        with self.assertRaises(MutationUnconfirmed) as caught:
            self.write()
        self.assertEqual(caught.exception.receipt['reason'], 'configuration_changed')


if __name__ == '__main__':
    unittest.main()
