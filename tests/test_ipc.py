"""Real local sockets and a real core; network changes remain FakeMac operations."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.core import CoreRuntime
from relay.core_service import CoreService
from relay.ipc import LocalClient, LocalServer, MAX_FRAME, RpcError, packed, proof, receive, send
from relay.models import ModelConsent, ResponsesModel
from test_diagnostics import FakeMac, config
from test_reasoning import Script, conclusion
from test_windows import FakeEvents


def spawned_core(directory, pipe):
    hold = threading.Event()
    mac = FakeMac()
    runtime = CoreRuntime(Path(directory) / 'core', platform='darwin', runner=mac, config=config(mac, company=True),
        events_factory=FakeEvents, guard_threaded=False, execution_directory=Path(directory) / 'execution')
    original = runtime.engine.run_all
    def delayed(**kwargs):
        hold.wait(20)
        return original(**kwargs)
    runtime.engine.run_all = delayed
    service = CoreService(runtime)
    try:
        service.start()
        pipe.send(service.instance)
        pipe.recv()
    finally:
        hold.set()
        service.close()
        pipe.close()


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged native Unix transport')
class LocalTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        def handler(*args):
            self.calls.append(args)
            return {'ok': True}
        self.server = LocalServer(self.temp.name, handler)
        self.server.start()
        self.addCleanup(self.server.close)
        self.client = LocalClient(self.temp.name)

    def test_real_peer_authentication_and_private_descriptor(self):
        self.assertEqual(self.client.call('status'), {'ok': True})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.server.directory / 'connection.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.server.directory / 'core.sock').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.client.secret.hex(), packed(self.calls).decode())

    def test_wrong_secret_or_user_cannot_dispatch(self):
        self.client.secret = bytes(32)
        with self.assertRaises(RpcError):
            self.client.call('status')
        with patch('relay.ipc.peer_uid', return_value=os.geteuid() + 1), self.assertRaises((PermissionError, EOFError)):
            LocalClient(self.temp.name).call('status')
        self.assertFalse(self.calls)

    def test_version_restart_and_descriptor_permissions_fail_closed(self):
        old = self.client
        self.server.close()
        self.server = LocalServer(self.temp.name, lambda *_: {'new': True})
        self.server.start()
        self.addCleanup(self.server.close)
        with self.assertRaises(RpcError):
            old.call('status')
        self.assertEqual(LocalClient(self.temp.name).call('status'), {'new': True})
        descriptor = self.server.directory / 'connection.json'
        descriptor.chmod(0o644)
        with self.assertRaises(PermissionError):
            LocalClient(self.temp.name)
        descriptor.chmod(0o600)

    def test_duplicate_server_does_not_remove_live_endpoint(self):
        with self.assertRaises(OSError):
            LocalServer(self.temp.name, lambda *_: None)
        self.assertTrue(self.client.call('status')['ok'])

    def test_replayed_envelope_wrong_nonce_and_oversize_are_not_dispatched(self):
        for mode in ('nonce', 'oversize', 'duplicate_json'):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(self.server.directory / 'core.sock'))
                deadline = time.monotonic() + 3
                hello = receive(connection, deadline)
                hello.pop('proof')
                body = {**hello, 'id': uuid.uuid4().hex, 'client': self.client.client_id, 'method': 'status', 'params': {}}
                if mode == 'nonce':
                    body['nonce'] = 'old_nonce'
                    send(connection, {**body, 'proof': proof(self.client.secret, 'request', body)}, deadline)
                elif mode == 'oversize':
                    connection.sendall(struct.pack('!I', MAX_FRAME + 1))
                else:
                    invalid = b'{"id":1,"id":2}'
                    connection.sendall(struct.pack('!I', len(invalid)) + invalid)
                connection.settimeout(3)
                self.assertEqual(connection.recv(1), b'')
        self.assertFalse(self.calls)


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged native Unix transport')
class CoreServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.mac = FakeMac()
        self.data = Path(self.temp.name) / 'core'
        self.make_service()

    def make_service(self, model=None):
        self.runtime = CoreRuntime(self.data, platform='darwin', runner=self.mac, config=config(self.mac, company=True),
            events_factory=FakeEvents, guard_threaded=False, execution_directory=Path(self.temp.name) / 'execution',
            model=model, model_consent=ModelConsent(model) if model else None)
        self.service = CoreService(self.runtime)
        self.service.start()
        self.addCleanup(self.service.close)
        self.client = LocalClient(self.data)

    def wait(self, operation, client=None):
        client = client or self.client
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = client.call('operation', {'id': operation['id']})
            if value['state'] not in ('running', 'accepted') and not client.call('status')['busy']:
                return value
            time.sleep(0.01)
        self.fail('Core operation did not settle')

    def prepare(self):
        done = self.wait(self.client.call('propose', {'issues': ['dns_mixed_on_vpn']}))
        self.assertEqual(done['state'], 'completed')
        return self.client.call('review', {'run_id': done['run_id']})

    @staticmethod
    def confirmation(review, accept=True):
        return {key: review[key] for key in ('run_id', 'proposal_hash', 'review_token')} | {
            'accept': accept, 'unrestricted': review['unrestricted']}

    def test_startup_is_idle_no_probe_guard_or_upload(self):
        state = self.client.call('status')
        self.assertTrue(state['ready'])
        self.assertFalse(state['guard']['enabled'])
        self.assertFalse(self.mac.calls)
        self.assertIsNone(state['report'])

    def test_check_survives_client_reconnect_and_journal_has_single_owner(self):
        op = self.client.call('check')
        another = LocalClient(self.data)
        done = self.wait(op, another)
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(another.call('status')['report']['run_id'], done['run_id'])
        self.assertFalse(self.mac.mutations)
        from relay.agent_store import AgentStore
        with self.assertRaises(OSError):
            AgentStore(self.data / 'agent')

    def test_review_is_exact_single_use_and_client_bound(self):
        review = self.prepare()
        params = self.confirmation(review)
        with self.assertRaisesRegex(RpcError, 'review_required'):
            LocalClient(self.data).call('confirm', params)
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.client.call('confirm', {**params, 'proposal_hash': '0' * 64})
        operation = self.client.call('confirm', params)
        self.assertEqual(self.wait(operation)['state'], 'completed')
        count = len(self.mac.mutations)
        self.assertGreater(count, 0)
        self.assertEqual(self.client.call('status')['report']['stage'], 'finished')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.client.call('confirm', params)
        self.assertEqual(len(self.mac.mutations), count)

    def test_identical_retry_returns_same_operation_changed_retry_rejected(self):
        review = self.prepare()
        params = self.confirmation(review)
        request_id = uuid.uuid4().hex
        operation = self.client.call('confirm', params, request_id)
        self.wait(operation)
        count = len(self.mac.mutations)
        self.assertEqual(self.client.call('confirm', params, request_id)['id'], request_id)
        self.assertEqual(len(self.mac.mutations), count)
        with self.assertRaisesRegex(RpcError, 'request_conflict'):
            self.client.call('confirm', {**params, 'accept': False}, request_id)

    def test_detach_invalidates_review_not_task_and_new_view_requires_review(self):
        review = self.prepare()
        self.client.call('detach')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.client.call('confirm', self.confirmation(review))
        another = LocalClient(self.data)
        self.assertEqual(another.call('status')['run_stage'], 'awaiting_authorization')
        updated = another.call('review', {'run_id': review['run_id']})
        op = another.call('confirm', self.confirmation(updated, False))
        self.assertEqual(self.wait(op, another)['state'], 'completed')
        self.assertFalse(self.mac.mutations)

    def test_pending_authorization_prevents_new_check_and_profile_change(self):
        self.prepare()
        with self.assertRaisesRegex(RpcError, 'pending_proposal'):
            self.client.call('check')
        with self.assertRaisesRegex(RpcError, 'pending_proposal'):
            self.client.call('profile_activate', {'profile_id': self.runtime.profiles.active['id']})
        self.assertFalse(self.mac.mutations)

    def test_guard_preserves_live_confirmation_but_expiry_does_not_stall_it_forever(self):
        review = self.prepare()
        old = self.runtime.agent._last_run
        self.runtime.start_guard()
        self.runtime.guard.schedule.next_due = 0
        count = len(self.mac.calls)
        self.runtime.guard.tick()
        self.assertEqual(len(self.mac.calls), count)
        self.assertEqual(old.stage, 'awaiting_authorization')
        with patch.object(self.runtime.agent, 'clock', return_value=review['expires_at'] + 1):
            self.runtime.guard.tick()
        self.assertEqual(old.stage, 'cancelled')
        self.assertNotEqual(self.runtime.agent._last_run.id, old.id)
        self.assertFalse(self.mac.mutations)

    def test_unknown_methods_extra_fields_and_embedded_authority_are_rejected(self):
        for method, params in [('execute', {}), ('check', {'grant': 'yes'}), ('guard', {'enabled': 1}),
                               ('confirm', {'accept': True})]:
            with self.assertRaisesRegex(RpcError, 'invalid_request'):
                self.client.call(method, params)
        self.assertFalse(self.mac.calls)

    def test_same_user_ui_disconnect_does_not_enable_or_disable_guard(self):
        self.wait(self.client.call('guard', {'enabled': True}))
        self.client.call('detach')
        self.assertTrue(LocalClient(self.data).call('status')['guard']['enabled'])
        self.wait(self.client.call('guard', {'enabled': False}))
        self.assertFalse(self.runtime.guard.schedule.enabled)
        source = self.runtime.events
        self.assertTrue(source.closed)
        self.wait(self.client.call('guard', {'enabled': True}))
        self.assertIsNot(self.runtime.events, source)
        self.assertTrue(self.runtime.events.available)

    def test_restart_invalidates_old_confirmation_and_retains_operation(self):
        review = self.prepare()
        params = self.confirmation(review)
        request_id = uuid.uuid4().hex
        done = self.wait(self.client.call('confirm', params, request_id))
        old_client = self.client
        count = len(self.mac.mutations)
        self.service.close()
        self.make_service()
        with self.assertRaises(RpcError):
            old_client.call('status')
        self.assertEqual(self.client.call('operation', {'id': request_id})['run_id'], done['run_id'])
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.client.call('confirm', params)
        self.assertEqual(len(self.mac.mutations), count)

    def test_busy_status_and_cancel_remain_responsive_during_probe(self):
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.engine.run_all
        def slow(**kwargs):
            entered.set()
            release.wait(3)
            return original(**kwargs)
        self.addCleanup(release.set)
        with patch.object(self.runtime.engine, 'run_all', side_effect=slow):
            operation = self.client.call('check')
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.client.call('status')['busy'], 'check')
            with self.assertRaisesRegex(RpcError, 'busy'):
                self.client.call('check')
            self.assertEqual(self.client.call('cancel')['state'], 'completed')
            release.set()
            self.wait(operation)
        self.assertFalse(self.mac.mutations)
        self.assertFalse(self.runtime.guard.schedule.enabled)

    def test_authorized_model_continuation_uses_same_task_over_socket(self):
        self.service.close()
        script = Script([('propose_repair', {'issues': ['dns_mixed_on_vpn']}), ('finish', conclusion('resolved'))])
        model = ResponsesModel('test', 'http://127.0.0.1:9999/v1/responses', transport=script)
        self.make_service(model)
        op = self.wait(self.client.call('investigate'))
        review = self.client.call('review', {'run_id': op['run_id']})
        done = self.wait(self.client.call('confirm', self.confirmation(review)))
        self.assertEqual(done['run_id'], op['run_id'])
        self.assertEqual(self.client.call('status')['report']['model']['calls'], 2)
        self.assertEqual(len(script.requests), 2)

    def test_dynamic_ipc_execution_requires_risk_ack_and_keeps_private_output_out_of_status(self):
        from test_dynamic import request
        self.service.close()
        document = request('unused', "from pathlib import Path; print(Path('private.txt').read_text())")
        document.pop('target_ids')
        document['targets'] = ['t1']
        script = Script([('propose_command', document), ('finish', conclusion('needs_user'))])
        model = ResponsesModel('test', 'http://127.0.0.1:9999/v1/responses', transport=script)
        self.make_service(model)
        operation = self.wait(self.client.call('investigate'))
        review = self.client.call('review', {'run_id': operation['run_id']})
        self.assertTrue(review['unrestricted'])
        parameters = self.confirmation(review)
        with self.assertRaisesRegex(RpcError, 'unrestricted_review_required'):
            self.client.call('confirm', {**parameters, 'unrestricted': False})
        directory = Path(review['proposal']['actions'][0]['spec']['cwd'])
        secret = 'private output never granted any additional permission'
        (directory / 'private.txt').write_text(secret, encoding='utf-8')
        result = self.wait(self.client.call('confirm', parameters))
        self.assertEqual(result['state'], 'completed')
        state = self.client.call('status')
        self.assertEqual(state['report']['stage'], 'needs_reconciliation')
        self.assertTrue(state['report']['recovery_pending'])
        self.assertNotIn(secret, json.dumps(state))
        self.assertNotIn(secret, json.dumps(script.requests))
        self.assertIn(secret, self.runtime.agent._last_run.receipt['job']['stdout'])
        self.assertFalse(self.mac.mutations)

    def test_private_profiles_use_strict_existing_contract_and_records_stay_redacted(self):
        profiles = self.client.call('profiles')
        document = self.client.call('profile_export', {'profile_id': profiles['active_id']})
        payload = {'document': {**document, 'name': 'Private copied profile'}, 'profile_id': None,
                   'revision': None, 'imported': True}
        self.assertEqual(self.wait(self.client.call('profile_save', payload))['state'], 'completed')
        before = copy.deepcopy(self.runtime.profiles.active)
        bad = {**payload, 'document': {**document, 'grant': True}}
        self.assertEqual(self.wait(self.client.call('profile_save', bad))['state'], 'failed')
        self.assertEqual(before, self.runtime.profiles.active)
        self.wait(self.client.call('check'))
        records = json.dumps(self.client.call('records'))
        self.assertNotIn('actions', records)
        self.assertNotIn('snapshot', records)
        self.assertFalse(self.mac.mutations)

    def test_record_failure_before_acceptance_never_runs_the_action(self):
        with patch.object(self.runtime.store, 'save_ipc_request', side_effect=OSError('private path')):
            with self.assertRaisesRegex(RpcError, 'operation_failed'):
                self.client.call('check')
        self.assertFalse(self.mac.calls)
        self.assertFalse(self.mac.mutations)

    def test_cancel_waiting_task_removes_review_and_updates_state(self):
        review = self.prepare()
        self.client.call('cancel')
        self.assertEqual(self.client.call('status')['run_stage'], 'cancelled')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.client.call('confirm', self.confirmation(review))
        self.assertFalse(self.mac.mutations)

    def test_confirmation_after_profile_drift_does_not_mutate(self):
        review = self.prepare()
        document = self.runtime.profiles.document(self.runtime.profiles.active['id'])
        document['name'] = 'Changed'
        self.runtime.profiles.save(document)
        done = self.wait(self.client.call('confirm', self.confirmation(review)))
        self.assertEqual(done['state'], 'failed')
        self.assertFalse(self.mac.mutations)

    def test_real_core_process_outlives_clients_and_restart_never_replays_request(self):
        self.service.close()
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=spawned_core, args=(self.temp.name, child))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(5))
            instance = parent.recv()
            client = LocalClient(self.data)
            client_id = client.client_id
            request_id = uuid.uuid4().hex
            operation = client.call('check', request_id=request_id)
            client.call('detach')
            another = LocalClient(self.data)
            self.assertEqual(another.call('status')['core_instance'], instance)
            self.assertEqual(another.call('status')['busy'], 'check')
            self.assertTrue(process.is_alive())
            # Kill only this isolated FakeMac harness, never an installed Relay process.
            process.kill()
            process.join(5)
            self.assertFalse(process.is_alive())
            self.make_service()
            self.client.client_id = client_id
            recorded = self.client.call('operation', {'id': operation['id']})
            self.assertEqual(recorded['state'], 'interrupted')
            self.assertEqual(self.client.call('check', request_id=request_id)['state'], 'interrupted')
            self.assertFalse(self.mac.calls)
            self.assertFalse(self.mac.mutations)
        finally:
            if process.is_alive():
                parent.send('stop')
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            parent.close()

    def test_expired_review_and_record_capacity_do_not_start_an_action(self):
        review = self.prepare()
        with patch('relay.core_service.time.time', return_value=review['expires_at'] + 1):
            with self.assertRaisesRegex(RpcError, 'review_required'):
                self.client.call('confirm', self.confirmation(review))
        with patch.object(self.runtime.store, 'save_ipc_request', side_effect=ValueError('capacity')):
            with self.assertRaisesRegex(RpcError, 'operation_failed'):
                self.client.call('confirm', self.confirmation(review))
        self.assertFalse(self.mac.mutations)


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged native Unix transport')
class RemoteControllerTests(unittest.TestCase):
    setUp = CoreServiceTests.setUp
    make_service = CoreServiceTests.make_service
    wait = CoreServiceTests.wait
    prepare = CoreServiceTests.prepare
    def controller(self):
        from relay.remote_controller import RemoteController
        self.events = []
        controller = RemoteController(self.data, lambda *value: self.events.append(value), lambda fn, *args: fn(*args))
        controller._poll()
        self.addCleanup(controller.close)
        return controller

    def test_ui_cache_does_not_own_journal_or_execute_network_commands(self):
        controller = self.controller()
        self.assertTrue(controller.ui_state()['ready'])
        self.assertFalse(self.mac.calls)
        controller.check()
        controller.queue.get_nowait()()
        self.wait({'id': controller.pending[0]})
        controller._poll()
        self.assertIsNotNone(controller.ui_state()['snapshot']['last_check'])
        self.assertFalse(self.mac.mutations)
        self.assertIsNone(controller.pending)

    def test_lost_ack_is_queried_not_retried_and_stale_ui_cannot_confirm(self):
        controller = self.controller()
        original = controller.client.call
        def lost(method, params=None, request_id=None):
            result = original(method, params, request_id)
            if method == 'check':
                raise EOFError('ack lost')
            return result
        controller.check()
        with patch.object(controller.client, 'call', side_effect=lost), self.assertRaises(EOFError):
            controller.queue.get_nowait()()
        operation_id = controller.pending[0]
        self.wait({'id': operation_id})
        controller._disconnected()
        controller._poll()
        self.assertIsNone(controller.pending)
        self.assertEqual(len(self.runtime.store.recent()), 1)
        review = self.prepare()
        controller._disconnected()
        self.assertFalse(controller.confirm_review(review, True))
        self.assertFalse(self.mac.mutations)
