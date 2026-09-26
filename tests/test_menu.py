"""Headless controller regression tests: fake diagnostic/account/OS boundaries."""
import contextlib
import copy
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('relay_menu', ROOT / 'code/network-doctor-menu.py')
menu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(menu)


class FakeConfig:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = copy.deepcopy(menu.DEFAULT_CONFIG)
        self.load_error = None
        self.saved = 0
    def load(self):
        assert threading.current_thread() is not threading.main_thread()
    def save(self):
        assert threading.current_thread() is not threading.main_thread()
        self.saved += 1
    def get(self, key, default=None):
        result = self.config
        for part in key.split('.'):
            if not isinstance(result, dict) or part not in result:
                return default
            result = result[part]
        return result
    def apply_preset(self, name):
        self.config['general']['preset'] = name
        return True
    def auto_detect(self):
        return {}


class FakeEngine:
    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.overall = 'unknown'
        self.check_entered = threading.Event()
        self.check_release = None
        self.result = {'status': {'wifi': 'ok', 'wifi_network': 'private-ssid', 'wifi_ip': '192.168.8.9'},
                       'issues': [('low', 'vpn_unknown', '归属未确认')], 'check_errors': {}, 'last_check': datetime.now()}
    def run_all(self):
        assert threading.current_thread() is not threading.main_thread()
        self.calls += 1
        self.check_entered.set()
        if self.check_release:
            self.check_release.wait(3)
        return self.snapshot()
    def snapshot(self):
        return copy.deepcopy(self.result)
    def get_overall_status(self):
        return self.overall
    def get_status_summary(self):
        return ['🟡 状态未确认']


class FakeFix:
    def __init__(self, config, engine):
        self.engine = engine
        self.calls = 0
        self.entered = threading.Event()
        self.release = None
    def describe_fixes(self, issues):
        assert threading.current_thread() is not threading.main_thread()
        return ['仅用于测试的安全修复计划']
    def fix_all(self, issues):
        assert threading.current_thread() is not threading.main_thread()
        self.calls += 1
        self.entered.set()
        if self.release:
            self.release.wait(3)
        return ['❌ 修复失败：测试故障', '↩ 已验证回滚']


class FakeAccount:
    def __init__(self, config):
        self.config = config
        self.email = None
        self.pro = False
        self.restore_error = None
        self.restore_release = None
        self.restore_entered = threading.Event()
        self.poll_release = None
        self.poll_entered = threading.Event()
        self.logged_out = threading.Event()
        self.polls = self.logouts = self.refreshes = 0
        self.calls = []
    def _background(self, method):
        assert threading.current_thread() is not threading.main_thread(), method
        self.calls.append(method)
    def restore(self):
        self._background('restore')
        self.restore_entered.set()
        if self.restore_release:
            self.restore_release.wait(3)
        if self.restore_error:
            raise self.restore_error
        return self.summary()
    def summary(self):
        self._background('summary')
        return {'email': self.email, 'tier': 'Pro' if self.pro else 'Free', 'message': 'test account'}
    def refresh_entitlement(self):
        self._background('refresh')
        self.refreshes += 1
        return self.summary()
    def allows(self, feature):
        self._background('allows')
        return self.pro
    def begin_login(self):
        self._background('begin_login')
        return {'authorizeUrl': self.config.origin + '/desktop/authorize/?request=test', 'expiresIn': 300}
    def poll_login(self, attempt):
        self._background('poll_login')
        self.polls += 1
        self.poll_entered.set()
        if self.poll_release:
            self.poll_release.wait(3)
        self.email = 'account@example.test'
        return True
    def logout(self):
        self._background('logout')
        self.logouts += 1
        self.email = None
        self.pro = False
        self.logged_out.set()
        return '已退出'


class MenuControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = queue.Queue()
        self.commercial = types.SimpleNamespace(configured=True, origin='https://relay.example.test')
        self.account = FakeAccount(self.commercial)
        self.controllers = []
        self.addCleanup(self._close)
    def _close(self):
        for controller in self.controllers:
            controller.close()
        for controller in self.controllers:
            for worker in (controller.diagnostics, controller.accounts, controller.histories, controller.files):
                worker.thread.join(4)
    def make(self, **kwargs):
        options = dict(on_event=lambda name, payload: self.events.put((name, payload)),
                       dispatch=lambda fn, *args: fn(*args), data_dir=self.tmp.name,
                       config_factory=FakeConfig, engine_factory=FakeEngine, fix_factory=FakeFix,
                       account_factory=lambda config: self.account,
                       commercial_loader=lambda: self.commercial,
                       open_browser=lambda url: True, login_poll_interval=0.01, start=False)
        options.update(kwargs)
        controller = menu.RelayController(**options)
        self.controllers.append(controller)
        return controller
    def drain(self, worker):
        done = threading.Event()
        self.assertTrue(worker.submit(done.set))
        self.assertTrue(done.wait(3), 'worker did not finish')
    def started(self, **kwargs):
        controller = self.make(**kwargs)
        controller.start()
        self.drain(controller.diagnostics)
        self.drain(controller.accounts)
        self.assertTrue(controller.ui_state()['ready'])
        return controller
    def event(self, kind, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            name, payload = self.events.get(timeout=max(0.01, end-time.monotonic()))
            if name == kind:
                return payload
        self.fail('No event: ' + kind)

    def test_account_config_failure_does_not_block_free_diagnosis(self):
        def broken():
            raise ValueError('invalid commercial json')
        controller = self.started(commercial_loader=broken)
        self.assertFalse(controller.ui_state()['account']['configured'])
        self.assertTrue(controller.check(notify=True))
        self.drain(controller.diagnostics)
        self.assertGreaterEqual(controller.engine.calls, 2)
        self.assertEqual(controller.ui_state()['overall'], 'unknown')
        self.assertEqual(self.event('alert')['title'], '检测未完全确认')

    def test_keychain_failure_does_not_block_free_diagnosis(self):
        self.account.restore_error = RuntimeError('Keychain unavailable')
        controller = self.started()
        self.assertTrue(controller.check())
        self.drain(controller.diagnostics)
        self.assertTrue(controller.ui_state()['ready'])
        self.assertEqual(controller.ui_state()['account']['tier'], 'Free')

    def test_ui_state_never_waits_for_account_http_or_calls_summary(self):
        self.account.restore_release = threading.Event()
        controller = self.make()
        controller.start()
        self.assertTrue(self.account.restore_entered.wait(1))
        self.drain(controller.diagnostics)
        # The account worker is blocked but the main thread only reads cache.
        state = controller.ui_state()
        self.assertTrue(state['ready'])
        self.assertTrue(controller.check())
        self.drain(controller.diagnostics)
        self.assertGreaterEqual(controller.engine.calls, 2)
        self.account.restore_release.set()
        self.drain(controller.accounts)

    def test_diagnostic_gate_blocks_crossing_check_fix_and_preset(self):
        controller = self.started()
        controller.engine.check_release = threading.Event()
        controller.engine.check_entered.clear()
        self.assertTrue(controller.check())
        self.assertTrue(controller.engine.check_entered.wait(1))
        self.assertFalse(controller.check())
        self.assertFalse(controller.prepare_fix())
        self.assertFalse(controller.set_preset('minimal'))
        controller.engine.check_release.set()
        self.drain(controller.diagnostics)
        self.assertEqual(controller.config.get('general.preset'), 'observe')

    def test_fix_confirmation_reserves_gate_and_results_are_not_relabelled_success(self):
        controller = self.started()
        self.assertTrue(controller.prepare_fix())
        plan = self.event('repair_confirmation')
        self.assertFalse(controller.check())
        self.assertFalse(controller.set_preset('minimal'))
        controller.fix_engine.release = threading.Event()
        self.assertTrue(controller.confirm_fix(plan['token'], True))
        self.assertTrue(controller.fix_engine.entered.wait(1))
        self.assertFalse(controller.check())
        controller.fix_engine.release.set()
        self.drain(controller.diagnostics)
        result = self.event('alert')
        self.assertEqual(result['title'], '修复结果')
        self.assertEqual(result['message'], '❌ 修复失败：测试故障\n↩ 已验证回滚')
        self.assertEqual(controller.ui_state()['busy'], '')

    def test_cancel_fix_releases_gate_without_mutation(self):
        controller = self.started()
        controller.prepare_fix(); plan = self.event('repair_confirmation')
        self.assertTrue(controller.confirm_fix(plan['token'], False))
        self.assertEqual(controller.fix_engine.calls, 0)
        self.assertTrue(controller.check())
        self.drain(controller.diagnostics)

    def test_cancel_login_revokes_a_late_success(self):
        controller = self.started()
        self.account.poll_release = threading.Event()
        self.assertTrue(controller.login())
        self.assertTrue(self.account.poll_entered.wait(1))
        self.assertTrue(controller.cancel_login())
        self.account.poll_release.set()
        self.drain(controller.accounts)
        self.assertEqual(self.account.email, None)
        self.assertGreaterEqual(self.account.logouts, 1)
        self.assertFalse(controller.ui_state()['login_active'])
        self.assertIsNone(controller.ui_state()['account']['email'])

    def test_logout_and_inflight_login_are_serialized(self):
        controller = self.started()
        self.account.poll_release = threading.Event()
        controller.login(); self.assertTrue(self.account.poll_entered.wait(1))
        self.assertTrue(controller.logout())
        self.assertFalse(controller.login())
        self.account.poll_release.set()
        self.drain(controller.accounts)
        self.assertIsNone(self.account.email)
        self.assertIsNone(controller.ui_state()['account']['email'])
        self.assertFalse(controller.ui_state()['login_active'])

    def test_quit_cancels_further_login_polls_and_clears_late_session(self):
        controller = self.started()
        self.account.poll_release = threading.Event()
        controller.login(); self.assertTrue(self.account.poll_entered.wait(1))
        controller.close()
        self.account.poll_release.set()
        self.assertTrue(self.account.logged_out.wait(1))
        self.assertEqual(self.account.polls, 1)
        self.assertIsNone(self.account.email)

    def test_quit_during_keychain_restore_does_not_start_http_refresh(self):
        self.account.restore_release = threading.Event()
        controller = self.make()
        controller.start()
        self.assertTrue(self.account.restore_entered.wait(1))
        controller.close()
        self.account.restore_release.set()
        controller.accounts.thread.join(1)
        self.assertFalse(controller.accounts.thread.is_alive())
        self.assertEqual(self.account.refreshes, 0)

    def test_free_diagnosis_and_export_survive_history_store_failure(self):
        self.account.pro = True
        failed = threading.Event()
        def broken_history(path):
            failed.set()
            raise OSError('history disk unavailable')
        controller = self.started(history_factory=broken_history)
        controller.check(); self.drain(controller.diagnostics)
        self.assertTrue(failed.wait(1))
        self.drain(controller.histories)
        self.assertTrue(controller.ui_state()['history_error'])
        self.assertTrue(controller.export_basic(self.tmp.name))
        self.drain(controller.files)
        paths = self.event('export_complete')['paths']
        exported = Path(paths[0]).read_text()
        self.assertNotIn('private-ssid', exported)
        self.assertNotIn('192.168.8.9', exported)
        self.assertEqual(stat.S_IMODE(Path(paths[0]).stat().st_mode), 0o600)
        self.assertTrue(controller.check())
        self.drain(controller.diagnostics)

    def test_history_requires_entitlement_and_is_independent_of_diagnostic_worker(self):
        record_started, record_release = threading.Event(), threading.Event()
        records = []
        class History:
            def __init__(self, path):
                pass
            def record(self, snapshot):
                record_started.set()
                record_release.wait(3)
                records.append(snapshot)
        controller = self.started(history_factory=History)
        self.drain(controller.histories)
        self.assertFalse(record_started.is_set())
        self.account.pro = True
        controller.check(); self.drain(controller.diagnostics)
        self.assertTrue(record_started.wait(1))
        # Optional recording is blocked, while another Free check completes.
        self.assertTrue(controller.check())
        self.drain(controller.diagnostics)
        record_release.set(); self.drain(controller.histories)
        self.assertTrue(records)
        self.account.pro = False
        before = len(records)
        controller.check(); self.drain(controller.diagnostics); self.drain(controller.histories)
        self.assertEqual(len(records), before)

    def test_pro_actions_recheck_permissions_in_background(self):
        controller = self.started()
        self.assertTrue(controller.show_history())
        self.drain(controller.histories)
        self.assertEqual(self.event('alert')['title'], '此功能需要 Relay Pro')
        self.assertTrue(controller.ui_state()['ready'])


class MenuPackagingTests(unittest.TestCase):
    def test_self_check_has_no_gui_or_system_command_requirement(self):
        with patch('subprocess.run', side_effect=AssertionError('system command forbidden')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(menu.self_check(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['version'], '3.0.0')
        self.assertEqual(len(result['plugins']), 7)
        self.assertTrue(result['ok'])

    def test_packaged_app_ignores_environment_commercial_config_override(self):
        with patch.dict(os.environ, {'RELAY_COMMERCIAL_CONFIG': '/tmp/untrusted-config.json'}):
            with patch.object(sys, 'frozen', True, create=True):
                self.assertEqual(menu.commercial_config_path().name, 'relay-commercial.json')
            with patch.object(sys, 'frozen', False, create=True):
                self.assertEqual(menu.commercial_config_path(), Path('/tmp/untrusted-config.json'))

    def test_free_redaction_fails_closed_when_optional_redactor_unavailable(self):
        with patch.object(menu, 'redact', None):
            report = menu.basic_report({'status': {'wifi': 'ok', 'wifi_network': 'private', 'vpn_ip': '10.0.0.1'}, 'issues': []})
        self.assertNotIn('private', report)
        self.assertNotIn('10.0.0.1', report)
        self.assertIn('ok', report)


if __name__ == '__main__':
    unittest.main()
