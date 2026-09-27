"""Goal-first presentation and persisted evidence/differences, using fake networking."""
import copy
from datetime import datetime
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
from relay.commercial.history import redact
from relay.models import ModelConsent, ResponsesModel
from relay.overview import overview
from relay.task_details import detail_text, task_detail
from test_panel import sample_state
from test_reasoning import Script, conclusion, hypothesis


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged Unix transport')
class TaskDetailTests(unittest.TestCase):
    setUp = fixtures.CoreServiceTests.setUp
    make_service = fixtures.CoreServiceTests.make_service
    wait = fixtures.CoreServiceTests.wait
    prepare = fixtures.CoreServiceTests.prepare

    def repair(self):
        review = self.prepare()
        self.wait(self.client.call('confirm', fixtures.CoreServiceTests.confirmation(review)))
        return self.runtime.store.get(review['run_id'])

    def test_verified_readback_is_durable_and_detail_lookup_does_not_probe(self):
        record = self.repair()
        row = record['receipt']['changes'][0]
        self.assertEqual(row['before'], ['8.8.8.8'])
        self.assertEqual(row['after'], self.mac.manual_dns)
        self.assertTrue(row['after_known'])
        self.assertEqual(row['readback_phase'], 'verification')
        recovery = json.loads(Path(record['receipt']['recovery_path']).read_text())
        self.assertEqual(recovery['changes'][0], row)
        before = list(self.mac.calls)
        detail = self.client.call('task_detail', {'run_id': record['id']})
        self.assertEqual(self.mac.calls, before)
        self.assertNotIn('8.8.8.8', json.dumps(detail))
        self.assertNotIn('recovery_path', json.dumps(detail))
        self.assertEqual(detail['executions'][0]['changes'][0]['comparison'], 'requested')
        self.assertIn('回读与计划值一致', detail_text(detail))

    def test_rollback_diff_is_readback_not_requested_value(self):
        self.mac.fail_apply = True
        record = self.repair()
        self.assertEqual(record['outcome'], 'rolled_back')
        row = record['receipt']['changes'][0]
        self.assertTrue(row['after_known'])
        self.assertEqual(row['after'], row['before'])
        self.assertNotEqual(row['after'], row['requested'])
        self.assertEqual(row['readback_phase'], 'rollback')
        self.assertEqual(task_detail(record, redact)['executions'][0]['changes'][0]['comparison'], 'original')

    def test_failed_rollback_never_invents_a_final_value(self):
        self.mac.fail_apply = self.mac.fail_rollback = True
        record = self.repair()
        self.assertEqual(record['outcome'], 'rollback_failed')
        row = record['receipt']['changes'][0]
        self.assertFalse(row['after_known'])
        self.assertIsNone(row['after'])
        self.assertIn('实际回读：未确认', detail_text(task_detail(record, redact)))
        self.assertTrue(self.runtime.store.needs_reconciliation())

    def test_hypotheses_and_evidence_refs_keep_their_status_without_tool_arguments(self):
        script = Script([('record_hypothesis', hypothesis(state='supported')),
                         ('probe_targets', {'targets': ['t1']}), ('finish', conclusion(evidence_ids=['e2']))])
        model = ResponsesModel('test', 'http://127.0.0.1:9999/v1/responses', transport=script)
        self.runtime.model, self.runtime.model_consent = model, ModelConsent(model)
        done = self.wait(self.client.call('investigate'))
        detail = self.client.call('task_detail', {'run_id': done['run_id']})
        self.assertEqual([row['id'] for row in detail['evidence']], ['e1', 'e2'])
        self.assertTrue(detail['evidence'][1]['partial'])
        self.assertEqual(detail['hypotheses'][0]['evidence_ids'], ['e1'])
        self.assertFalse(detail['hypotheses'][0]['verified_root_cause'])
        self.assertIn('非已确认根因', detail_text(detail))
        self.assertIn('模型结论（不替代实测', detail_text(detail))
        self.assertNotIn('arguments', json.dumps(detail))
        self.assertFalse(self.mac.mutations)

    def test_projection_excludes_private_output_and_notes_and_handles_missing_redactor(self):
        run, _ = self.runtime.check(bounded=True)
        record = self.runtime.store.get(run.id)
        record['receipt'] = {'kind': 'dynamic_command', 'job': {'stdout': 'private-value'}, 'outcome': 'needs_review'}
        record['manual_review'] = {'note': 'private-value'}
        record['tool_steps'] = [{'tool': 'propose_command', 'arguments': {'argv': ['private-value']}, 'state': 'completed'}]
        for redactor in (redact, None):
            detail = task_detail(record, redactor)
            self.assertNotIn('private-value', json.dumps(detail))
            self.assertNotIn('snapshot', json.dumps(detail))
        self.assertIn('任意副作用未自动验证', detail_text(task_detail(record, redact)))
        self.assertFalse(task_detail(record, None)['steps'])

    def test_legacy_and_multiple_receipts_do_not_fabricate_missing_differences(self):
        record = self.repair()
        previous = copy.deepcopy(record['receipt'])
        record['execution_history'] = [previous]
        record['receipt'].pop('changes')
        detail = task_detail(record, redact)
        self.assertEqual(len(detail['executions']), 2)
        self.assertTrue(detail['executions'][0]['changes'])
        self.assertFalse(detail['executions'][1]['changes'])
        self.assertIn('不能将计划值当成实际修改结果', detail_text(detail))

    def test_committed_details_and_privacy_remain_readable_while_probe_is_busy(self):
        record = self.repair()
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.engine.run_all
        def delayed(**kwargs):
            entered.set()
            release.wait(5)
            return original(**kwargs)
        with patch.object(self.runtime.engine, 'run_all', side_effect=delayed):
            operation = self.client.call('check')
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.client.call('task_detail', {'run_id': record['id']})['record']['id'], record['id'])
                self.assertTrue(self.client.call('records')['records'])
                self.assertFalse(self.client.call('settings')['model']['consented'])
            finally:
                release.set()
            self.wait(operation)


class OverviewTests(unittest.TestCase):
    def state(self):
        state = sample_state()
        state['agent'] = {'profile': {'health': 'healthy', 'name': 'Test profile', 'covered': 1, 'total': 1,
            'observed_at': time.time(), 'targets': [{'id': 'target', 'name': 'Company', 'state': 'healthy',
                'label': '符合预期', 'summary': '已验证', 'expected_path': 'vpn'}]}}
        return state

    def test_target_status_uses_profile_not_green_technical_checks(self):
        state = self.state()
        state['agent']['profile']['health'] = 'degraded'
        state['agent']['profile']['covered'] = 0
        state['agent']['profile']['targets'][0].update(state='degraded', label='不可用')
        result = overview(state)
        self.assertIn('需要关注', result['title'])
        self.assertEqual(result['coverage'], '0/1 个目标符合预期')
        self.assertEqual(result['targets'][0]['path_label'], '已确认 VPN')

    def test_expired_and_disconnected_observations_are_not_currently_healthy(self):
        state = self.state()
        state['agent']['profile']['observed_at'] -= 700
        result = overview(state)
        self.assertEqual(result['targets'][0]['state'], 'stale')
        self.assertIn('未确认', result['title'])
        self.assertEqual(state['agent']['profile']['targets'][0]['state'], 'healthy')
        state.update(ready=False, connection='disconnected')
        result = overview(state)
        self.assertEqual(result['targets'][0]['state'], 'unknown')
        self.assertTrue(all(row['tone'] == 'neutral' for row in result['checks']))

    def test_pending_recovery_first_run_and_model_outage_are_distinct(self):
        state = self.state()
        state['snapshot']['last_check'] = None
        self.assertIn('尚未检测', overview(state)['title'])
        state['snapshot']['last_check'] = datetime.now()
        state['run_stage'] = 'awaiting_authorization'
        self.assertIn('等待确认', overview(state)['title'])
        state['agent']['recovery_pending'] = True
        self.assertIn('需要核对', overview(state)['title'])
        state['snapshot']['last_check'] = None
        self.assertIn('需要核对', overview(state)['title'])
        state['report'] = {'model': {'state': 'limited', 'reason': 'connection_unavailable'}}
        self.assertEqual(overview(state)['reason'], '模型连接不可用')


if __name__ == '__main__':
    unittest.main()
