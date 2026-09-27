"""Local desktop management contracts; no host network changes or cloud requests."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
import test_ipc as fixtures
from relay.ipc import LocalClient, RpcError
from relay.models import ModelConsent, ResponsesModel
from test_dynamic import request
from test_reasoning import Script, conclusion


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged Unix transport')
class ManagementTests(unittest.TestCase):
    setUp = fixtures.CoreServiceTests.setUp
    make_service = fixtures.CoreServiceTests.make_service
    wait = fixtures.CoreServiceTests.wait
    prepare = fixtures.CoreServiceTests.prepare

    def configure(self, **changes):
        value = {'name': 'test-model', 'endpoint': 'https://example.invalid/v1/responses', 'api_key': 'secret-key-test',
                 'credential_action': 'replace', 'revision': self.client.call('settings')['model']['revision']}
        operation = self.client.call('model_configure', {**value, **changes})
        return self.wait(operation)

    def consent(self, review=None, client=None):
        review = review or self.client.call('consent_review')
        return (client or self.client).call('confirm_consent', {
            key: review[key] for key in ('revision', 'binding', 'review_token')} | {'allow_upload': True})

    def dynamic(self):
        target = self.runtime.profiles.active['targets'][0]['id']
        run = self.runtime.prepare_command(request(target, "print('private-output-8247')"))
        self.runtime.execute_command(run, self.runtime.agent._proposal_hash(run), acknowledge_unrestricted=True)
        self.service.refresh()
        return run

    def acknowledge(self, review, **changes):
        params = {key: review[key] for key in ('run_id', 'receipt_hash', 'review_token')} | {
            'acknowledge': True, 'note': 'Reviewed the temporary test command and its output.'}
        return self.client.call('confirm_receipt', {**params, **changes})

    def test_model_configuration_does_not_grant_upload_or_persist_credential(self):
        self.assertEqual(self.configure()['state'], 'completed')
        settings = self.client.call('settings')
        self.assertTrue(settings['model']['credential_present'])
        self.assertFalse(settings['model']['consented'])
        self.assertFalse(self.mac.calls)
        self.assertFalse(self.runtime.guard.schedule.enabled)
        self.assertNotIn('secret-key-test', json.dumps(settings))
        self.assertNotIn(b'secret-key-test', self.runtime.store.path.read_bytes())
        self.assertEqual(self.runtime.store.model_configuration(), {
            'name': 'test-model', 'endpoint': 'https://example.invalid/v1/responses'})
        self.assertEqual(self.wait(self.consent())['state'], 'completed')
        self.assertTrue(self.client.call('status')['model']['consented'])
        self.assertFalse(self.mac.calls)

    def test_consent_is_instance_client_revision_and_purpose_bound(self):
        self.configure()
        review = self.client.call('consent_review')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.consent(review, LocalClient(self.data))
        self.assertEqual(self.configure(name='replacement')['state'], 'completed')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.consent(review)
        review = self.client.call('consent_review')
        self.service.reviews[review['review_token']]['kind'] = 'receipt'
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.consent(review)
        review = self.client.call('consent_review')
        self.service.reviews[review['review_token']]['expires_at'] = time.time() - 1
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.consent(review)

    def test_endpoint_changes_cannot_inherit_a_credential_and_restart_has_no_consent(self):
        self.configure()
        self.wait(self.consent())
        self.assertEqual(self.configure(endpoint='https://other.invalid/v1/responses',
            credential_action='keep', api_key='')['state'], 'failed')
        self.assertEqual(self.runtime.model.endpoint, 'https://example.invalid/v1/responses')
        self.assertTrue(self.runtime.model_consent.allows(self.runtime.model))
        self.assertEqual(self.configure(name='replacement', credential_action='keep', api_key='')['state'], 'completed')
        self.assertTrue(self.runtime.model.credential_present)
        self.assertFalse(self.client.call('status')['model']['consented'])
        self.service.close()
        self.make_service()
        model = self.client.call('settings')['model']
        self.assertEqual(model['name'], 'replacement')
        self.assertFalse(model['credential_present'])
        self.assertFalse(model['consented'])
        self.assertEqual(self.wait(self.consent())['state'], 'failed')

    def test_credential_in_endpoint_invalid_url_and_extra_fields_never_persist(self):
        for changes in ({'endpoint': 'https://example.invalid/secret-key-test/responses'},
                        {'endpoint': 'http://example.invalid/v1/responses'},
                        {'endpoint': 'https://user:pass@example.invalid/responses'}):
            self.assertEqual(self.configure(**changes)['state'], 'failed')
            self.assertIsNone(self.runtime.store.model_configuration())
        with self.assertRaisesRegex(RpcError, 'invalid_request'):
            self.client.call('preferences_save', {'revision': 0, 'values': {
                'history_days': 7, 'history_limit': 100, 'notifications_enabled': True, 'auto_fix': True}})
        self.assertFalse(self.mac.calls)
        self.configure()
        self.assertEqual(self.configure(endpoint='https://example.invalid/secret-key-test/responses',
                                       credential_action='forget', api_key='')['state'], 'failed')
        self.assertNotIn(b'secret-key-test', self.runtime.store.path.read_bytes())

    def test_revoke_and_forget_invalidate_pending_consent_and_are_durably_audited(self):
        self.configure()
        review = self.client.call('consent_review')
        self.client.call('revoke_model')
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.consent(review)
        self.wait(self.consent())
        self.client.call('forget_credential')
        status = self.client.call('status')['model']
        self.assertFalse(status['credential_present'])
        self.assertFalse(status['consented'])
        self.assertEqual(self.client.call('settings')['privacy_events'][0]['action'], 'credential_forgotten')
        self.assertNotIn(b'secret-key-test', self.runtime.store.path.read_bytes())

    def test_busy_investigation_can_revoke_without_waiting_for_runtime_gate(self):
        entered, release = threading.Event(), threading.Event()
        cancelled = []
        def transport(*args, **kwargs):
            entered.set()
            release.wait(5)
            cancelled.append(not kwargs['allowed']())
            return Script([('finish', conclusion('needs_user'))])(*args, **kwargs)
        model = ResponsesModel('test', 'http://127.0.0.1:9999/v1/responses', transport=transport)
        self.runtime.model, self.runtime.model_consent = model, ModelConsent(model)
        operation = self.client.call('investigate')
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.client.call('revoke_model')['state'], 'completed')
            self.assertFalse(self.client.call('status')['model']['consented'])
        finally:
            release.set()
        self.wait(operation)
        self.assertEqual(cancelled, [True])

    def test_revocation_survives_audit_failure_and_operation_is_terminal(self):
        self.configure()
        self.wait(self.consent())
        with patch.object(self.runtime.store, 'privacy_event', side_effect=OSError('private path')):
            result = self.client.call('revoke_model')
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(self.client.call('status')['model']['consented'])
        self.assertEqual(self.client.call('operation', {'id': result['id']})['state'], 'failed')

    def test_revocation_is_effective_even_when_request_journal_cannot_accept_it(self):
        self.configure()
        self.wait(self.consent())
        with patch.object(self.runtime.store, 'save_ipc_request', side_effect=OSError('cannot save')):
            with self.assertRaises(RpcError):
                self.client.call('forget_credential')
        model = self.client.call('status')['model']
        self.assertFalse(model['consented'])
        self.assertFalse(model['credential_present'])

    def test_receipt_review_keeps_private_output_local_and_synchronizes_live_task(self):
        run = self.dynamic()
        records = self.client.call('records')
        self.assertEqual(records['pending'][0]['id'], run.id)
        self.assertNotIn('private-output-8247', json.dumps(records))
        review = self.client.call('receipt_review', {'run_id': run.id})
        self.assertIn('private-output-8247', json.dumps(review))
        self.assertTrue(review['can_acknowledge'])
        self.assertEqual(self.wait(self.acknowledge(review))['state'], 'completed')
        self.assertEqual(run.outcome, 'reviewed')
        self.assertFalse(run.manual_review['command_effects_verified'])
        self.assertFalse(self.client.call('status')['agent']['recovery_pending'])
        self.assertFalse(self.client.call('records')['pending'])
        self.runtime.agent._save(run, required=True)
        self.assertEqual(self.runtime.store.get(run.id)['manual_review'], run.manual_review)
        with self.assertRaisesRegex(RpcError, 'review_required'):
            self.acknowledge(review)
        self.assertFalse(self.mac.mutations)

    def test_receipt_review_rejects_changed_hash_profile_and_missing_acknowledgment(self):
        run = self.dynamic()
        review = self.client.call('receipt_review', {'run_id': run.id})
        for changes in ({'note': '   '}, {'acknowledge': False}, {'receipt_hash': '0' * 64}):
            with self.assertRaises(RpcError):
                self.acknowledge(review, **changes)
        record = self.runtime.store.get(run.id)
        record['receipt']['unreviewed_change'] = True
        with self.runtime.store._connect() as db:
            db.execute('UPDATE runs SET payload=? WHERE id=?', (json.dumps(record), run.id))
        self.assertEqual(self.wait(self.acknowledge(review))['state'], 'failed')
        review = self.client.call('receipt_review', {'run_id': run.id})
        document = self.runtime.profiles.document(self.runtime.profiles.active['id'])
        self.runtime.profiles.save({**document, 'name': 'Changed profile'})
        self.assertEqual(self.wait(self.acknowledge(review))['state'], 'failed')
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertFalse(self.mac.mutations)

    def test_old_pending_receipts_remain_visible_outside_recent_list(self):
        run = self.dynamic()
        for i in range(51):
            self.runtime.store.save(replace(run, id=f'finished-{i}', stage='finished', receipt=None))
        records = self.client.call('records')
        self.assertNotIn(run.id, [row['id'] for row in records['records']])
        self.assertEqual(records['pending'][0]['id'], run.id)

    def test_cancelling_human_review_during_fresh_check_keeps_recovery_block(self):
        run = self.dynamic()
        review = self.client.call('receipt_review', {'run_id': run.id})
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.engine.run_all
        def delayed(**kwargs):
            snapshot = original(**kwargs)
            entered.set()
            release.wait(5)
            return snapshot
        with patch.object(self.runtime.engine, 'run_all', side_effect=delayed):
            operation = self.acknowledge(review)
            try:
                self.assertTrue(entered.wait(5))
                self.client.call('cancel')
            finally:
                release.set()
            self.assertEqual(self.wait(operation)['state'], 'failed')
        self.assertTrue(self.runtime.store.needs_reconciliation())
        self.assertFalse(self.runtime.store.get(run.id).get('manual_review'))
        self.assertFalse(self.mac.mutations)

    def test_retention_preferences_are_versioned_and_never_purge_unresolved_tasks(self):
        values = {'history_days': 7, 'history_limit': 100, 'notifications_enabled': True}
        saved = self.wait(self.client.call('preferences_save', {'revision': 0, 'values': values}))
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(self.client.call('status')['preferences'], {'revision': 1, 'values': values})
        stale = self.wait(self.client.call('preferences_save', {'revision': 0, 'values': values}))
        self.assertEqual(stale['state'], 'failed')
        run, _ = self.runtime.check(bounded=True)
        old = replace(run, id='old-finished', stage='finished')
        pending = replace(run, id='old-pending', stage='needs_reconciliation')
        self.runtime.store.save(old)
        self.runtime.store.save(pending)
        with self.runtime.store._connect() as db:
            db.execute('UPDATE runs SET updated=? WHERE id IN (?, ?)',
                       (time.time() - 10 * 86400, old.id, pending.id))
        for i in range(102):
            self.runtime.store.save(replace(run, id=f'finished-{i}', stage='finished'))
        rows = self.runtime.store.recent(500)
        self.assertEqual(len(rows), 101)
        self.assertIn(pending.id, [row['id'] for row in rows])
        self.assertNotIn(old.id, [row['id'] for row in rows])
        self.assertFalse(self.mac.mutations)

    def test_opening_other_review_kinds_does_not_discard_pending_proposal(self):
        self.configure()
        proposal = self.prepare()
        self.client.call('consent_review')
        confirmation = fixtures.CoreServiceTests.confirmation(proposal, False)
        self.assertEqual(self.wait(self.client.call('confirm', confirmation))['state'], 'completed')


class ManagementControllerTests(unittest.TestCase):
    def test_notifications_are_opt_in_idle_and_only_on_meaningful_changes(self):
        from relay.remote_controller import RemoteController
        events = []
        controller = RemoteController('/unused', lambda kind, payload: events.append((kind, payload)),
                                      lambda fn, *args: fn(*args))
        state = {'ready': True, 'busy': '', 'agent': {'incident_id': 'incident-1'}, 'report': {'stage': 'observed'},
                 'preferences': {'values': {'notifications_enabled': True}}}
        controller._notification(state)
        state['report']['stage'] = 'awaiting_authorization'
        controller._notification({**state, 'busy': 'investigate'})
        self.assertFalse(events)
        controller._notification(state)
        controller._notification(state)
        self.assertEqual(len(events), 1)
        state['report']['stage'] = 'needs_reconciliation'
        controller._notification(state)
        self.assertEqual(len(events), 2)
        state['preferences']['values']['notifications_enabled'] = False
        state['agent']['incident_id'] = 'incident-2'
        controller._notification(state)
        self.assertEqual(len(events), 2)

    def test_stale_core_cannot_submit_receipt_or_consent(self):
        from relay.remote_controller import RemoteController
        controller = RemoteController('/unused', lambda *_: None, lambda fn, *args: fn(*args))
        review = {'core_instance': 'old'}
        self.assertFalse(controller.confirm_receipt(review, 'note'))
        self.assertFalse(controller.confirm_consent(review))
        self.assertTrue(controller.queue.empty())


if __name__ == '__main__':
    unittest.main()
