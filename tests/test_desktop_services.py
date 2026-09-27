"""Default desktop compatibility with fake accounts and real private local history."""
import copy
from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
from relay.commercial.client import AccountError, CommercialConfig
from relay.commercial.history import HistoryStore
from relay.desktop_services import DesktopServices, commercial_config_path, history_text, redacted_snapshot
import test_commercial
from test_menu import FakeAccount, menu


class DesktopServicesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.config = CommercialConfig('https://relay.example.test', 'test-key')
        self.account = FakeAccount(self.config)
        self.events = queue.Queue()
        self.snapshot = {'last_check': datetime.now(), 'status': {'wifi': 'ok', 'wifi_ip': '10.0.0.2',
            'wifi_network': 'private-network'}, 'issues': [], 'check_errors': {}}
        self.services = DesktopServices(lambda *args: self.events.put(args), lambda fn, *args: fn(*args), self.data,
            lambda: copy.deepcopy(self.snapshot), account_factory=lambda _: self.account, commercial_loader=lambda: self.config,
            open_browser=Mock(return_value=True), login_poll_interval=0.001)
        self.addCleanup(self.close)
        self.services.start(periodic=False)
        self.drain(self.services.accounts)

    def close(self):
        self.services.close()
        for worker in (self.services.accounts, self.services.histories, self.services.files):
            worker.thread.join(5)

    def drain(self, worker):
        deadline = time.monotonic() + 5
        while worker.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(worker.queue.unfinished_tasks)

    def event(self, name):
        found = []
        while not self.events.empty():
            kind, payload = self.events.get_nowait()
            if kind == name:
                found.append(payload)
        self.assertTrue(found, name)
        return found[-1]

    def test_free_basic_export_without_core_or_history_preserves_redaction(self):
        self.assertTrue(self.services.export_basic(self.data))
        self.drain(self.services.files)
        path = Path(self.event('export_complete')['paths'][0])
        value = json.loads(path.read_text())
        self.assertEqual(value['schema'], 'basic-diagnostic-v1')
        self.assertNotIn('private-network', path.read_text())
        self.assertNotIn('10.0.0.2', path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.data / 'history.sqlite3').exists())

    def test_existing_history_is_reused_and_new_committed_snapshots_deduplicate(self):
        history = HistoryStore(self.data / 'history.sqlite3')
        history.record({'status': {'wifi': 'error'}, 'issues': []})
        self.account.pro = True
        self.services.refresh_account()
        self.drain(self.services.accounts)
        state = {'ready': True, 'core_instance': 'core', 'snapshot': self.snapshot}
        self.services.observe_state(state)
        self.services.observe_state(state)
        self.drain(self.services.histories)
        self.assertEqual(len(history.list()), 2)
        self.services.show_history()
        self.drain(self.services.histories)
        result = self.event('history')
        self.assertEqual(len(result['records']), 2)
        self.assertNotIn('private-network', json.dumps(result))
        self.services.compare_latest()
        self.drain(self.services.histories)
        self.assertTrue(self.event('history')['available'])

    def test_entitlement_checked_again_after_choosing_export_directory(self):
        self.account.pro = True
        self.services.record_snapshot(self.snapshot)
        self.drain(self.services.histories)
        self.services.prepare_pro_export()
        self.drain(self.services.histories)
        self.assertEqual(self.event('choose_export_directory')['kind'], 'pro')
        self.account.pro = False
        self.services.export_pro(self.data)
        self.drain(self.services.histories)
        self.assertEqual(self.event('alert')['title'], '此功能需要 NetCare Pro')
        self.assertFalse(list(self.data.glob('netcare-report-*')))
        self.assertEqual(len(HistoryStore(self.data / 'history.sqlite3').list()), 1)

    def test_offline_signed_pro_and_expiry_use_existing_verifier(self):
        fixture = test_commercial.LicenseTests()
        fixture.setUp()
        client = fixture.client()
        self.services.account = client
        with patch.object(client, '_request', side_effect=AccountError('offline')):
            self.services.refresh_account()
            self.drain(self.services.accounts)
        self.assertEqual(self.services.ui_state()['account']['tier'], 'Pro')
        self.services.record_snapshot(self.snapshot)
        self.drain(self.services.histories)
        self.assertEqual(len(HistoryStore(self.data / 'history.sqlite3').list()), 1)
        fixture.now += 604800
        self.services.export_pro(self.data)
        self.drain(self.services.histories)
        self.assertEqual(self.event('alert')['title'], '此功能需要 NetCare Pro')
        self.assertEqual(self.services.ui_state()['account']['tier'], 'Free')
        self.assertFalse(list(self.data.glob('netcare-report-*')))

    def test_late_login_cancel_clears_session_and_cannot_grant_core_permissions(self):
        self.account.poll_release = threading.Event()
        self.services.login()
        self.assertTrue(self.account.poll_entered.wait(2))
        self.services.cancel_login()
        self.account.poll_release.set()
        self.drain(self.services.accounts)
        self.assertIsNone(self.account.email)
        state = self.services.ui_state()
        self.assertFalse(state['login_active'])
        self.assertFalse(set(state) & {'trust', 'model', 'authorization', 'guard'})

    def test_stale_disconnected_or_partial_snapshot_is_not_appended(self):
        self.account.pro = True
        self.services.refresh_account()
        self.drain(self.services.accounts)
        for ready, snapshot in ((False, self.snapshot), (True, {**self.snapshot, 'last_check': None})):
            self.services.observe_state({'ready': ready, 'snapshot': snapshot, 'core_instance': 'core'})
        self.drain(self.services.histories)
        self.assertFalse((self.data / 'history.sqlite3').exists())

    def test_slow_entitlement_does_not_block_ui_or_free_export(self):
        entered, release = threading.Event(), threading.Event()
        def slow(_):
            entered.set()
            release.wait(5)
            return True
        with patch.object(self.account, 'allows', side_effect=slow):
            self.services.record_snapshot(self.snapshot)
            self.assertTrue(entered.wait(2))
            try:
                start = time.monotonic()
                self.assertEqual(self.services.ui_state()['account']['tier'], 'Free')
                self.services.export_basic(self.data)
                self.drain(self.services.files)
                self.event('export_complete')
                self.assertLess(time.monotonic() - start, 0.5)
            finally:
                release.set()
            self.drain(self.services.histories)

    def test_history_store_failure_does_not_block_basic_report(self):
        self.account.pro = True
        self.services.history_factory = Mock(side_effect=OSError('history unavailable'))
        self.services.record_snapshot(self.snapshot)
        self.drain(self.services.histories)
        self.assertIn('unavailable', self.services.ui_state()['history_error'])
        self.services.export_basic(self.data)
        self.drain(self.services.files)
        self.assertEqual(len(self.event('export_complete')['paths']), 1)

    def test_current_snapshot_is_recorded_after_optional_account_restore_finishes(self):
        state = {'ready': True, 'snapshot': self.snapshot, 'core_instance': 'core'}
        self.services.observe_state(state)
        self.drain(self.services.histories)
        self.assertFalse((self.data / 'history.sqlite3').exists())
        self.account.pro = True
        self.services.refresh_account()
        self.drain(self.services.accounts)
        self.services.observe_state(state)
        self.drain(self.services.histories)
        self.assertEqual(len(HistoryStore(self.data / 'history.sqlite3').list()), 1)

    def test_redactor_failure_and_frozen_origin_override_fail_closed(self):
        with patch('relay.desktop_services.redact', None):
            self.assertNotIn('private-network', json.dumps(redacted_snapshot(self.snapshot)))
        with patch.dict('os.environ', {'RELAY_COMMERCIAL_CONFIG': '/tmp/untrusted.json'}), \
             patch.object(sys, 'frozen', True, create=True):
            self.assertEqual(commercial_config_path().name, 'relay-commercial.json')

    def test_history_summary_does_not_infer_health_from_missing_or_failed_checks(self):
        summary = history_text({'records': [{'id': 1, 'createdAt': 0, 'status': {'wifi': 'error'},
            'issues': [], 'check_errors': {'dns': 'unavailable'}}]})
        self.assertIn('网络连接：异常', summary)
        self.assertIn('未完成的检查：网站地址查找', summary)
        self.assertNotIn('正常', summary)
        comparison = history_text({'available': True, 'before': 0, 'after': 1,
            'changes': [{'field': 'wifi', 'before': 'error', 'after': 'ok'}]})
        self.assertIn('之前：异常\n之后：正常', comparison)
        self.assertEqual(history_text({'records': []}), '暂无本地诊断历史')


class DefaultEntryTests(unittest.TestCase):
    def test_default_entry_and_explicit_service_routes_do_not_initialize_legacy_runtime(self):
        for arguments, expected in (([], ['--agent-desktop']),
                (['--data-dir', '/tmp/example'], ['--agent-desktop', '--data-dir', '/tmp/example']),
                (['--core-service', '--managed'], ['--core-service', '--managed']),
                (['--lifecycle=status'], ['--lifecycle=status'])):
            with self.subTest(arguments=arguments), patch.object(sys, 'argv', ['NetCare', *arguments]), \
                 patch('relay_app.main', return_value=0) as new, patch.object(menu, 'build_app_class') as legacy:
                self.assertEqual(menu.main(), 0)
                new.assert_called_once_with(expected)
                legacy.assert_not_called()

    def test_legacy_entry_is_explicit_and_rejects_mixed_role_arguments(self):
        with patch.object(sys, 'argv', ['NetCare', '--legacy-desktop']), patch.object(menu, 'build_app_class') as legacy:
            self.assertEqual(menu.main(), 0)
            legacy.return_value.return_value.run.assert_called_once()
        with patch.object(sys, 'argv', ['NetCare', '--legacy-desktop', '--core-service']), patch.object(menu, 'build_app_class') as legacy:
            self.assertEqual(menu.main(), 2)
            legacy.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', 'native desktop entry')
    def test_duplicate_desktop_is_rejected_without_starting_another_ui_or_core(self):
        from relay.remote_desktop import run_desktop
        with tempfile.TemporaryDirectory() as directory:
            app = Mock()
            def nested():
                with self.assertRaises(OSError):
                    run_desktop(directory)
            app.run.side_effect = nested
            with patch('relay.remote_desktop.build_connected_app_class', return_value=Mock(return_value=app)) as factory:
                self.assertEqual(run_desktop(directory), 0)
                self.assertEqual(factory.call_count, 1)


if __name__ == '__main__':
    unittest.main()
