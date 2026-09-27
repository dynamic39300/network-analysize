"""Real anonymous XPC endpoints only; no launchd registration or network calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native XPC checks')
class NativeXPCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from relay.mac_identity import native_bridge
        if not native_bridge.cache_info().currsize:
            subprocess.run([sys.executable, str(ROOT / 'scripts/build-native-ipc.py')], check=True, capture_output=True)
        cls.identity = dict(native_bridge().identity())
        cls.requirement = 'cdhash H"' + cls.identity['cdhash'] + '"'

    def setUp(self):
        from Foundation import NSXPCListener
        from relay.mac_xpc import MacXPCClient, MacXPCServer
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        def handler(*args):
            self.calls.append(args)
            return {'ok': True, 'method': args[0]}
        self.listener = NSXPCListener.anonymousListener()
        self.server = MacXPCServer(self.temp.name, handler, requirement=self.requirement, listener=self.listener)
        self.addCleanup(self.server.close)
        self.server.start()
        self.descriptor = json.loads((Path(self.temp.name) / 'ipc/connection.json').read_text())
        self.client = MacXPCClient(self.descriptor, requirement=self.requirement, endpoint=self.listener.endpoint())

    def test_native_protocol_and_authenticated_request_round_trip(self):
        self.assertTrue(self.identity['valid'])
        self.assertEqual(self.client.call('status'), {'ok': True, 'method': 'status'})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][3], self.client.client_id)

    def test_wrong_server_requirement_sends_no_application_payload(self):
        self.client.requirement = 'identifier "never.a.valid.relay.peer"'
        with self.assertRaises((ConnectionError, TimeoutError)):
            self.client.call('model_configure', {'api_key': 'must-never-reach-peer'})
        self.assertFalse(self.calls)

    def test_wrong_client_requirement_does_not_call_handler(self):
        self.server.requirement = 'identifier "never.a.valid.relay.peer"'
        with self.assertRaises((ConnectionError, TimeoutError)):
            self.client.call('confirm', {'accept': True})
        self.assertFalse(self.calls)

    def test_other_session_or_user_cannot_invoke_core(self):
        for attribute in ('session', 'uid'):
            original = getattr(self.server, attribute)
            setattr(self.server, attribute, original + 1)
            try:
                with self.assertRaises((ConnectionError, TimeoutError)):
                    self.client.call('confirm', {'accept': True})
            finally:
                setattr(self.server, attribute, original)
        self.assertFalse(self.calls)

    def test_forged_instance_malformed_id_and_frame_limit_have_no_effect(self):
        from relay.ipc import MAX_FRAME, RpcError
        self.client.instance = '0' * 32
        with self.assertRaisesRegex(RpcError, 'core_changed'):
            self.client.call('confirm')
        self.client.instance = self.server.instance
        with self.assertRaises((ConnectionError, TimeoutError)):
            self.client.call('confirm', request_id='invalid')
        with self.assertRaisesRegex(RpcError, 'frame_limit'):
            self.client.call('model_configure', {'api_key': 'x' * MAX_FRAME})
        self.assertFalse(self.calls)

    def test_disconnect_timeout_does_not_replay_and_close_waits_for_handler(self):
        from unittest.mock import patch
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def held(*args):
            self.calls.append(args)
            entered.set()
            release.wait(3)
            return {}
        self.server.handler = held
        with patch('relay.mac_xpc.TIMEOUT', 0.2):
            with self.assertRaises((ConnectionError, TimeoutError)):
                self.client.call('confirm')
        self.assertTrue(entered.is_set())
        def close():
            self.server.close()
            closed.set()
        thread = threading.Thread(target=close)
        thread.start()
        try:
            self.assertFalse(closed.wait(0.1))
        finally:
            release.set()
            thread.join(5)
        self.assertTrue(closed.is_set())
        self.assertEqual(len(self.calls), 1)

    def test_real_core_proposal_confirmation_dedupe_and_receipt_over_xpc(self):
        from Foundation import NSXPCListener
        from relay.core import CoreRuntime
        from relay.core_service import CoreService
        from relay.mac_xpc import MacXPCClient, MacXPCServer
        from test_diagnostics import FakeMac, config
        from test_windows import FakeEvents
        self.server.close()
        mac = FakeMac()
        runtime = CoreRuntime(self.temp.name, platform='darwin', config=config(mac, company=True), runner=mac,
            events_factory=FakeEvents, guard_threaded=False, execution_directory=Path(self.temp.name) / 'execution')
        listener = NSXPCListener.anonymousListener()
        def factory(data, handler, instance, **_):
            return MacXPCServer(data, handler, instance, requirement=self.requirement, listener=listener)
        service = CoreService(runtime, server_factory=factory)
        self.addCleanup(service.close)
        service.start()
        descriptor = json.loads((Path(self.temp.name) / 'ipc/connection.json').read_text())
        client = MacXPCClient(descriptor, requirement=self.requirement, endpoint=listener.endpoint())
        def wait(operation):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                done = client.call('operation', {'id': operation['id']})
                if done['state'] not in ('accepted', 'running') and not client.call('status')['busy']:
                    return done
                time.sleep(0.01)
            self.fail('Core operation did not settle')
        self.assertEqual(client.call('settings')['ipc_identity'], 'signed_build_and_session')
        done = wait(client.call('propose', {'issues': ['dns_mixed_on_vpn']}))
        self.assertEqual(done['state'], 'completed')
        review = client.call('review', {'run_id': done['run_id']})
        params = {key: review[key] for key in ('run_id', 'proposal_hash', 'review_token')}
        params.update(accept=True, unrestricted=False)
        request_id = uuid.uuid4().hex
        operation = client.call('confirm', params, request_id)
        self.assertEqual(wait(operation)['state'], 'completed')
        mutations = len(mac.mutations)
        self.assertGreater(mutations, 0)
        self.assertEqual(client.call('confirm', params, request_id)['id'], request_id)
        self.assertEqual(len(mac.mutations), mutations)
        self.assertEqual(client.call('task_detail', {'run_id': review['run_id']})['record']['outcome'], 'verified')


if __name__ == '__main__':
    unittest.main()
