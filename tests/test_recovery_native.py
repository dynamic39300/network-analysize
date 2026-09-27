"""Desktop IPC -> Core -> authenticated native helper, with memory-only networking."""
import copy
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native recovery')
class NativeRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_helper_native import NativeHelperTests
        NativeHelperTests.setUpClass.__func__(cls)

    def setUp(self):
        from test_helper_native import NativeHelperTests
        from test_diagnostics import FakeMac, config
        NativeHelperTests.setUp(self)
        self.mac = FakeMac()
        self.configuration = config(self.mac, company=True)
        self.fixture.preferences.snapshot['configuration']['ServerAddresses'] = list(self.mac.manual_dns)
        def committed(snapshot):
            self.mac.manual_dns = list(snapshot['configuration'].get('ServerAddresses', []))
            self.mac.has_applied = True
        self.fixture.preferences.on_commit = committed
        self.make_core()

    def make_core(self):
        from relay.core import CoreRuntime
        from relay.core_service import CoreService
        from relay.ipc import LocalClient
        from relay.mutations import MacMutationWriter
        from test_windows import FakeEvents
        self.runtime = CoreRuntime(self.fixture.directory / 'core', platform='darwin', runner=self.mac,
            config=self.configuration, events_factory=FakeEvents, guard_threaded=False,
            execution_directory=self.fixture.directory / 'execution')
        self.runtime.fix.mutation_writer = MacMutationWriter(self.transport.perform,
            lambda *_: dict(self.fixture.target), coordinator=self.transport)
        self.core = CoreService(self.runtime)
        self.core.start()
        self.addCleanup(self.core.close)
        self.client = LocalClient(self.runtime.data_dir)

    def pending(self):
        run = self.runtime.agent.propose(['dns_mixed_on_vpn'])
        with patch.object(self.transport, 'end', side_effect=TimeoutError('synthetic lost closure')):
            self.runtime.agent.execute(run, self.runtime.agent.authorize(run))
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertEqual(self.fixture.preferences.writes, 1, run.events)
        self.core.refresh()
        return run

    def review(self, run):
        return self.client.call('receipt_review', {'run_id': run.id})

    def confirm(self, review, choice='restore', request_id=None):
        return self.client.call('confirm_recovery', self.params(review, choice), request_id)

    @staticmethod
    def params(review, choice='restore'):
        return {key: review[key] for key in ('run_id', 'receipt_hash', 'review_token')} | {
            'choice': choice, 'acknowledge': True}

    def wait(self, operation):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            value = self.client.call('operation', {'id': operation['id']})
            if value['state'] not in ('accepted', 'running') and not self.client.call('status')['busy']:
                return value
            time.sleep(0.01)
        self.fail('Recovery operation did not finish')

    def test_restore_updates_original_task_and_health_without_replaying_plan(self):
        run = self.pending()
        original = copy.deepcopy(run.receipt)
        review = self.review(run)
        self.assertNotIn('review_id', review['recovery'])
        self.assertNotIn('requests', review['recovery'])
        self.assertFalse(review['can_acknowledge'])
        self.assertTrue(review['recovery']['can_restore'])
        self.assertEqual(self.wait(self.confirm(review))['state'], 'completed')
        self.assertEqual(run.outcome, 'restored')
        self.assertEqual(run.stage, 'finished')
        self.assertEqual(run.receipt, original)
        self.assertFalse(self.runtime.store.needs_reconciliation())
        self.assertFalse(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertFalse(self.mac.mutations)
        saved = json.loads(Path(run.receipt['recovery_path']).read_text())
        self.assertEqual(saved['helper_recoveries'][-1]['state'], 'settled')
        self.assertFalse(saved['helper_recoveries'][-1]['closure']['network_verified'])
        self.runtime.agent._save(run, required=True)
        self.assertEqual(self.runtime.store.get(run.id)['outcome'], 'restored')

    def test_retain_needs_fresh_network_verification_and_makes_no_extra_write(self):
        run = self.pending()
        self.assertEqual(self.wait(self.confirm(self.review(run), 'retain'))['state'], 'completed')
        self.assertEqual(run.outcome, 'verified')
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertFalse(self.runtime.store.needs_reconciliation())

    def test_failed_retain_keeps_batch_for_later_explicit_restore(self):
        run = self.pending()
        self.mac.break_network_after_apply = True
        self.assertEqual(self.wait(self.confirm(self.review(run), 'retain'))['state'], 'completed')
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertEqual(self.wait(self.confirm(self.review(run)))['state'], 'completed')
        self.assertEqual(run.outcome, 'restored')
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_core_review_is_exact_client_bound_and_single_use(self):
        from relay.ipc import LocalClient, RpcError
        run = self.pending()
        review = self.review(run)
        params = self.params(review)
        with self.assertRaisesRegex(RpcError, 'review_required'):
            LocalClient(self.runtime.data_dir).call('confirm_recovery', params)
        for changes in ({'acknowledge': False}, {'choice': 'apply'}, {'requests': []}, {'receipt_hash': '0' * 64}):
            with self.assertRaises(RpcError):
                self.client.call('confirm_recovery', {**params, **changes})
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertEqual(self.wait(self.confirm(review))['state'], 'completed')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.confirm(review)
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_changed_receipt_or_profile_blocks_before_native_write(self):
        run = self.pending()
        review = self.review(run)
        record = self.runtime.store.get(run.id)
        record['receipt']['changed'] = True
        with self.runtime.store._connect() as db:
            db.execute('UPDATE runs SET payload=? WHERE id=?', (json.dumps(record), run.id))
        self.assertEqual(self.wait(self.confirm(review))['state'], 'failed')
        review = self.review(run)
        document = self.runtime.profiles.document(self.runtime.profiles.active['id'])
        self.runtime.profiles.save({**document, 'name': 'Changed current profile'})
        self.assertEqual(self.wait(self.confirm(review))['state'], 'failed')
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_expiry_cancel_and_changed_private_manifest_never_write(self):
        from relay.ipc import RpcError
        run = self.pending()
        review = self.review(run)
        self.core.reviews[review['review_token']]['expires_at'] = time.time() - 1
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.confirm(review)
        review = self.review(run)
        self.client.call('cancel')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.confirm(review)
        review = self.review(run)
        path = Path(run.receipt['recovery_path'])
        saved = json.loads(path.read_text())
        saved['system_mutations']['operations']['dns']['restore_id'] = uuid.uuid4().hex
        self.runtime.fix._persist(saved, path)
        self.assertEqual(self.wait(self.confirm(review))['state'], 'failed')
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_external_change_after_review_is_preserved_and_options_disable(self):
        run = self.pending()
        review = self.review(run)
        current = ['8.8.4.4']
        self.fixture.preferences.snapshot['configuration']['ServerAddresses'] = current
        self.assertEqual(self.wait(self.confirm(review))['state'], 'failed')
        recovery = self.review(run)['recovery']
        self.assertEqual(recovery['fields'][0]['actual'], current)
        self.assertFalse(recovery['can_restore'])
        self.assertFalse(recovery['can_retain'])
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_lost_recovery_reply_is_not_replayed_and_fresh_review_finishes_readonly(self):
        run = self.pending()
        review, request_id = self.review(run), uuid.uuid4().hex
        original = self.transport.call
        def lost(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == 'recovery_apply':
                raise EOFError('synthetic lost response')
            return result
        with patch.object(self.transport, 'call', side_effect=lost):
            done = self.wait(self.confirm(review, request_id=request_id))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertEqual(self.confirm(review, request_id=request_id), done)
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.wait(self.confirm(self.review(run)))['state'], 'completed')
        self.assertEqual(run.outcome, 'restored')
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_cancel_during_network_verification_leaves_receipts_for_fresh_review(self):
        run = self.pending()
        review = self.review(run)
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.engine.run_all
        def delayed(**kwargs):
            result = original(**kwargs)
            entered.set()
            release.wait(5)
            return result
        with patch.object(self.runtime.engine, 'run_all', side_effect=delayed):
            operation = self.confirm(review)
            try:
                self.assertTrue(entered.wait(5))
                self.client.call('cancel')
            finally:
                release.set()
            self.assertEqual(self.wait(operation)['state'], 'failed')
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertEqual(self.wait(self.confirm(self.review(run)))['state'], 'completed')
        self.assertEqual(self.fixture.preferences.writes, 2)

    def test_core_and_helper_restart_require_new_review_not_saved_authority(self):
        from relay.ipc import RpcError
        from test_helper_native import NativeHelperTests
        run = self.pending()
        old_review = self.review(run)
        self.core.close()
        NativeHelperTests.restart_helper(self)
        self.transport = NativeHelperTests.new_client(self)
        self.make_core()
        with self.assertRaises(RpcError):
            self.confirm(old_review)
        self.assertEqual(self.fixture.preferences.writes, 1)
        self.assertEqual(self.wait(self.confirm(self.review(run)))['state'], 'completed')
        self.assertEqual(self.runtime.store.get(run.id)['outcome'], 'restored')
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.assertFalse(self.runtime.trust.current)

    def test_local_intent_failure_prevents_restore_and_preserves_pending_batch(self):
        run = self.pending()
        review = self.review(run)
        with patch.object(self.runtime.fix, '_persist', side_effect=OSError('synthetic journal failure')):
            self.assertEqual(self.wait(self.confirm(review))['state'], 'failed')
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_lost_terminal_reply_is_resolved_readonly_from_durable_helper_history(self):
        run = self.pending()
        review = self.review(run)
        original = self.transport.call
        def lost(method, *args, **kwargs):
            value = original(method, *args, **kwargs)
            if method == 'recovery_end':
                raise EOFError('synthetic lost terminal response')
            return value
        with patch.object(self.transport, 'call', side_effect=lost):
            self.assertEqual(self.wait(self.confirm(review))['state'], 'failed')
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertFalse(self.transport.call('status')['batch_active'])
        self.assertEqual(self.fixture.preferences.writes, 2)
        self.runtime.agent.observe()
        self.assertEqual(self.runtime.store.get(run.id)['outcome'], 'restored')
        self.assertFalse(self.runtime.store.needs_reconciliation())
        self.assertEqual(self.fixture.preferences.writes, 2)

        following = self.runtime.agent.propose(['dns_mixed_on_vpn'])
        self.runtime.agent.execute(following, self.runtime.agent.authorize(following))
        self.assertEqual(following.outcome, 'verified')
        self.assertEqual(self.fixture.preferences.writes, 3)

    def test_native_history_mismatch_or_unavailability_does_not_clear_local_task(self):
        run = self.pending()
        self.runtime.agent.observe()
        self.assertTrue(self.runtime.store.needs_reconciliation())
        original = self.transport.inspect_recovery
        def mismatched(batch_id, **kwargs):
            value = copy.deepcopy(original(batch_id, **kwargs))
            value['requests'][0]['apply']['proposal'] = '0' * 64
            return value
        with patch.object(self.transport, 'inspect_recovery', side_effect=mismatched):
            self.assertEqual(self.review(run)['recovery']['state'], 'unavailable')
            self.runtime.agent.observe()
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_unaccepted_begin_is_sealed_readonly_and_next_authorized_task_runs(self):
        run = self.runtime.agent.propose(['dns_mixed_on_vpn'])
        original = self.transport.call
        def lost(method, *args, **kwargs):
            if method == 'begin':
                raise EOFError('synthetic request lost before admission')
            return original(method, *args, **kwargs)
        with patch.object(self.transport, 'call', side_effect=lost):
            self.runtime.agent.execute(run, self.runtime.agent.authorize(run))
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertEqual(self.fixture.preferences.writes, 0)
        context = copy.deepcopy(self.transport.context)
        self.runtime.agent.observe()
        self.assertEqual(self.runtime.store.get(run.id)['outcome'], 'not_started')
        self.assertFalse(self.runtime.store.needs_reconciliation())
        self.assertIsNone(self.transport.context)
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))
        with self.assertRaises(PermissionError):
            self.transport.begin(context['batch_id'], context['requests'])
        following = self.runtime.agent.propose(['dns_mixed_on_vpn'])
        self.runtime.agent.execute(following, self.runtime.agent.authorize(following))
        self.assertEqual(following.outcome, 'verified')
        self.assertEqual(self.fixture.preferences.writes, 1)

    def test_lost_sealing_reply_is_resolved_from_history_without_replay(self):
        run = self.runtime.agent.propose(['dns_mixed_on_vpn'])
        original = self.transport.call
        with patch.object(self.transport, 'call', side_effect=EOFError('synthetic unavailable helper')):
            self.runtime.agent.execute(run, self.runtime.agent.authorize(run))
            self.runtime.agent.observe()
        self.assertTrue(self.runtime.store.needs_reconciliation())
        context = copy.deepcopy(self.transport.context)
        def lost(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == 'recovery_inspect':
                raise EOFError('synthetic lost proof reply')
            return result
        with patch.object(self.transport, 'call', side_effect=lost):
            self.runtime.agent.observe()
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertEqual(self.transport.context, context)
        self.runtime.agent.observe()
        self.assertEqual(self.runtime.store.get(run.id)['outcome'], 'not_started')
        self.assertIsNone(self.transport.context)
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_accepted_begin_with_lost_reply_stays_pending_until_explicit_review(self):
        run = self.runtime.agent.propose(['dns_mixed_on_vpn'])
        original = self.transport.call
        def lost(method, *args, **kwargs):
            if method == 'end':
                raise TimeoutError('synthetic unavailable closure')
            result = original(method, *args, **kwargs)
            if method == 'begin':
                raise EOFError('synthetic reply lost after admission')
            return result
        with patch.object(self.transport, 'call', side_effect=lost):
            self.runtime.agent.execute(run, self.runtime.agent.authorize(run))
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.runtime.agent.observe()
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertTrue(self.transport.call('status')['batch_active'])
        self.assertEqual(self.wait(self.confirm(self.review(run)))['state'], 'completed')
        self.assertEqual(run.outcome, 'not_started')
        self.assertFalse(self.runtime.store.needs_reconciliation())
        self.assertEqual((self.fixture.preferences.reads, self.fixture.preferences.writes), (0, 0))

    def test_connected_cocoa_recovery_uses_same_core_and_native_helper(self):
        from AppKit import NSApplication
        from Foundation import NSDate, NSRunLoop
        from relay.remote_desktop import build_connected_app_class
        from desktop_fixture import offline_desktop
        NSApplication.sharedApplication()
        run = self.pending()
        app = build_connected_app_class(self.runtime.data_dir, desktop_factory=offline_desktop)()
        def until(condition):
            deadline = time.monotonic() + 6
            while not condition() and time.monotonic() < deadline:
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.03))
            self.assertTrue(condition())
        try:
            until(lambda: app.controller.ui_state()['ready'])
            app.controller.review_receipt(run.id)
            until(lambda: app.receipt.review is not None)
            self.assertFalse(app.receipt.restore.isEnabled())
            app.receipt.acknowledge.setState_(1)
            app.receipt.actions.restore_(None)
            until(lambda: self.runtime.store.get(run.id)['stage'] == 'finished')
            self.assertEqual(run.outcome, 'restored')
            self.assertFalse(self.runtime.store.needs_reconciliation())
            self.assertFalse(self.transport.call('status')['batch_active'])
            self.assertEqual(self.fixture.preferences.writes, 2)
            self.assertFalse(self.mac.mutations)
        finally:
            app.controller.close()
            app.controller.thread.join(6)
            for view in (app.workspace, app.review, app.records, app.report, app.profiles,
                         app.settings, app.privacy, app.receipt, app.task_report):
                view.window.setDelegate_(None)
                view.window.orderOut_(None)


if __name__ == '__main__':
    unittest.main()
