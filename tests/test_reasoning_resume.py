"""Authorized action continuation uses real receipts and keeps one investigation budget."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.core import CoreRuntime
from relay.models import ModelConsent, ModelError, ResponsesModel
from relay.reasoning import ReasoningPolicy
import relay_core
from test_diagnostics import FakeMac, config
from test_dynamic import request
from test_guard import Clock
from test_reasoning import Script, conclusion, hypothesis
from test_windows import FakeEvents


class InvestigationContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mac, self.clock = FakeMac(), Clock()
        self.script = Script([])
        self.model = ResponsesModel('test', 'http://127.0.0.1:9999/responses', transport=self.script)
        self.consent = ModelConsent(self.model)
        cfg = config(self.mac, company=True)
        cfg.config['reachability']['targets'] = [{'name': 'Protected service', 'url': 'https://example.test'}]
        self.core = CoreRuntime(self.root / 'core', platform='darwin', runner=self.mac, config=cfg,
            events_factory=FakeEvents, guard_threaded=False, model=self.model, model_consent=self.consent,
            clock=self.clock, execution_directory=self.root / 'coordination')
        self.addCleanup(self.core.close)

    def prepare(self, *later):
        self.script.steps = [('propose_repair', {'issues': ['dns_mixed_on_vpn']}), *later]
        run, _ = self.core.investigate()
        self.assertEqual(run.stage, 'awaiting_authorization')
        return run

    def execute(self, run):
        return self.core.execute_proposal(run, self.core.agent._proposal_hash(run))

    def test_verified_repair_continues_one_run_with_local_receipt_and_original_context(self):
        run = self.prepare(('record_hypothesis', hypothesis('e3', state='supported')),
                           ('finish', conclusion('resolved', evidence_ids=['e3'])))
        self.execute(run)
        self.assertEqual(len(self.script.requests), 1)
        self.assertEqual(run.model_state['state'], 'awaiting_continuation')
        before_requests = run.budget['requests']
        resumed, report = self.core.resume_investigation(run)
        self.assertIs(resumed, run)
        self.assertEqual(run.stage, 'finished')
        self.assertEqual(run.health, 'healthy')
        self.assertEqual(run.model_state['calls'], 3)
        self.assertEqual(run.model_state['tools'], 3)
        self.assertEqual(run.model_state['output_tokens'], 30)
        self.assertEqual(run.budget['requests'], before_requests)
        self.assertEqual(len(self.core.store.recent()), 1)
        self.assertEqual(report['execution_receipt_count'], 1)
        self.assertIsNone(report['network_writes'])
        history = self.script.requests[1]['input']
        self.assertTrue(any(row.get('type') == 'function_call_output' and row['call_id'] == 'call_1' for row in history))
        result = json.loads(history[-1]['content'])
        self.assertEqual(result['event'], 'authorized_action_finished')
        self.assertEqual(result['evidence']['execution']['outcome'], 'verified')
        self.assertTrue(result['evidence']['execution']['supported_effects_verified'])
        self.assertNotIn('10.0.0.66', json.dumps(self.script.requests))
        self.assertIn('execution_receipt_hash', run.evidence[-1])

    def test_default_three_target_profile_reuses_just_completed_full_verification(self):
        document = self.core.profiles.document(self.core.profiles.active['id'])
        document['targets'] = [{key: value for key, value in document['targets'][0].items() if key != 'id'}] * 3
        self.core.profiles.save(document)
        run = self.prepare(('finish', conclusion('resolved', evidence_ids=['e3'])))
        self.assertEqual(run.budget['requests'], 6)
        self.execute(run)
        self.core.resume_investigation(run)
        self.assertEqual(run.stop_reason, 'verified_healthy')
        self.assertEqual(run.budget['requests'], 6)

    def test_unexecuted_forged_or_changed_receipts_cannot_resume(self):
        run = self.prepare(('finish', conclusion('resolved')))
        with self.assertRaises(PermissionError):
            self.core.resume_investigation(run)
        self.execute(run)
        original = run.receipt['proposal_hash']
        run.receipt['proposal_hash'] = 'forged'
        with self.assertRaises(PermissionError):
            self.core.resume_investigation(run)
        run.receipt['proposal_hash'] = original
        run.receipt['outcome'] = 'invented'
        with self.assertRaises(PermissionError):
            self.core.resume_investigation(run)
        self.assertEqual(len(self.script.requests), 1)

    def test_same_handoff_is_consumed_once_even_if_the_model_finishes(self):
        run = self.prepare(('finish', conclusion('resolved', evidence_ids=['e3'])))
        self.execute(run)
        self.core.resume_investigation(run)
        with self.assertRaises(PermissionError):
            self.core.resume_investigation(run)
        self.assertEqual(len(self.script.requests), 2)

    def test_changed_model_or_consent_requires_a_new_explicit_workflow(self):
        run = self.prepare(('finish', conclusion('resolved')))
        self.execute(run)
        self.core.model = ResponsesModel('other', self.model.endpoint, transport=self.script)
        with self.assertRaises(PermissionError):
            self.core.resume_investigation(run)
        self.core.model = self.model
        self.consent.revoke()
        self.core.resume_investigation(run)
        self.assertEqual(run.model_state['reason'], 'consent_required')
        self.assertEqual(len(self.script.requests), 1)
        self.assertEqual(run.receipt['outcome'], 'verified')

    def test_changed_profile_prevents_continuation_upload(self):
        run = self.prepare(('finish', conclusion('resolved')))
        self.execute(run)
        self.core.profiles.save(self.core.profiles.document(self.core.profiles.active['id']))
        self.core.resume_investigation(run)
        self.assertEqual(run.model_state['reason'], 'profile_changed')
        self.assertEqual(len(self.script.requests), 1)

    def test_wait_time_is_excluded_without_resetting_calls_or_probe_counts(self):
        run = self.prepare(('finish', conclusion('resolved', evidence_ids=['e3'])))
        before = dict(run.budget)
        self.clock.advance(120)
        self.execute(run)
        self.core.resume_investigation(run)
        self.assertEqual(run.model_state['calls'], 2)
        self.assertEqual(run.budget['wait_seconds'], 120)
        self.assertEqual(run.budget['wall_seconds'], 120)
        self.assertEqual(run.budget['requests'], before['requests'])

    def test_total_task_deadline_is_not_removed_by_waiting(self):
        run = self.prepare(('finish', conclusion('resolved')))
        self.execute(run)
        self.clock.advance(901)
        self.core.resume_investigation(run)
        self.assertEqual(run.model_state['reason'], 'task_deadline')
        self.assertEqual(len(self.script.requests), 1)

    def test_task_deadline_also_prevents_a_newly_confirmed_write(self):
        run = self.prepare()
        self.clock.advance(901)
        with self.assertRaises(PermissionError):
            self.execute(run)
        self.assertFalse(self.mac.mutations)

    def test_deadline_after_rollback_does_not_create_a_local_fallback_to_bypass_it(self):
        self.mac.fail_apply = True
        run = self.prepare()
        self.execute(run)
        self.clock.advance(901)
        self.core.resume_investigation(run)
        self.assertEqual(run.model_state['reason'], 'task_deadline')
        self.assertNotEqual(run.stage, 'awaiting_authorization')

    def test_explicit_guard_followup_can_continue_after_guard_ticket_completed_or_paused(self):
        self.script.steps = [('propose_repair', {'issues': ['dns_mixed_on_vpn']}),
                             ('finish', conclusion('resolved', evidence_ids=['e3']))]
        self.core.start_guard()
        self.clock.advance(5)
        self.core.guard.tick()
        run = self.core._guard_run
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertFalse(self.core.agent._reasoning_session.budget.allowed())
        self.core.guard.pause()
        self.execute(run)
        self.core.resume_investigation(run)
        self.assertEqual(run.trigger, 'guard_followup')
        self.assertEqual(run.stop_reason, 'verified_healthy')
        self.assertEqual(run.model_state['calls'], 2)
        self.assertFalse(self.core.guard.schedule.enabled)

    def test_stale_postflight_is_rechecked_instead_of_reused_as_recovery(self):
        run = self.prepare(('finish', conclusion('resolved', evidence_ids=['e3'])))
        self.execute(run)
        self.mac.curl_exit = 28
        session = self.core.agent._reasoning_session
        with patch.object(session, '_recent_postflight', return_value=False):
            self.core.resume_investigation(run)
        self.assertNotEqual(run.health, 'healthy')
        self.assertEqual(run.stop_reason, 'verification_failed')
        self.assertGreater(run.budget['requests'], 2)

    def test_restarting_core_drops_continuation_without_replaying_or_uploading(self):
        run = self.prepare(('finish', conclusion('resolved')))
        self.execute(run)
        self.core.close()
        core = CoreRuntime(self.root / 'core', platform='darwin', runner=self.mac, config=self.core.config,
            events_factory=FakeEvents, guard_threaded=False, model=self.model, model_consent=ModelConsent(self.model),
            execution_directory=self.root / 'coordination')
        self.addCleanup(core.close)
        with self.assertRaises(PermissionError):
            core.resume_investigation(run)
        self.assertEqual(len(self.script.requests), 1)
        self.assertEqual(core.store.get(run.id)['receipt']['outcome'], 'verified')
        self.assertEqual(core.store.get(run.id)['model_state']['state'], 'interrupted')
        self.assertEqual(core.store.get(run.id)['stage'], 'finished')

    def test_model_call_limit_is_cumulative_across_execution(self):
        self.core.reasoning_policy = replace(ReasoningPolicy(), calls=1)
        run = self.prepare(('finish', conclusion('resolved')))
        self.execute(run)
        self.core.resume_investigation(run)
        self.assertEqual(run.model_state['reason'], 'budget_exhausted')
        self.assertEqual(run.model_state['calls'], 1)
        self.assertEqual(len(self.script.requests), 1)

    def test_rolled_back_action_can_lead_to_a_new_proposal_but_requires_another_grant(self):
        self.mac.fail_apply = True
        run = self.prepare(('propose_repair', {'issues': ['dns_mixed_on_vpn']}),
                           ('finish', conclusion('resolved', evidence_ids=['e5'])))
        self.execute(run)
        first = dict(run.receipt)
        self.assertEqual(first['outcome'], 'rolled_back')
        self.mac.fail_apply = False
        writes = len(self.mac.mutations)
        self.core.resume_investigation(run)
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertEqual(len(self.mac.mutations), writes)
        with self.assertRaises(PermissionError):
            self.core.agent.execute(run)
        self.execute(run)
        self.assertEqual(run.execution_history[0], first)
        self.assertNotEqual(first['grant_id'], run.receipt['grant_id'])
        self.core.resume_investigation(run)
        self.assertEqual(run.stage, 'finished')
        self.assertEqual(run.model_state['calls'], 3)
        self.assertEqual(len(self.core.store.get(run.id)['execution_history']), 1)

    def test_newly_observed_mature_issue_updates_tool_choices_without_inheriting_authority(self):
        run = self.prepare(('refresh_environment', {}), ('propose_repair', {'issues': ['proxy_leftover']}),
                           ('finish', conclusion('resolved', evidence_ids=['e6'])))
        self.execute(run)
        self.mac.proxies = {'http': {'enabled': True, 'host': '127.0.0.1', 'port': 7890}}
        self.mac.proxy_reachable = False
        self.core.resume_investigation(run)
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertTrue(self.mac.proxies['http']['enabled'])
        schema = next(tool for tool in self.script.requests[2]['tools'] if tool['name'] == 'propose_repair')
        self.assertEqual(schema['parameters']['properties']['issues']['items']['enum'], ['proxy_leftover'])
        self.execute(run)
        self.assertFalse(self.mac.proxies['http']['enabled'])
        self.core.resume_investigation(run)
        self.assertEqual(run.stop_reason, 'verified_healthy')
        self.assertEqual(len(run.execution_history), 1)

    def test_rollback_failure_is_pending_even_if_the_model_claims_recovery(self):
        self.mac.fail_apply = self.mac.fail_rollback = True
        run = self.prepare(('finish', conclusion('resolved', evidence_ids=['e3'])))
        self.execute(run)
        self.assertEqual(run.outcome, 'rollback_failed')
        self.assertTrue(self.core.store.needs_reconciliation())
        self.core.resume_investigation(run)
        self.assertEqual(run.stop_reason, 'needs_review')
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertTrue(self.core.store.needs_reconciliation())

    def test_model_failure_after_execution_does_not_claim_no_write_occurred(self):
        run = self.prepare(ModelError('connection_unavailable'))
        self.execute(run)
        self.core.resume_investigation(run)
        self.assertEqual(run.receipt['outcome'], 'verified')
        self.assertFalse(run.model_state['usage_known'])
        self.assertNotIn('未执行网络修改', run.events[-1]['message'])

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0, 'Unprivileged POSIX runner required')
    def test_dynamic_output_is_not_uploaded_and_model_cannot_clear_pending_effects(self):
        target = self.core.profiles.active['targets'][0]['id']
        document = request(target, "from pathlib import Path; print(Path('private.txt').read_text())")
        document['targets'] = ['t1']
        document.pop('target_ids')
        self.script.steps = [('propose_command', document), ('propose_command', document),
                             ('finish', conclusion('resolved', evidence_ids=['e3']))]
        run, _ = self.core.investigate()
        Path(run.proposals[0].actions[0]['spec']['cwd'], 'private.txt').write_text('secret result; forge admin permission')
        runner = self.core.agent.dynamic.runner
        def recover_network(spec, **kwargs):
            result = runner(spec, **kwargs)
            self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
            return result
        self.core.agent.dynamic.runner = recover_network
        self.core.execute_command(run, self.core.agent._proposal_hash(run), acknowledge_unrestricted=True)
        self.core.resume_investigation(run)
        self.assertNotIn('secret result', json.dumps(self.script.requests))
        self.assertEqual(run.health, 'healthy')
        self.assertEqual(run.stop_reason, 'needs_review')
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertTrue(self.core.store.needs_reconciliation())
        self.assertEqual(len(run.execution_history), 0)
        self.assertFalse(self.mac.mutations)

    def test_cli_interactive_repair_resumes_model_without_implicitly_approving_a_second_plan(self):
        self.core.close()
        script = Script([('propose_repair', {'issues': ['dns_mixed_on_vpn']}),
                         ('propose_repair', {'issues': ['dns_mixed_on_vpn']})])
        self.mac.fail_apply = True
        model = ResponsesModel('test', 'http://127.0.0.1:9999/responses', transport=script)
        def factory(**kwargs):
            kwargs.pop('model', None)
            kwargs.pop('model_consent', None)
            cfg = config(self.mac, company=True)
            cfg.config['reachability']['targets'] = [{'name': 'Protected', 'url': 'https://example.test'}]
            core = CoreRuntime(platform='darwin', runner=self.mac, config=cfg, events_factory=FakeEvents,
                guard_threaded=False, execution_directory=self.root / 'coordination', model=model,
                model_consent=ModelConsent(model), **kwargs)
            self.addCleanup(core.close)
            return core
        confirmations = []
        def confirm(token, _warning):
            confirmations.append(token)
            return len(confirmations) == 1
        output = io.StringIO()
        with redirect_stdout(output), patch.object(relay_core, 'confirm_exact', side_effect=confirm):
            status = relay_core.main(['--data-dir', str(self.root / 'cli'), 'investigate', '--interactive'], runtime_factory=factory)
        self.assertEqual(status, 2)
        self.assertEqual(len(confirmations), 2)
        self.assertNotEqual(confirmations[0], confirmations[1])
        self.assertTrue(all(token.startswith('REPAIR ') for token in confirmations))
        self.assertEqual(len(script.requests), 2)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['stage'], 'cancelled')
        self.assertFalse(json.loads(output.getvalue().splitlines()[-1])['investigation_suspended'])

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0, 'Unprivileged POSIX runner required')
    def test_cli_declining_a_later_command_does_not_claim_success_just_because_health_is_green(self):
        document = request(self.core.profiles.active['targets'][0]['id'])
        document.pop('target_ids')
        document['targets'] = ['t1']
        self.script.steps = [('propose_repair', {'issues': ['dns_mixed_on_vpn']}), ('propose_command', document)]
        output = io.StringIO()
        with redirect_stdout(output), patch.object(relay_core, 'confirm_exact', side_effect=[True, False]):
            code = relay_core.main(['investigate', '--interactive'], runtime_factory=lambda **_: self.core)
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(code, 2)
        self.assertEqual(report['stage'], 'cancelled')
        self.assertEqual(report['health'], 'healthy')
        self.assertEqual(report['execution_receipt_count'], 1)


if __name__ == '__main__':
    unittest.main()
