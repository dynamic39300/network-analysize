"""Scoped trust through real IPC and real journals; all network writes are simulated."""
import copy
from dataclasses import replace
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.ipc import LocalClient, RpcError
from relay.models import ModelConsent, ResponsesModel
from relay.trust import MODES
import test_ipc as ipc_fixture
from test_dynamic import request
from test_guard import Clock
from test_reasoning import Script, conclusion


class TrustTests(unittest.TestCase):
    setUp = ipc_fixture.CoreServiceTests.setUp
    make_service = ipc_fixture.CoreServiceTests.make_service
    wait = ipc_fixture.CoreServiceTests.wait
    prepare = ipc_fixture.CoreServiceTests.prepare

    @staticmethod
    def params(review, mode='continuous'):
        return {key: review[key] for key in ('run_id', 'proposal_hash', 'review_token')} | {'mode': mode, 'acknowledge': True}

    def grant(self, mode='continuous', review=None):
        review = review or self.prepare()
        result = self.wait(self.client.call('confirm_trust', self.params(review, mode)))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.runtime.agent._last_run.outcome, 'verified')
        return review

    def recurrence(self):
        self.mac.manual_dns = ['8.8.8.8']
        self.mac.has_applied = False

    def test_default_no_authority_and_review_has_exact_effects_and_fixed_limits(self):
        review = self.prepare()
        self.assertTrue(review['trust_offer']['available'])
        scope = review['trust_offer']['scope']
        self.assertEqual(scope['actions'][0]['desired'], ['10.0.0.66', '10.0.0.68'])
        self.assertNotIn('before', scope['actions'][0])
        self.assertTrue(scope['actions'][0]['environment']['wifi_interface'])
        self.assertEqual(review['trust_offer']['modes'], MODES)
        self.assertFalse(self.mac.mutations)
        self.assertIsNone(self.client.call('settings')['trust']['grant'])

    def test_continuous_repairs_later_task_with_distinct_one_use_grants_and_receipts(self):
        self.grant()
        first = copy.deepcopy(self.runtime.agent._last_run.receipt)
        policy = self.runtime.trust.status()['grant']
        self.assertFalse(self.runtime.guard.schedule.enabled)
        self.assertIsNone(self.runtime.model_consent)
        self.recurrence()
        done = self.wait(self.client.call('investigate'))
        self.assertEqual(done['state'], 'completed')
        run = self.runtime.agent._last_run
        self.assertEqual(run.outcome, 'verified')
        self.assertEqual(run.receipt['authorization'], {'mode': 'continuous', 'trust_id': policy['id']})
        self.assertNotEqual(first['grant_id'], run.receipt['grant_id'])
        self.assertEqual(self.runtime.store.trust_record()['used'], 2)
        self.assertEqual(self.runtime.store.trust_history()[0]['event'], 'reserved')
        with self.assertRaises(PermissionError):
            self.runtime.agent.execute(run)

    def test_readonly_check_does_not_use_existing_trust(self):
        self.grant()
        self.recurrence()
        count = len(self.mac.mutations)
        self.wait(self.client.call('check'))
        self.assertEqual(len(self.mac.mutations), count)
        self.assertEqual(self.runtime.trust.status()['grant']['used'], 1)

    def test_task_trust_ends_with_task_and_does_not_cover_new_incident(self):
        self.grant('task')
        self.assertEqual(self.runtime.trust.status()['grant']['reason'], 'task_finished')
        self.recurrence()
        count = len(self.mac.mutations)
        self.wait(self.client.call('investigate'))
        self.assertEqual(len(self.mac.mutations), count)
        self.assertEqual(self.runtime.agent._last_run.stage, 'awaiting_authorization')

    def test_same_model_task_can_use_trust_for_second_matching_plan(self):
        document = self.runtime.profiles.document(self.runtime.profiles.active['id'])
        document['targets'] = document['targets'][:1]
        self.runtime.profiles.save(document)
        def recurrence():
            self.recurrence()
            return ('refresh_environment', {})
        script = Script([('propose_repair', {'issues': ['dns_mixed_on_vpn']}), recurrence,
                         ('propose_repair', {'issues': ['dns_mixed_on_vpn']}),
                         ('finish', conclusion('resolved', evidence_ids=['e6']))])
        model = ResponsesModel('test', 'http://127.0.0.1:9999/responses', transport=script)
        self.runtime.model, self.runtime.model_consent = model, ModelConsent(model)
        done = self.wait(self.client.call('investigate'))
        review = self.client.call('review', {'run_id': done['run_id']})
        self.grant('task', review)
        run = self.runtime.agent._last_run
        self.assertEqual(run.id, done['run_id'])
        self.assertEqual(len(run.execution_history), 1)
        self.assertEqual(self.runtime.trust.status()['grant']['used'], 2)
        self.assertEqual(self.runtime.trust.status()['grant']['reason'], 'task_finished')
        self.assertEqual(len(script.requests), 4)
        self.assertEqual(run.stop_reason, 'verified_healthy')

    def test_dynamic_cannot_create_or_reuse_scope_trust(self):
        self.grant()
        run = self.runtime.prepare_command(request(self.runtime.profiles.active['targets'][0]['id']))
        review = self.client.call('review', {'run_id': run.id})
        self.assertFalse(review['trust_offer']['available'])
        with self.assertRaises(RpcError):
            self.client.call('confirm_trust', self.params(review))
        self.assertEqual(self.runtime.trust.reason(run), 'unsupported_action')
        with self.assertRaises(PermissionError):
            self.runtime.agent.authorize(run, trust_id=self.runtime.trust.current['id'], acknowledge_unrestricted=True)

    def test_changed_target_profile_environment_and_privilege_fields_escalate(self):
        self.grant()
        self.recurrence()
        run, _ = self.runtime.propose()
        self.assertEqual(self.runtime.trust.reason(run), '')
        for key, value in [('desired', ['9.9.9.9']), ('service', 'Other'), ('health_profile', {'id': 'elsewhere'}),
                           ('environment', {'wifi_ip': '192.168.0.107', 'wifi_network': 'Other', 'default_interface': 'en1'}),
                           ('requires_admin', True)]:
            changed = copy.deepcopy(run)
            action = {**changed.proposals[0].actions[0], key: value}
            changed.proposals = (replace(changed.proposals[0], actions=(action,)),)
            self.assertNotEqual(self.runtime.trust.reason(changed), '', key)
        changed = copy.deepcopy(run)
        changed.snapshot['status']['vpn_client'] = 'Unapproved VPN'
        self.assertEqual(self.runtime.trust.reason(changed), 'scope_changed')
        self.assertFalse(self.runtime.trust.offer(replace(run, snapshot={**run.snapshot, 'check_errors': {'wifi': 'failed'}}))['available'])

    def test_additional_mature_field_does_not_inherit_dns_scope(self):
        self.grant()
        self.runtime.config.config['ipv6']['should_be'] = 'off'
        count = len(self.mac.mutations)
        self.wait(self.client.call('investigate'))
        self.assertEqual(len(self.mac.mutations), count)
        self.assertEqual(self.runtime.agent._last_run.authorization_status['reason'], 'scope_changed')

    def test_vpn_identity_change_before_proxy_write_blocks_trusted_action(self):
        self.mac.proxies = {'http': {'enabled': True, 'host': '127.0.0.1', 'port': 7890}}
        self.mac.proxy_reachable = False
        run, _ = self.runtime.propose(['proxy_leftover'])
        self.service.refresh()
        review = self.client.call('review', {'run_id': run.id})
        original = self.runtime.fix._assert_action_context
        def drift(action, before_write=False):
            if before_write:
                self.mac.services = {}
            return original(action, before_write)
        with patch.object(self.runtime.fix, '_assert_action_context', side_effect=drift):
            self.wait(self.client.call('confirm_trust', self.params(review)))
        self.assertFalse(self.mac.mutations)
        self.assertTrue(self.mac.proxies['http']['enabled'])
        self.assertEqual(run.outcome, 'blocked')

    def test_redacted_detail_retains_permission_enum_without_sensitive_header_fields(self):
        from relay.commercial.history import redact
        from relay.task_details import detail_text, task_detail
        self.grant()
        run = self.runtime.agent._last_run
        detail = task_detail(self.runtime.store.get(run.id), redact)
        self.assertEqual(detail['executions'][0]['basis']['mode'], 'continuous')
        self.assertIn('限定范围持续信任', detail_text(detail))
        self.assertEqual(redact({'authorization': 'private'}), {'authorization': '[已隐藏]'})

    def test_revoked_expired_clock_rollback_and_quota_stop_future_execution(self):
        self.grant()
        self.recurrence()
        run, _ = self.runtime.propose()
        trust = self.runtime.trust
        with patch.object(trust, 'clock', return_value=trust.current['expires_at']):
            self.assertEqual(trust.reason(run), 'expired')
        with patch.object(trust, 'clock', return_value=trust.current['created_at'] - 1):
            self.assertEqual(trust.reason(run), 'expired')
        with patch.object(trust, 'monotonic', return_value=trust.deadline):
            self.assertEqual(trust.reason(run), 'expired')
        trust.current['used'] = trust.current['limit']
        self.assertEqual(trust.reason(run), 'exhausted')
        self.assertEqual(trust.status()['grant']['state'], 'exhausted')
        self.client.call('revoke_trust')
        self.assertEqual(trust.reason(run), 'revoked')

    def test_real_budget_reservations_never_refunded_or_exceed_limit(self):
        review = self.prepare()
        run = self.runtime.agent._last_run
        trust = self.runtime.trust
        trust.create(run, 'task', review['trust_offer']['scope_hash'], 'test-user', trust.revision)
        for _ in range(3):
            self.runtime.agent.authorize(run, trust_id=trust.current['id'])
            run.stage = 'awaiting_authorization'
        with self.assertRaises(PermissionError):
            self.runtime.agent.authorize(run, trust_id=trust.current['id'])
        self.assertEqual(self.runtime.store.trust_record()['used'], 3)
        self.assertFalse(self.mac.mutations)

    def test_reservation_storage_failure_blocks_before_first_write(self):
        review = self.prepare()
        original = self.runtime.store.save_trust
        def failure(grant, event, *args):
            if event == 'reserved':
                raise OSError('full')
            return original(grant, event, *args)
        with patch.object(self.runtime.store, 'save_trust', side_effect=failure):
            done = self.wait(self.client.call('confirm_trust', self.params(review)))
        self.assertEqual(done['state'], 'failed')
        self.assertFalse(self.mac.mutations)
        self.assertFalse(self.runtime.trust.status()['available'])

    def test_revoke_survives_journal_failure_and_cannot_restore_on_restart(self):
        self.grant()
        with patch.object(self.runtime.store, 'save_trust', side_effect=OSError('full')):
            done = self.client.call('revoke_trust')
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(self.runtime.trust.status()['grant']['state'], 'revoked')
        self.service.close()
        self.make_service()
        self.assertNotEqual(self.runtime.trust.status()['grant']['state'], 'active')
        self.recurrence()
        count = len(self.mac.mutations)
        self.wait(self.client.call('investigate'))
        self.assertEqual(len(self.mac.mutations), count)

    def test_restart_keeps_audit_but_does_not_restore_execution_authority(self):
        self.grant()
        policy_id = self.runtime.trust.current['id']
        self.service.close()
        self.make_service()
        status = self.client.call('settings')
        self.assertEqual(status['trust']['grant']['id'], policy_id)
        self.assertEqual(status['trust']['grant']['state'], 'suspended')
        self.assertFalse(self.runtime.guard.schedule.enabled)
        self.assertTrue(status['authorization_events'])

    def test_close_releases_runtime_even_when_revoke_audit_cannot_be_saved(self):
        self.grant()
        with patch.object(self.runtime.store, 'save_trust', side_effect=OSError('full')):
            self.service.close()
        self.assertTrue(self.runtime.closed)
        self.assertIsNone(self.runtime.store._lock_fd)
        self.make_service()
        self.assertEqual(self.runtime.trust.status()['grant']['state'], 'suspended')

    def test_scope_review_token_is_client_bound_one_use_and_strict(self):
        review = self.prepare()
        other = LocalClient(self.data)
        with self.assertRaises(RpcError):
            other.call('confirm_trust', self.params(review))
        for extra in ({'acknowledge': False}, {'mode': 'unrestricted'}, {'scope': {'all': True}}):
            with self.assertRaises(RpcError):
                self.client.call('confirm_trust', {**self.params(review), **extra})
        request_id = uuid.uuid4().hex
        done = self.wait(self.client.call('confirm_trust', self.params(review), request_id=request_id))
        count = len(self.mac.mutations)
        self.assertEqual(self.client.call('confirm_trust', self.params(review), request_id=request_id), done)
        self.assertEqual(len(self.mac.mutations), count)
        with self.assertRaises(RpcError):
            self.client.call('confirm_trust', self.params(review))

    def test_revoke_invalidates_accepted_but_not_yet_started_trust(self):
        review = self.prepare()
        with self.runtime.gate:
            op = self.client.call('confirm_trust', self.params(review))
            self.client.call('revoke_trust')
        self.assertEqual(self.wait(op)['state'], 'failed')
        self.assertIsNone(self.runtime.trust.current)
        self.assertFalse(self.mac.mutations)

    def test_revoke_between_writes_prevents_next_write_and_still_rolls_back(self):
        self.runtime.config.config['ipv6']['should_be'] = 'off'
        run, _ = self.runtime.propose()
        self.service.refresh()
        review = self.client.call('review', {'run_id': run.id})
        self.assertEqual(len(review['trust_offer']['scope']['actions']), 2)
        original = self.runtime.fix._write_field
        def revoke_after(field, value, service):
            original(field, value, service)
            self.runtime.trust.revoke()
        before = list(self.mac.manual_dns)
        with patch.object(self.runtime.fix, '_write_field', side_effect=revoke_after):
            self.wait(self.client.call('confirm_trust', self.params(review)))
        self.assertEqual(self.mac.ipv6, 'Automatic')
        self.assertEqual(self.mac.manual_dns, before)
        self.assertEqual(run.outcome, 'rolled_back')
        self.assertEqual(run.receipt['changes'][0]['after'], before)

    def test_failed_verification_suspends_automation_instead_of_retrying(self):
        self.mac.fail_apply = True
        review = self.prepare()
        self.wait(self.client.call('confirm_trust', self.params(review)))
        self.assertEqual(self.runtime.agent._last_run.outcome, 'rolled_back')
        self.assertEqual(self.runtime.trust.status()['grant']['reason'], 'execution_not_verified')
        self.mac.fail_apply = False
        count = len(self.mac.mutations)
        self.wait(self.client.call('investigate'))
        self.assertEqual(len(self.mac.mutations), count)

    def test_guard_uses_existing_trust_and_pause_during_preflight_stops_write(self):
        self.grant()
        clock = Clock()
        self.runtime.guard.schedule.clock = clock
        self.recurrence()
        self.runtime.start_guard()
        clock.advance(5)
        original_write = self.runtime.fix._write_field
        observed = []
        def observe(*args):
            observed.append(self.client.call('status'))
            return original_write(*args)
        with patch.object(self.runtime.fix, '_write_field', side_effect=observe):
            self.runtime.guard.tick()
        self.assertEqual(self.runtime._guard_run.outcome, 'verified')
        self.assertEqual(observed[0]['active_execution']['phase'], 'applying')
        self.assertEqual(observed[0]['busy'], 'trusted_execution')
        self.assertEqual(observed[0]['run_stage'], 'executing')
        from relay.overview import overview
        self.assertEqual(overview(observed[0])['title'], '正在处理范围内的问题')
        self.assertIsNone(self.client.call('status')['active_execution'])
        self.recurrence()
        count = len(self.mac.mutations)
        original = self.runtime.fix.fix_all
        def pause(*args, **kwargs):
            paused = self.client.call('guard', {'enabled': False})
            self.assertEqual(paused['state'], 'completed')
            return original(*args, **kwargs)
        self.runtime.guard.changed()
        clock.advance(300)
        with patch.object(self.runtime.fix, 'fix_all', side_effect=pause):
            self.runtime.guard.tick()
        self.assertEqual(len(self.mac.mutations), count)
        self.assertFalse(self.runtime.guard.schedule.enabled)


if __name__ == '__main__':
    unittest.main()
