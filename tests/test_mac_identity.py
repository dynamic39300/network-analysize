"""Release identity selection cannot be weakened by descriptors or CLI flags."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.ipc import LocalClient, LocalServer, RpcError, core_server
from relay.lifecycle import CoreLifecycle
from relay.mac_identity import IDENTIFIER, UNSAFE_ENTITLEMENTS, production_requirement, requirement_for


class SigningPolicyTests(unittest.TestCase):
    def setUp(self):
        self.identity = {'valid': True, 'developer_id': True, 'adhoc': False, 'hardened': True,
            'debugged': False, 'identifier': IDENTIFIER, 'team': 'ABCDEFGHIJ', 'cdhash': 'a' * 40, 'entitlements': {}}

    def test_requirement_pins_signer_application_and_current_executable(self):
        rule = requirement_for(self.identity)
        self.assertIn('anchor apple generic', rule)
        self.assertIn('certificate leaf[subject.OU] = "ABCDEFGHIJ"', rule)
        self.assertIn('identifier "com.wangxinlei.relay"', rule)
        self.assertIn('cdhash H"' + 'a' * 40 + '"', rule)
        self.assertNotEqual(rule, requirement_for({**self.identity, 'cdhash': 'b' * 40}))

    def test_incomplete_tampered_debuggable_or_other_application_never_falls_back(self):
        for key, value in [('valid', False), ('hardened', False), ('debugged', True), ('developer_id', False),
                           ('identifier', 'another.app'), ('team', '" or always'), ('cdhash', 'bad')]:
            with self.subTest(key=key), self.assertRaises(PermissionError):
                requirement_for({**self.identity, key: value})
        for entitlement in UNSAFE_ENTITLEMENTS:
            with self.subTest(entitlement=entitlement), self.assertRaises(PermissionError):
                requirement_for({**self.identity, 'entitlements': {entitlement: True}})

    def test_only_explicit_adhoc_development_has_no_signer_policy(self):
        self.assertIsNone(requirement_for({**self.identity, 'adhoc': True, 'team': '', 'developer_id': False}))
        with patch('relay.mac_identity.native_bridge', side_effect=AssertionError('source must not load bridge')):
            self.assertIsNone(production_requirement())

    def test_frozen_missing_bridge_and_old_macos_fail_closed(self):
        with patch.object(sys, 'platform', 'darwin'), patch.object(sys, 'frozen', True, create=True):
            with patch('relay.mac_identity.native_bridge', side_effect=OSError('missing')):
                with self.assertRaises(OSError):
                    production_requirement()
            with patch('relay.mac_identity.native_bridge') as bridge, patch('relay.mac_identity.platform.mac_ver', return_value=('12.0', (), 'arm64')):
                bridge.return_value.identity.return_value = self.identity
                with self.assertRaises(PermissionError):
                    production_requirement()


@unittest.skipUnless(sys.platform == 'darwin', 'macOS ordinary-user transport')
class SigningTransportSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_signed_client_rejects_socket_descriptor_instead_of_downgrading(self):
        server = LocalServer(self.data, Mock(side_effect=AssertionError('untrusted payload')))
        self.addCleanup(server.close)
        with patch('relay.ipc.production_requirement', return_value='test-requirement'):
            with self.assertRaisesRegex(RpcError, 'signed_transport_required'):
                LocalClient(self.data)

    def test_unsigned_client_cannot_claim_signed_identity_from_descriptor(self):
        server = LocalServer(self.data, lambda *_: {})
        self.addCleanup(server.close)
        path = self.data / 'ipc/connection.json'
        value = json.loads(path.read_text())
        value['transport'] = 'mac-xpc-v1'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RpcError, 'signed_client_required'):
            LocalClient(self.data)

    def test_signed_server_rejects_unmanaged_and_custom_directory(self):
        with patch('relay.ipc.production_requirement', return_value='test-requirement'):
            for managed in (False, True):
                with self.subTest(managed=managed), self.assertRaises(PermissionError):
                    core_server(self.data, lambda *_: {}, 'a' * 32, managed=managed)
        self.assertFalse((self.data / 'ipc').exists())

    def test_signed_launch_requires_explicit_service_registration_without_spawn(self):
        service = Mock()
        service.status.return_value = {'available': True, 'state': 'not_registered'}
        spawn = Mock(side_effect=AssertionError('weaker core launched'))
        manager = CoreLifecycle(self.data, service=service, spawn=spawn)
        with patch('relay.lifecycle.production_requirement', return_value='test-requirement'):
            self.assertIsNone(manager.start())
            self.assertIsNone(manager.start(explicit=True))
            self.assertTrue(manager.status()['signed_service_required'])
        service.register.assert_not_called()
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
