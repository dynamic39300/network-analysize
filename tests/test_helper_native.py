"""Actual native XPC service with memory preferences; never installs a daemon."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in helper XPC checks')
class NativeHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_mutation_native as fixture
        fixture.NativeMutationTests.setUpClass()
        cls.fixture_class = fixture.NativeMutationTests

    def setUp(self):
        import objc
        from Foundation import NSXPCListener
        from relay.mac_identity import native_bridge
        from relay.mac_helper import MacHelperTransport
        self.fixture = self.fixture_class()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.requirement = 'cdhash H"' + dict(native_bridge().identity())['cdhash'] + '"'
        self.listener = NSXPCListener.anonymousListener()
        self.service = objc.lookUpClass('RelayHelperService').alloc().initWithExecutor_identity_(
            self.fixture.executor, {'test_build': self.requirement})
        self.service.startWithListener_requirement_(self.listener, self.requirement)
        self.addCleanup(self.service.close)
        self.transport = MacHelperTransport(requirement=self.requirement, endpoint=self.listener.endpoint(), peer_uid=os.geteuid())

    def begin(self):
        request = self.fixture.request()
        restore = self.fixture.restore(request)
        self.transport.begin(uuid.uuid4().hex, [{'apply': request, 'restore': restore}])
        return request, restore

    def new_client(self):
        from relay.mac_helper import MacHelperTransport
        return MacHelperTransport(requirement=self.requirement, endpoint=self.listener.endpoint(), peer_uid=os.geteuid())

    def restart_helper(self):
        import objc
        from Foundation import NSXPCListener
        self.service.close()
        self.listener = NSXPCListener.anonymousListener()
        self.service = objc.lookUpClass('RelayHelperService').alloc().initWithExecutor_identity_(
            self.fixture.executor, {'test_build': self.requirement})
        self.service.startWithListener_requirement_(self.listener, self.requirement)
        self.addCleanup(self.service.close)

    def test_batch_write_deduplication_readback_and_settlement(self):
        request, _restore = self.begin()
        first = self.transport.perform(request)
        self.assertEqual(first['state'], 'configured')
        self.assertEqual(self.transport.perform(request), first)
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.transport.end('verified')
        self.assertFalse(self.transport.call('status')['batch_active'])

    def test_no_unscoped_or_outside_manifest_write(self):
        self.assertEqual(self.transport.call('perform', {'request': self.fixture.request()})['error'], 'batch_required')
        request, _restore = self.begin()
        self.assertEqual(self.transport.perform({**request, 'desired': ['8.8.8.8']})['error'], 'outside_batch')
        self.assertEqual(self.fixture.preferences.writes, 0)
        self.transport.end('blocked')

    def test_lost_end_response_can_be_queried_without_repeating_a_write(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        original = self.transport.call
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise EOFError('synthetic lost receipt')
        with patch.object(self.transport, 'call', side_effect=lost):
            with self.assertRaises(EOFError):
                self.transport.end('verified')
        self.assertIsNotNone(self.transport.context)
        self.transport.end('verified')
        self.assertIsNone(self.transport.context)
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_invalid_batch_never_creates_a_lease(self):
        request = self.fixture.request()
        restore = self.fixture.restore(request)
        for requests in ([], [{'apply': request}], [{'apply': request, 'restore': {**restore, 'desired': []}}],
                         [{'apply': request, 'restore': restore}] * 6):
            with self.subTest(requests=requests):
                with self.assertRaises(PermissionError):
                    self.transport.begin(uuid.uuid4().hex, requests)
                self.assertFalse(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.reads, 0)

    def test_terminal_history_releases_only_matching_local_batch_contexts(self):
        request, _ = self.begin()
        batch_id = self.transport.context['batch_id']
        self.transport.perform(request)
        original = self.transport.call
        def lost(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == 'end':
                raise EOFError('synthetic lost response')
            return result
        with patch.object(self.transport, 'call', side_effect=lost):
            with self.assertRaises(EOFError):
                self.transport.end('verified')
        self.assertIsNotNone(self.transport.context)
        self.assertEqual(self.transport.inspect_recovery(batch_id)['state'], 'finished')
        self.assertIsNone(self.transport.context)
        next_request, _ = self.begin()
        context = dict(self.transport.context)
        self.transport.inspect_recovery(batch_id)
        self.assertEqual(self.transport.context, context)
        self.transport.end('blocked')
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertNotEqual(request['id'], next_request['id'])

    def test_invalid_terminal_receipt_does_not_release_local_context(self):
        self.begin()
        context = dict(self.transport.context)
        for changes in ({'network_verified': True}, {'network_verified': 0}, {'disposition': 'unknown'},
                        {'batch_id': uuid.uuid4().hex}):
            result = {'state': 'finished', 'batch_id': context['batch_id'], 'requests': context['requests'],
                      'disposition': 'restored', 'network_verified': False, **changes}
            with patch.object(self.transport, 'call', return_value=result):
                with self.assertRaises((ValueError, PermissionError)):
                    self.transport.inspect_recovery(context['batch_id'])
            self.assertEqual(self.transport.context, context)
        self.transport.end('blocked')

    def test_wrong_peer_signature_or_uid_sends_no_business_parameters(self):
        old = self.transport.requirement
        for requirement, user in (('identifier "never.relay.helper"', os.geteuid()), (old, os.geteuid() + 1)):
            self.transport.requirement, self.transport.peer_uid = requirement, user
            with self.assertRaises((PermissionError, ConnectionError, TimeoutError)):
                self.transport.call('perform', {'request': self.fixture.request()})
        self.assertEqual(self.fixture.preferences.writes, 0)
        self.assertIsNone(json.loads((self.fixture.directory / 'helper-control').read_text())['batch'])

    def manifest(self):
        request = self.fixture.request()
        return [{'apply': request, 'restore': self.fixture.restore(request)}]

    def test_absent_batch_requires_manifest_and_is_sealed_before_late_admission(self):
        batch_id, requests = uuid.uuid4().hex, self.manifest()
        self.assertEqual(self.transport.call('recovery_inspect', {'batch_id': batch_id}), {'error': 'batch_not_found'})
        result = self.transport.inspect_recovery(batch_id, requests=requests)
        self.assertEqual(result['disposition'], 'not_started')
        self.assertIs(result['network_verified'], False)
        path = self.fixture.directory / ('batch-' + batch_id)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.transport.inspect_recovery(batch_id, requests=requests), result)
        with self.assertRaises(PermissionError):
            self.transport.begin(batch_id, requests)
        self.restart_helper()
        other = self.new_client()
        self.assertEqual(other.inspect_recovery(batch_id, requests=requests)['disposition'], 'not_started')
        with self.assertRaises(PermissionError):
            other.begin(batch_id, requests)
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_manifest_conflict_never_seals_or_clears_an_accepted_batch(self):
        request, restore = self.begin()
        context = dict(self.transport.context)
        with self.assertRaises(PermissionError):
            self.transport.inspect_recovery(context['batch_id'], requests=self.manifest())
        self.assertEqual(self.transport.context, context)
        result = self.transport.inspect_recovery(context['batch_id'], requests=[{'apply': request, 'restore': restore}])
        self.assertEqual(result['state'], 'pending')
        self.assertFalse((self.fixture.directory / ('batch-' + context['batch_id'])).exists())
        self.assertEqual(self.transport.perform(request)['state'], 'configured')
        self.transport.end('verified')
        with self.assertRaises(PermissionError):
            self.transport.inspect_recovery(context['batch_id'], requests=self.manifest())
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_sealing_other_absent_batch_preserves_active_owner_and_review(self):
        self.begin()
        context = dict(self.transport.context)
        review = self.transport.inspect_recovery(context['batch_id'])
        self.assertEqual(self.transport.inspect_recovery(uuid.uuid4().hex, requests=self.manifest())['disposition'], 'not_started')
        self.assertEqual(self.transport.context, context)
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.transport.recover(review, 'restore')
        self.transport.end_recovery()
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_reused_operation_ids_cannot_prove_absence(self):
        requests = self.manifest()
        active = uuid.uuid4().hex
        self.transport.begin(active, requests)
        for stage in ('active', 'finished'):
            with self.subTest(stage=stage):
                batch_id = uuid.uuid4().hex
                with self.assertRaises(PermissionError):
                    self.transport.inspect_recovery(batch_id, requests=requests)
                self.assertFalse((self.fixture.directory / ('batch-' + batch_id)).exists())
            if stage == 'active':
                self.transport.end('blocked')
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_orphan_operation_receipt_prevents_false_absence(self):
        requests = self.manifest()
        request = requests[0]['apply']
        result = self.fixture.executor.perform_user_session_(request, os.geteuid() + 1, 99)
        self.assertEqual(result['state'], 'configured')
        batch_id = uuid.uuid4().hex
        with self.assertRaises(PermissionError):
            self.transport.inspect_recovery(batch_id, requests=requests)
        self.assertFalse((self.fixture.directory / ('batch-' + batch_id)).exists())
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_corrupt_or_unsafe_history_blocks_absence_proof(self):
        path = self.fixture.directory / ('batch-' + uuid.uuid4().hex)
        for state in ('corrupt', 'wide', 'symlink'):
            with self.subTest(state=state):
                if state == 'symlink':
                    path.symlink_to(self.fixture.directory / 'helper-control')
                else:
                    path.write_text('{}' if state == 'corrupt' else '{"state":"finished","requests":[]}')
                    path.chmod(0o600 if state == 'corrupt' else 0o644)
                try:
                    batch_id = uuid.uuid4().hex
                    with self.assertRaises(PermissionError):
                        self.transport.inspect_recovery(batch_id, requests=self.manifest())
                    self.assertFalse((self.fixture.directory / ('batch-' + batch_id)).exists())
                finally:
                    path.unlink()
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_missing_control_with_existing_receipts_cannot_initialize_fresh(self):
        import objc
        self.transport.inspect_recovery(uuid.uuid4().hex, requests=self.manifest())
        self.service.close()
        (self.fixture.directory / 'helper-control').unlink()
        with self.assertRaises(Exception):
            objc.lookUpClass('RelayHelperService').alloc().initWithExecutor_identity_(
                self.fixture.executor, {'test_build': self.requirement})
        self.assertFalse((self.fixture.directory / 'helper-control').exists())
        self.assertEqual(self.fixture.preferences.writes, 0)

    def test_terminal_proof_manifest_mismatch_preserves_local_context(self):
        self.begin()
        context = dict(self.transport.context)
        result = {'state': 'finished', 'batch_id': context['batch_id'], 'requests': self.manifest(),
                  'disposition': 'not_started', 'network_verified': False}
        with patch.object(self.transport, 'call', return_value=result):
            with self.assertRaises(ValueError):
                self.transport.inspect_recovery(context['batch_id'])
        self.assertEqual(self.transport.context, context)
        self.transport.end('blocked')

    def test_sealing_cannot_claim_another_users_terminal_batch(self):
        batch_id, requests = uuid.uuid4().hex, self.manifest()
        self.transport.inspect_recovery(batch_id, requests=requests)
        path = self.fixture.directory / ('batch-' + batch_id)
        record = json.loads(path.read_text())
        record['user'] = os.geteuid() + 1
        path.write_text(json.dumps(record))
        before = path.read_bytes()
        for manifest in (requests, self.manifest()):
            with self.assertRaises(PermissionError):
                self.transport.inspect_recovery(batch_id, requests=manifest)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.fixture.preferences.writes, 0)

    def test_reserved_recovery_operation_id_prevents_absence_proof(self):
        from test_mutation_native import native_dictionary
        self.begin()
        requests = self.manifest()
        control = json.loads((self.fixture.directory / 'helper-control').read_text())
        control['batch']['recovery_attempts'] = [{'restore_ids': [requests[0]['apply']['id']]}]
        self.service.setState_(native_dictionary(control).mutableCopy())
        batch_id = uuid.uuid4().hex
        with self.assertRaises(PermissionError):
            self.transport.inspect_recovery(batch_id, requests=requests)
        self.assertFalse((self.fixture.directory / ('batch-' + batch_id)).exists())
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 0)

    def test_invalid_manifest_and_failed_terminal_save_never_prove_absence(self):
        batch_id, requests = uuid.uuid4().hex, self.manifest()
        for manifest in ([], requests * 2, [{'apply': requests[0]['apply']}], {'apply': requests[0]['apply']}):
            self.assertEqual(self.transport.call('recovery_inspect', {'batch_id': batch_id, 'requests': manifest}),
                             {'error': 'invalid_batch'})
        path = self.fixture.directory / ('batch-' + batch_id)
        path.mkdir()
        try:
            with self.assertRaises(PermissionError):
                self.transport.inspect_recovery(batch_id, requests=requests)
            self.assertTrue(path.is_dir())
        finally:
            path.rmdir()
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_recovery_capacity_reserve_does_not_reopen_ordinary_admission(self):
        requests = self.manifest()
        record = {'state': 'finished', 'requests': requests}
        for number in range(1000):
            path = self.fixture.directory / ('batch-' + f'{number:032x}')
            path.write_text(json.dumps(record))
            path.chmod(0o600)
        with self.assertRaises(PermissionError):
            self.transport.begin(uuid.uuid4().hex, self.manifest())
        result = self.transport.inspect_recovery(uuid.uuid4().hex, requests=self.manifest())
        self.assertEqual(result['disposition'], 'not_started')
        for number in range(1000, 1999):
            path = self.fixture.directory / ('batch-' + f'{number:032x}')
            path.write_text(json.dumps(record))
            path.chmod(0o600)
        with self.assertRaises(PermissionError):
            self.transport.inspect_recovery(uuid.uuid4().hex, requests=self.manifest())
        self.assertEqual(self.transport.inspect_recovery(result['batch_id'])['disposition'], 'not_started')
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_wrong_client_signature_does_not_enter_service(self):
        self.service.setRequirement_('identifier "never.relay.client"')
        with self.assertRaises((ConnectionError, TimeoutError)):
            self.transport.call('status')
        self.assertEqual(self.fixture.preferences.reads, 0)

    def test_other_client_and_drain_cannot_interrupt_active_batch(self):
        from relay.mac_helper import MacHelperTransport
        request, restore = self.begin()
        other = MacHelperTransport(requirement=self.requirement, endpoint=self.listener.endpoint(), peer_uid=os.geteuid())
        self.assertEqual(other.call('drain')['error'], 'batch_unsettled')
        with self.assertRaises(PermissionError):
            other.begin(uuid.uuid4().hex, [{'apply': request, 'restore': restore}])
        self.assertEqual(other.call('perform', {'request': request})['error'], 'batch_required')
        self.transport.end('blocked')
        self.assertEqual(other.call('drain'), {'draining': True})
        with self.assertRaises(PermissionError):
            self.begin()
        self.assertEqual(other.call('resume'), {'draining': False})
        self.begin()
        self.transport.end('blocked')

    def test_expired_batch_stops_forward_write_but_allows_restore(self):
        request, restore = self.begin()
        self.transport.perform(request)
        self.service.setBatchDeadline_(0)
        self.assertEqual(self.transport.perform(request)['error'], 'batch_expired')
        self.assertEqual(self.transport.perform(restore)['state'], 'configured')
        self.transport.end('rolled_back')
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_another_users_drain_cannot_be_resumed_or_replaced(self):
        from test_mutation_native import native_dictionary
        control = json.loads((self.fixture.directory / 'helper-control').read_text())
        control.update(draining=True, drain_user=os.geteuid() + 1)
        self.service.setState_(native_dictionary(control).mutableCopy())
        for method in ('drain', 'resume'):
            self.assertEqual(self.transport.call(method), {'error': 'drain_owned_by_another_user'})
        with self.assertRaises(PermissionError):
            self.begin()
        self.assertEqual(self.fixture.preferences.writes, 0)

    def test_unknown_outcome_does_not_release_batch_and_readonly_settlement(self):
        request, _restore = self.begin()
        self.fixture.preferences.apply_ok = self.fixture.preferences.persist = False
        self.assertEqual(self.transport.perform(request)['state'], 'needs_verification')
        with self.assertRaises(PermissionError):
            self.transport.end('verified')
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.transport.end('rolled_back')
        self.assertFalse(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_uncertain_ordinary_compensation_keeps_batch_until_explicit_review(self):
        request, restore = self.begin()
        self.transport.perform(request)
        self.fixture.preferences.apply_ok = False
        self.assertEqual(self.transport.perform(restore)['state'], 'needs_verification')
        with self.assertRaises(PermissionError):
            self.transport.end('rolled_back')
        self.assertTrue(self.transport.call('status')['batch_active'])
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        self.transport.recover(review, 'restore')
        self.transport.end_recovery()
        self.assertFalse(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertTrue(all(json.loads(path.read_text())['phase'] == 'completed'
                            for path in self.fixture.directory.glob('*.json')))

    def test_restart_preserves_batch_but_never_replays_and_changed_identity_rejected(self):
        import objc
        from Foundation import NSXPCListener
        request, _restore = self.begin()
        self.transport.perform(request)
        self.service.close()
        listener = NSXPCListener.anonymousListener()
        service = objc.lookUpClass('RelayHelperService').alloc().initWithExecutor_identity_(
            self.fixture.executor, {'test_build': self.requirement})
        self.addCleanup(service.close)
        service.startWithListener_requirement_(listener, self.requirement)
        self.transport.endpoint = listener.endpoint()
        with self.assertRaises(PermissionError):
            self.transport.perform(request)
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 1)
        with self.assertRaises(Exception):
            objc.lookUpClass('RelayHelperService').alloc().initWithExecutor_identity_(self.fixture.executor, {'test_build': 'changed'})

    def test_close_waits_for_native_commit_without_replay(self):
        request, _restore = self.begin()
        self.fixture.preferences.entered, self.fixture.preferences.release = threading.Event(), threading.Event()
        finished, result = threading.Event(), []
        def write():
            try:
                result.append(self.transport.perform(request))
            except (ConnectionError, TimeoutError):
                pass
        thread = threading.Thread(target=write)
        thread.start()
        self.assertTrue(self.fixture.preferences.entered.wait(2))
        close = threading.Thread(target=lambda: (self.service.close(), finished.set()))
        close.start()
        try:
            self.assertFalse(finished.wait(0.1))
        finally:
            self.fixture.preferences.release.set()
            thread.join(6)
            close.join(6)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_native_executable_refuses_unapproved_entry_and_pair_is_not_adhoc(self):
        from relay.mac_identity import native_bridge
        self.assertEqual(dict(native_bridge().trustedPair()), {})
        identity = subprocess.run([str(ROOT / 'build/macos-native/RelayHelper'), '--identity'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(identity.stdout)['identifier'], 'com.wangxinlei.relay.helper')
        refused = subprocess.run([str(ROOT / 'build/macos-native/RelayHelper')], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 77)
        pair = subprocess.run([str(ROOT / 'build/macos-native/RelayHelper'), '--verify-pair'], capture_output=True, text=True)
        self.assertEqual(pair.returncode, 1)
        self.assertEqual(json.loads(pair.stdout), {'trusted_pair': False})

    def test_real_agent_over_native_helper_transport(self):
        from relay.mutations import MacMutationWriter
        mac, agent, fix = self.fixture.build_agent()
        fix.mutation_writer = MacMutationWriter(self.transport.perform, lambda *_: dict(self.fixture.target), coordinator=self.transport)
        run = agent.propose(['dns_mixed_on_vpn'])
        agent.execute(run, agent.authorize(run))
        self.assertEqual(run.outcome, 'verified')
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertFalse(self.transport.call('status')['batch_active'])
        self.assertEqual(mac.mutations, [])

    def test_agent_unconfirmed_end_preserves_pending_recovery_phase(self):
        from relay.mutations import MacMutationWriter
        _mac, agent, fix = self.fixture.build_agent()
        fix.mutation_writer = MacMutationWriter(self.transport.perform, lambda *_: dict(self.fixture.target), coordinator=self.transport)
        run = agent.propose(['dns_mixed_on_vpn'])
        with patch.object(self.transport, 'end', side_effect=TimeoutError('lost')):
            agent.execute(run, agent.authorize(run))
        self.assertEqual(run.outcome, 'rollback_failed')
        record = json.loads(next(fix.snapshot_dir.glob('*.json')).read_text())
        self.assertEqual(record['phase'], 'needs_verification')
        self.assertEqual(record['helper_settlement'], 'unconfirmed')
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_recovery_restore_requires_review_then_keeps_batch_until_settled(self):
        request, restore = self.begin()
        batch_id = self.transport.context['batch_id']
        self.transport.perform(request)
        review = self.transport.inspect_recovery(batch_id)
        self.assertTrue(review['can_restore'])
        self.assertEqual(review['fields'][0]['actual'], request['desired'])
        self.assertNotIn('SearchDomains', json.dumps(review))
        result = self.transport.recover(review, 'restore')
        self.assertEqual(result['state'], 'ready_to_settle')
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.transport.perform(restore)['error'], 'recovery_in_progress')
        closed = self.transport.end_recovery()
        self.assertEqual(closed['disposition'], 'restored')
        self.assertFalse(closed['network_verified'])
        self.assertEqual(self.fixture.preferences.snapshot['configuration']['ServerAddresses'], request['expected'])
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertFalse(self.transport.call('status')['batch_active'])

    def test_unknown_apply_can_be_retained_without_repeating_or_inventing_success(self):
        request, _restore = self.begin()
        self.fixture.preferences.apply_ok = False
        self.assertEqual(self.transport.perform(request)['state'], 'needs_verification')
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        self.assertTrue(review['can_retain'])
        self.assertEqual(self.transport.recover(review, 'retain')['state'], 'ready_to_settle')
        self.assertEqual(self.transport.end_recovery()['disposition'], 'retained')
        record = json.loads(next(self.fixture.directory.glob('*.json')).read_text())
        self.assertEqual(record['phase'], 'completed')
        self.assertEqual(record['result']['state'], 'needs_verification')
        self.assertFalse(record['result']['network_verified'])
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_another_client_cannot_take_live_lease_but_can_recover_after_expiry(self):
        request, restore = self.begin()
        self.transport.perform(request)
        batch_id = self.transport.context['batch_id']
        other = self.new_client()
        review = other.inspect_recovery(batch_id)
        self.assertFalse(review['eligible'])
        self.assertFalse(review['can_restore'])
        refused = other.call('recovery_apply', {'batch_id': batch_id, 'review_id': review['review_id'], 'choice': 'restore'})
        self.assertEqual(refused['error'], 'recovery_unavailable')
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.service.setBatchDeadline_(0)
        review = other.inspect_recovery(batch_id)
        other.recover(review, 'restore')
        self.assertEqual(self.transport.perform(restore)['error'], 'recovery_in_progress')
        self.assertEqual(other.end_recovery()['disposition'], 'restored')
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_helper_restart_allows_fresh_review_not_old_token_or_forward_replay(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        batch_id = self.transport.context['batch_id']
        old = self.transport.inspect_recovery(batch_id)
        self.restart_helper()
        self.transport.endpoint = self.listener.endpoint()
        with self.assertRaises(PermissionError):
            self.transport.recover(old, 'restore')
        other = self.new_client()
        review = other.inspect_recovery(batch_id)
        self.assertTrue(review['eligible'])
        self.assertNotEqual(review['helper_instance'], old['helper_instance'])
        other.recover(review, 'restore')
        other.end_recovery()
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_changed_configuration_invalidates_review_and_never_overwrites_third_party(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        batch_id = self.transport.context['batch_id']
        review = self.transport.inspect_recovery(batch_id)
        self.fixture.preferences.snapshot['configuration']['SearchDomains'] = ['changed.invalid']
        with self.assertRaises(PermissionError):
            self.transport.recover(review, 'restore')
        self.fixture.preferences.snapshot['configuration']['ServerAddresses'] = ['9.9.9.9']
        review = self.transport.inspect_recovery(batch_id)
        self.assertEqual(review['fields'][0]['state'], 'drift')
        self.assertFalse(review['can_restore'])
        self.assertFalse(review['can_retain'])
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertTrue(self.transport.call('status')['batch_active'])

    def test_recovery_review_is_one_use_and_cannot_be_shared_between_clients(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        other = self.new_client()
        with self.assertRaises(PermissionError):
            other.recover(review, 'restore')
        self.transport.recover(review, 'restore')
        with self.assertRaises(PermissionError):
            self.transport.recover(review, 'restore')
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertTrue(self.transport.call('status')['batch_active'])

    def test_lost_recovery_reply_is_resolved_by_readonly_closure_not_replay(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        original = self.transport.call
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise EOFError('synthetic lost reply')
        with patch.object(self.transport, 'call', side_effect=lost):
            with self.assertRaises(EOFError):
                self.transport.recover(review, 'restore')
        self.assertIsNotNone(self.transport.recovery_context)
        self.assertEqual(self.transport.end_recovery()['disposition'], 'restored')
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_restart_after_compensation_needs_no_second_configuration_write(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        batch_id = self.transport.context['batch_id']
        self.transport.recover(self.transport.inspect_recovery(batch_id), 'restore')
        self.restart_helper()
        other = self.new_client()
        review = other.inspect_recovery(batch_id)
        self.assertEqual(review['fields'][0]['state'], 'original')
        other.recover(review, 'restore')
        other.end_recovery()
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertTrue(all(json.loads(path.read_text())['phase'] == 'completed'
                            for path in self.fixture.directory.glob('*.json')))

    def test_uncertain_compensation_is_settled_from_actual_original_value(self):
        request, _restore = self.begin()
        self.transport.perform(request)
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        self.fixture.preferences.apply_ok = False
        result = self.transport.recover(review, 'restore')
        self.assertEqual(result['state'], 'needs_verification')
        self.assertEqual(self.transport.end_recovery()['disposition'], 'restored')
        self.assertTrue(all(json.loads(path.read_text())['phase'] == 'completed'
                            for path in self.fixture.directory.glob('*.json')))
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_not_started_batch_is_closed_without_reading_or_writing_network(self):
        self.begin()
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        self.assertFalse(review['can_retain'])
        self.assertTrue(review['can_restore'])
        self.transport.recover(review, 'restore')
        self.assertEqual(self.transport.end_recovery()['disposition'], 'not_started')
        self.assertEqual(self.fixture.preferences.reads, 0)
        self.assertEqual(self.fixture.preferences.writes, 0)

    def test_unknown_readback_offers_no_recovery_and_other_user_gets_no_private_data(self):
        from test_mutation_native import native_dictionary
        request, _restore = self.begin()
        self.transport.perform(request)
        batch_id = self.transport.context['batch_id']
        self.fixture.preferences.fail_readback = True
        review = self.transport.inspect_recovery(batch_id)
        self.assertEqual(review['fields'][0]['state'], 'unknown')
        self.assertFalse(review['can_restore'])
        self.assertFalse(review['can_retain'])
        control = json.loads((self.fixture.directory / 'helper-control').read_text())
        control['batch']['user'] = os.geteuid() + 1
        self.service.setState_(native_dictionary(control).mutableCopy())
        self.assertEqual(self.transport.call('recovery_inspect', {'batch_id': batch_id}), {'error': 'batch_not_found'})

    def test_terminal_history_survives_later_batches_and_crash_before_control_clear(self):
        from test_mutation_native import native_dictionary
        request, _restore = self.begin()
        batch_id = self.transport.context['batch_id']
        self.transport.perform(request)
        control = json.loads((self.fixture.directory / 'helper-control').read_text())
        self.transport.end('verified')
        self.fixture.executor.saveControl_(native_dictionary(control))
        self.restart_helper()
        other = self.new_client()
        self.assertFalse(other.call('status')['batch_active'])
        self.assertEqual(other.inspect_recovery(batch_id)['disposition'], 'retained')
        other.begin(uuid.uuid4().hex, [{'apply': request, 'restore': self.fixture.restore(request)}])
        other.end('verified')
        self.assertEqual(other.inspect_recovery(batch_id)['disposition'], 'retained')
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_agent_readback_does_not_clear_pending_helper_batch(self):
        from relay.mutations import MacMutationWriter
        _mac, agent, fix = self.fixture.build_agent()
        fix.mutation_writer = MacMutationWriter(self.transport.perform, lambda *_: dict(self.fixture.target), coordinator=self.transport)
        run = agent.propose(['dns_mixed_on_vpn'])
        with patch.object(self.transport, 'end', side_effect=TimeoutError('lost')):
            agent.execute(run, agent.authorize(run))
        agent.observe()
        self.assertEqual(agent.store.get(run.id)['stage'], 'needs_reconciliation')
        self.assertTrue(agent.recovery_pending)
        review = self.transport.inspect_recovery(self.transport.context['batch_id'])
        self.transport.recover(review, 'retain')
        self.transport.end_recovery()
        agent.observe()
        self.assertEqual(agent.store.get(run.id)['stage'], 'finished')
        self.assertEqual(agent.store.get(run.id)['outcome'], 'verified')
        self.assertFalse(agent.recovery_pending)
        self.assertEqual(self.fixture.preferences.writes, 1)


if __name__ == '__main__':
    unittest.main()
