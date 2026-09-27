"""Panel semantics and real repair progress with a simulated macOS boundary."""
import copy
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.engine import DetectionEngine, FixEngine
from relay.presentation import diagnostic_rows, optimization_issues, panel_summary, scan_caption
from test_diagnostics import FakeMac


class Profile:
    def __init__(self):
        self.data = {
            'wifi': {'interface': 'en0', 'service_name': 'Wi-Fi'},
            'vpn': {'known_clients': [{'name': 'UniVPN', 'process': 'UniVPN', 'service_id': 'company-service'}],
                    'company_dns': ['10.0.0.66', '10.0.0.68']},
            'dns': {'public_dns': ['8.8.8.8'], 'enforce_company_dns_on_vpn': True},
            'ipv6': {'should_be': 'observe'},
            'reachability': {'targets': [{'name': 'Example', 'url': 'https://example.test', 'timeout': 1}]},
        }

    def get(self, key, default=None):
        value = self.data
        for part in key.split('.'):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def sample_state():
    return {'ready': True, 'busy': '', 'overall': 'warning', 'repair_options': {'dns_company_leftover': ['恢复自动 DNS']},
            'enabled_checks': ['wifi', 'vpn', 'proxy', 'system_proxy', 'dns', 'ipv6', 'reachability'],
            'snapshot': {'last_check': datetime.now(), 'check_errors': {},
                'issues': [('medium', 'dns_company_leftover', '公司 DNS 残留')],
                'status': {'wifi': 'ok', 'wifi_ip': '192.168.1.8', 'vpn': 'off', 'vpn_path': 'off',
                           'proxy_app': 'off', 'system_proxy': 'ok', 'proxy': 'off',
                           'dns': 'warning', 'dns_mode': 'manual', 'dns_effective_servers': ['10.0.0.66'],
                           'ipv6': 'on', 'ipv6_mode': 'Automatic', 'reachability': 'ok',
                           'reachability_results': {'Example': {'transport': 'ok', 'service': 'responding',
                                                                'http_status': 200, 'path': 'direct'}}}}}


class PresentationTests(unittest.TestCase):
    def row(self, state, name):
        return next(row for row in diagnostic_rows(state) if row['id'] == name)

    def test_idle_before_first_check_is_not_a_failure_or_success(self):
        state = sample_state()
        state.update(snapshot={'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None}, repair_options={})
        rows = diagnostic_rows(state)
        self.assertTrue(all(row['state'] == '待检测' and row['tone'] == 'neutral' and not row['action'] for row in rows))
        self.assertEqual(panel_summary(state)[0], '尚未检测')
        self.assertEqual(optimization_issues(state), [])

    def test_only_eligible_issue_has_repair_button(self):
        state = sample_state()
        dns = self.row(state, 'dns')
        self.assertEqual(dns['issue_types'], ['dns_company_leftover'])
        self.assertEqual(dns['action'], '立即帮我处理')
        state['repair_options'] = {}
        self.assertEqual(self.row(state, 'dns')['action'], '立即帮我处理')
        self.assertEqual(self.row(state, 'dns')['issue_types'], [])

    def test_disabled_unknown_and_pending_are_never_green(self):
        state = sample_state()
        state['snapshot']['status'].pop('vpn')
        state['enabled_checks'].remove('vpn')
        self.assertEqual(self.row(state, 'vpn')['state'], '未检测')
        state['snapshot']['check_errors']['dns'] = '命令超时'
        row = self.row(state, 'dns')
        self.assertEqual(row['state'], '未确认')
        self.assertEqual(row['issue_types'], [])
        state['live_snapshot'] = {'status': {}, 'issues': [], 'check_errors': {}}
        state['scan_progress'] = {'check': 'wifi', 'phase': 'running'}
        self.assertEqual(self.row(state, 'wifi')['state'], '检测中')
        self.assertEqual(self.row(state, 'dns')['state'], '待检测')

    def test_ipv6_and_disconnected_vpn_are_not_automatically_faults(self):
        state = sample_state()
        self.assertEqual(self.row(state, 'ipv6')['tone'], 'ok')
        self.assertEqual(self.row(state, 'vpn')['state'], '未启用')

    def test_service_denial_is_explained_separately_from_network_failure(self):
        state = sample_state()
        state['snapshot']['status']['reachability_results']['Example'].update(service='access_denied', http_status=403)
        evidence = self.row(state, 'reachability')['evidence']
        self.assertIn('网络可达，服务拒绝访问', evidence)
        self.assertIn('HTTP 403', evidence)

    def test_global_check_error_does_not_show_healthy_summary(self):
        state = sample_state()
        state['snapshot']['issues'] = []
        state['snapshot']['check_errors']['config'] = '配置无法读取'
        state['overall'] = 'unknown'
        self.assertIn('未完成', panel_summary(state)[0])

    def test_unrelated_check_error_blocks_individual_automatic_repair(self):
        state = sample_state()
        state['snapshot']['check_errors']['wifi'] = '检查超时'
        self.assertEqual(self.row(state, 'dns')['issue_types'], [])
        self.assertEqual(self.row(state, 'dns')['action'], '立即帮我处理')

    def test_numbering_matches_actual_serial_detection_order(self):
        rows = diagnostic_rows(sample_state())
        self.assertEqual([row['id'] for row in rows], DetectionEngine.CHECK_ORDER)
        self.assertEqual([row['number'] for row in rows], list(range(1, 8)))
        state = sample_state()
        state['scan_progress'] = {'check': 'dns', 'phase': 'running'}
        self.assertIn('04', scan_caption(state))
        self.assertNotIn('并行', scan_caption(state))

    def test_optimization_only_selects_current_eligible_problems(self):
        state = sample_state()
        state['repair_options']['no_longer_present'] = ['stale plan']
        state['snapshot']['issues'].append(('low', 'vpn_unconfirmed', 'VPN'))
        self.assertEqual(optimization_issues(state), ['dns_company_leftover'])
        state['busy'] = '网络检测'
        self.assertEqual(optimization_issues(state), [])
        state['busy'] = ''
        state['snapshot']['check_errors']['dns'] = '检查未完成'
        self.assertEqual(optimization_issues(state), [])

    def test_brief_issue_text_is_plain_language_and_preserves_detail(self):
        row = self.row(sample_state(), 'dns')
        self.assertIn('公司上网设置', row['summary'])
        self.assertNotIn('DNS', row['summary'])
        self.assertTrue(row['evidence'])

    def test_automatic_proxy_is_not_presented_as_verified(self):
        state = sample_state()
        state['snapshot']['status'].update(proxy_pac=True, proxy='on')
        row = self.row(state, 'system_proxy')
        self.assertEqual(row['state'], '待确认')
        self.assertIn('效果还需确认', row['summary'])


class RepairProgressTests(unittest.TestCase):
    def setUp(self):
        self.mac = FakeMac()
        self.mac.tunnels = {'utun8': '10.254.254.88'}
        self.mac.processes = ['UniVPN']
        self.mac.anchor_routes = {'10.0.0.66': 'utun8', '10.0.0.68': 'utun8'}
        self.mac.services = {'company-service': 'utun8'}
        self.mac.manual_dns = ['8.8.8.8']
        self.profile = Profile()
        self.engine = DetectionEngine(self.profile, runner=self.mac)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fix = FixEngine(self.profile, self.engine, snapshot_dir=self.temp.name)
        self.engine.run_all()

    def test_live_scan_does_not_publish_partial_snapshot_as_final(self):
        previous = self.engine.snapshot()
        events = []
        published = []
        def observe(event):
            published.append(self.engine.snapshot())
            events.append(copy.deepcopy(event))
            event['snapshot']['status'].clear()
        self.engine.run_all(progress=observe)
        self.assertEqual(events[0]['phase'], 'running')
        self.assertEqual(len(events), 2 * len(self.engine.checks))
        self.assertTrue(all(snapshot == previous for snapshot in published))
        self.assertEqual(events[-1]['completed'], len(self.engine.checks))
        self.assertEqual(self.engine.snapshot()['status']['wifi'], 'ok')

    def test_success_reports_verified_only_after_checks(self):
        events = []
        options = self.fix.repair_options()
        self.assertIn('dns_mixed_on_vpn', options)
        self.assertEqual(self.mac.mutations, [])
        self.fix.fix_all(progress=events.append)
        self.assertEqual([e['phase'] for e in events], ['preflight', 'snapshot', 'snapshot', 'applying', 'verifying', 'finished'])
        self.assertTrue(Path(events[2]['recovery_path']).exists())
        self.assertEqual(events[-1]['outcome'], 'verified')
        self.assertEqual(self.mac.manual_dns, ['10.0.0.66', '10.0.0.68'])

    def test_failed_repair_reports_rollback_not_verified(self):
        self.mac.break_network_after_apply = True
        events = []
        self.fix.fix_all(progress=events.append)
        self.assertIn('rollback', [event['phase'] for event in events])
        self.assertEqual(events[-1]['outcome'], 'rolled_back')
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])

    def test_failed_rollback_is_a_separate_outcome(self):
        self.mac.fail_apply = self.mac.fail_rollback = True
        events = []
        self.fix.fix_all(progress=events.append)
        self.assertEqual(events[-1]['outcome'], 'rollback_failed')

    def test_progress_callback_failure_cannot_interrupt_rollback(self):
        self.mac.fail_apply = True
        def broken(event):
            raise RuntimeError('closed view')
        lines = self.fix.fix_all(progress=broken)
        self.assertTrue(any('已验证回滚' in line for line in lines))
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])

    def test_changed_preconditions_report_blocked_without_writes(self):
        self.mac.services = {}
        events = []
        self.fix.fix_all(progress=events.append)
        self.assertEqual(events[-1]['outcome'], 'blocked')
        self.assertEqual(self.mac.mutations, [])
