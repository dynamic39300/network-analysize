"""Real Cocoa/IPC migration workflows; fake OS, account and browser only."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeDesktopCompatibilityTests(unittest.TestCase):
    def setUp(self):
        from AppKit import NSApplication
        from relay.commercial.client import CommercialConfig
        from relay.core import CoreRuntime
        from relay.core_service import CoreService
        from relay.desktop_services import DesktopServices
        from relay.remote_desktop import build_connected_app_class
        from test_diagnostics import FakeMac, config
        from test_menu import FakeAccount
        from test_windows import FakeEvents
        NSApplication.sharedApplication()
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.mac = FakeMac()
        cfg = config(self.mac, company=True)
        cfg.config_path = str(self.data / 'config.json')
        cfg.save()
        self.runtime = CoreRuntime(self.data, config=cfg, platform='darwin', runner=self.mac,
            events_factory=FakeEvents, guard_threaded=False, execution_directory=self.data / 'execution')
        self.service = CoreService(self.runtime)
        self.service.start()
        self.addCleanup(self.service.close)
        commercial = CommercialConfig('https://relay.example.test', 'test-key')
        self.account = FakeAccount(commercial)
        self.browser = Mock(return_value=True)
        def factory(*args, **kwargs):
            return DesktopServices(*args, **kwargs, account_factory=lambda _: self.account,
                commercial_loader=lambda: commercial, open_browser=self.browser, login_poll_interval=0.01)
        self.app = build_connected_app_class(self.data, desktop_factory=factory)()
        self.addCleanup(self.close)
        self.until(lambda: self.app.controller.ui_state()['ready'] and self.app.desktop.ui_state()['account']['configured'])

    def until(self, condition):
        from Foundation import NSDate, NSRunLoop
        deadline = time.monotonic() + 6
        while not condition() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
        self.assertTrue(condition())

    def close(self):
        self.app.controller.close()
        self.app.controller.thread.join(6)
        for worker in (self.app.desktop.accounts, self.app.desktop.histories, self.app.desktop.files):
            worker.thread.join(6)
        for view in (self.app.workspace, self.app.review, self.app.receipt, self.app.report,
                     self.app.task_report, self.app.history_report):
            if view:
                view.window.setDelegate_(None)
                view.window.orderOut_(None)

    def settings(self):
        self.app.workspace.show('settings')
        self.until(lambda: self.app.settings.payload is not None)
        return self.app.settings

    def test_browser_login_cancel_and_logout_do_not_change_core_authority(self):
        settings = self.settings()
        model = self.runtime.configure_model('test', 'http://127.0.0.1:9999/v1/responses', '',
                                             'forget', self.runtime.model_revision)
        self.runtime.grant_model_consent(model['revision'], model['binding'])
        self.runtime.start_guard()
        before = self.runtime.trust.revision
        self.account.poll_release = threading.Event()
        settings.login.performClick_(None)
        self.until(self.account.poll_entered.is_set)
        self.assertTrue(settings.cancel_login.isEnabled())
        settings.cancel_login.performClick_(None)
        self.account.poll_release.set()
        self.until(lambda: not self.app.desktop.ui_state()['login_active'])
        self.assertIsNone(self.account.email)
        settings.login.performClick_(None)
        self.until(lambda: settings.logout.isEnabled() and bool(self.account.email))
        self.until(lambda: not self.app.desktop.ui_state()['login_active'])
        settings.logout.performClick_(None)
        self.until(lambda: not self.app.desktop.ui_state()['logout_pending'] and self.account.email is None)
        self.assertEqual(self.runtime.trust.revision, before)
        self.assertTrue(self.runtime.model_consent.allows(self.runtime.model))
        self.assertTrue(self.runtime.guard.schedule.enabled)
        self.assertFalse(self.mac.calls)
        self.assertEqual(self.browser.call_count, 2)

    def test_slow_account_sync_does_not_block_core_checks_basic_export_or_free_task_records(self):
        entered, release = threading.Event(), threading.Event()
        def held():
            entered.set()
            release.wait(5)
        with patch.object(self.account, 'refresh_entitlement', side_effect=held):
            self.app.desktop.refresh_account()
            self.until(entered.is_set)
            try:
                self.app.controller.check()
                self.until(lambda: self.app.controller.ui_state()['snapshot'].get('last_check') is not None)
                self.app.workspace.show('records')
                self.until(lambda: bool(self.app.records.rows))
                self.app.show_report(None)
                self.assertNotIn('Test-only', self.app.report.current_text)
                with patch.object(self.app, 'choose_directory', return_value=str(self.data)):
                    self.app.export_basic()
                self.until(lambda: bool(list(self.data.glob('relay-basic-*.json'))))
                report = next(self.data.glob('relay-basic-*.json'))
                self.assertTrue(json.loads(report.read_text())['redacted'])
                self.assertFalse((self.data / 'history.sqlite3').exists())
            finally:
                release.set()
        self.assertFalse(self.mac.mutations)

    def test_existing_pro_history_comparison_and_export_are_separate_from_agent_records(self):
        from relay.commercial.history import HistoryStore
        history = HistoryStore(self.data / 'history.sqlite3')
        history.record({'status': {'wifi': 'error'}, 'issues': []})
        self.account.pro = True
        self.app.desktop.refresh_account()
        self.until(lambda: self.app.desktop.ui_state()['account']['tier'] == 'Pro')
        self.app.controller.check()
        self.until(lambda: len(history.list()) == 2)
        self.app.workspace.show('records')
        self.until(lambda: bool(self.app.records.rows))
        task_ids = [row['id'] for row in self.app.records.rows]
        self.app.records.history.performClick_(None)
        self.until(lambda: self.app.history_report is not None)
        self.assertEqual(len(self.app.history_report.snapshot['records']), 2)
        from AppKit import NSBitmapImageFileTypePNG
        root = self.app.history_report.root
        bitmap = root.bitmapImageRepForCachingDisplayInRect_(root.bounds())
        root.cacheDisplayInRect_toBitmapImageRep_(root.bounds(), bitmap)
        path = ROOT / 'build/ui-preview/history-report.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))
        self.app.records.compare.performClick_(None)
        self.until(lambda: self.app.history_report.snapshot.get('available'))
        with patch.object(self.app, 'choose_directory', return_value=str(self.data)):
            self.app.records.bundle.performClick_(None)
            self.until(lambda: bool(list(self.data.glob('netcare-report-*.html'))))
        self.assertEqual([row['id'] for row in self.app.records.rows], task_ids)
        self.account.pro = False
        self.app.records.history.performClick_(None)
        self.until(lambda: '此功能需要 NetCare Pro' in self.app.workspace.message)
        self.assertEqual(len(history.list()), 2)
        self.assertEqual([row['id'] for row in self.app.records.rows], task_ids)

    def test_detection_controls_round_trip_revision_and_keep_health_targets(self):
        settings = self.settings()
        targets = copy.deepcopy(self.runtime.profiles.profiles)
        settings.detection_preset.selectItemAtIndex_(3)
        settings.save_detection.performClick_(None)
        self.until(lambda: settings.payload['detection']['preset'] == 'minimal')
        self.assertEqual(self.runtime.config.get('general.preset'), 'minimal')
        self.assertEqual(self.runtime.profiles.profiles, targets)
        self.assertFalse(self.mac.calls)
        settings.redetect.performClick_(None)
        self.until(lambda: self.app.controller.ui_state()['snapshot'].get('last_check') is not None)
        self.assertEqual(self.runtime.profiles.profiles, targets)
        self.assertFalse(self.mac.mutations)
        settings.update_state({**settings.state, 'detection': {**settings.state['detection'], 'revision': '0' * 64}})
        self.assertFalse(settings.save_detection.isEnabled())
        self.assertFalse(settings.redetect.isEnabled())

    def test_account_offline_and_minimum_long_content_rendering(self):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG, NSTextField
        from relay.panel import text_height
        settings = self.settings()
        self.account.email = 'very-long-account-name-for-layout-verification@example.test'
        self.account.pro = True
        self.app.desktop.refresh_account()
        self.until(lambda: settings.account_state.get('account', {}).get('email') == self.account.email)
        self.app.workspace.window.setContentSize_((760, 560))
        for appearance, suffix in [('NSAppearanceNameAqua', 'light'), ('NSAppearanceNameDarkAqua', 'dark')]:
            self.app.workspace.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            self.app.workspace.layout()
            for control in settings.document.subviews():
                frame = control.frame()
                self.assertLessEqual(frame.origin.x + frame.size.width, settings.document.frame().size.width + 1)
                if isinstance(control, NSTextField) and not control.isEditable():
                    self.assertGreaterEqual(frame.size.height + 2, text_height(control, frame.size.width))
            root = self.app.workspace.root
            bitmap = root.bitmapImageRepForCachingDisplayInRect_(root.bounds())
            root.cacheDisplayInRect_toBitmapImageRep_(root.bounds(), bitmap)
            path = ROOT / 'build/ui-preview' / ('account-workspace-' + suffix + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))
        self.service.close()
        self.until(lambda: not settings.state.get('ready'))
        self.assertIsNotNone(settings.logout.superview())
        self.assertTrue(settings.logout.isEnabled())
        self.assertFalse(settings.save_detection.isEnabled())
        self.assertFalse(settings.save_model_button.isEnabled())
        self.assertIsNone(settings.save_model_button.superview())


if __name__ == '__main__':
    unittest.main()
