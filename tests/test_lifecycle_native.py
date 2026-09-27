"""Native lifecycle controls with a fake service manager, never host registration."""
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeLifecycleTests(unittest.TestCase):
    def until(self, condition):
        from Foundation import NSDate, NSRunLoop
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.03))
        self.assertTrue(condition())

    def setUp(self):
        from AppKit import NSApplication
        from relay.remote_desktop import build_connected_app_class
        from relay.remote_controller import RemoteController
        from desktop_fixture import offline_desktop
        NSApplication.sharedApplication()
        self.lifecycle = Mock()
        self.state = {'core': 'stopped', 'background': {'available': True, 'state': 'not_registered'}, 'inhibited': True}
        self.lifecycle.status.side_effect = lambda: dict(self.state)
        self.events = patch.object(RemoteController, 'start')
        self.events.start()
        self.addCleanup(self.events.stop)
        self.app = build_connected_app_class('/tmp/not-created-relay-lifecycle-ui', lifecycle=self.lifecycle,
                                            desktop_factory=offline_desktop)()
        self.addCleanup(self.close)
        self.until(lambda: self.lifecycle.start.called and not self.app.settings.lifecycle_busy)

    def close(self):
        self.app.controller.close()
        if self.app.lifecycle_thread:
            self.app.lifecycle_thread.join(5)
        for view in (self.app.workspace, self.app.review, self.app.receipt, self.app.report, self.app.task_report):
            view.window.setDelegate_(None)
            view.window.orderOut_(None)

    def test_open_respects_inhibit_and_offline_settings_can_start(self):
        self.lifecycle.start.assert_called_once_with()
        self.app.workspace.show('settings')
        self.assertFalse(self.app.settings.root.isHidden())
        self.assertTrue(self.app.workspace.placeholder.isHidden())
        self.assertTrue(self.app.settings.start_core.isEnabled())
        self.assertFalse(self.app.settings.stop_core.isEnabled())
        self.app.settings.actions.startCore_(None)
        self.until(lambda: self.lifecycle.start.call_count == 2 and not self.app.settings.lifecycle_busy)
        self.lifecycle.start.assert_called_with(explicit=True)

    def test_background_cancel_and_enable_use_separate_confirmation(self):
        with patch('rumps.alert', return_value=0):
            self.app.settings.actions.background_(None)
        self.lifecycle.background.assert_not_called()
        self.assertFalse(self.app.settings.background.state())
        self.state['background'] = {'available': True, 'state': 'requires_approval'}
        with patch('rumps.alert', return_value=1):
            self.app.settings.actions.background_(None)
            self.until(lambda: self.lifecycle.background.called and not self.app.settings.lifecycle_busy)
        self.lifecycle.background.assert_called_once_with(True)
        self.assertTrue(self.app.settings.background.state())
        self.app.settings.actions.systemSettings_(None)
        self.until(lambda: self.lifecycle.service.open_settings.called and not self.app.settings.lifecycle_busy)

    def test_stop_is_async_and_quit_cannot_interrupt_unregister_sequence(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            release.wait(5)
        self.lifecycle.stop.side_effect = hold
        self.state['core'] = 'running'
        self.app.lifecycle_done(self.state, None, None)
        with patch('rumps.alert', return_value=1), patch('rumps.quit_application') as quit_app:
            self.app.settings.actions.stopCore_(None)
            self.assertTrue(entered.wait(2))
            try:
                self.assertTrue(self.app.settings.lifecycle_busy)
                self.assertFalse(self.app.settings.background.isEnabled())
                self.app.quit(None)
                quit_app.assert_not_called()
                self.assertIn('等待', self.app.workspace.message)
            finally:
                release.set()
                self.until(lambda: not self.app.settings.lifecycle_busy)

    def test_uninstall_result_is_visible_and_does_not_close_desktop(self):
        self.lifecycle.prepare_uninstall.return_value = {'ready_to_remove_app': True, 'data_preserved': True}
        with patch('rumps.alert', return_value=1), patch('rumps.quit_application') as quit_app:
            self.app.settings.actions.uninstall_(None)
            self.until(lambda: self.lifecycle.prepare_uninstall.called and not self.app.settings.lifecycle_busy)
            self.assertIn('本地资料未删除', self.app.workspace.message)
            quit_app.assert_not_called()
            self.app.quit(None)
            self.until(lambda: quit_app.called)
            quit_app.assert_called_once()
        self.lifecycle.stop.assert_not_called()

    def test_unavailable_registration_and_failure_do_not_claim_stopped(self):
        self.state['background'] = {'available': False, 'state': 'unavailable'}
        self.state['core'] = 'unavailable'
        self.app.lifecycle_done(self.state, '未确认完成', None)
        self.assertFalse(self.app.settings.background.isEnabled())
        self.assertFalse(self.app.settings.system_settings.isEnabled())
        self.assertFalse(self.app.settings.start_core.isEnabled())
        self.assertIn('未确认完成', self.app.workspace.message)

    def test_minimum_window_offline_lifecycle_light_dark_controls_fit(self):
        self.capture_lifecycle_modes('lifecycle')

    def test_helper_approval_is_independent_and_cancellable(self):
        self.state['helper'] = {'available': True, 'state': 'not_registered'}
        self.app.lifecycle_done(self.state, None, None)
        with patch('rumps.alert', return_value=0):
            self.app.settings.actions.privileged_(None)
        self.lifecycle.privileged.assert_not_called()
        self.assertFalse(self.app.settings.privileged.state())
        with patch('rumps.alert', return_value=1):
            self.state['helper']['state'] = 'requires_approval'
            self.app.settings.actions.privileged_(None)
            self.until(lambda: self.lifecycle.privileged.called and not self.app.settings.lifecycle_busy)
        self.lifecycle.privileged.assert_called_once_with(True)
        self.lifecycle.background.assert_not_called()
        self.assertTrue(self.app.settings.privileged.state())
        self.assertFalse(self.app.settings.background.state())
        self.capture_lifecycle_modes('helper-approval')

    def test_helper_unavailable_or_busy_is_not_clickable(self):
        self.state['helper'] = {'available': False, 'state': 'unavailable', 'reason': 'signed_bundle_required'}
        self.app.lifecycle_done(self.state, None, None)
        self.assertFalse(self.app.settings.privileged.isEnabled())
        self.assertFalse(self.app.settings.uninstall.isEnabled())
        self.state['helper'] = {'available': True, 'state': 'enabled'}
        self.app.settings.update_lifecycle(self.state, busy=True)
        self.assertFalse(self.app.settings.privileged.isEnabled())
        self.assertFalse(self.app.settings.activate_helper.isEnabled())

    def test_helper_can_be_explicitly_activated_after_system_approval(self):
        self.state['helper'] = {'available': True, 'state': 'enabled'}
        self.app.lifecycle_done(self.state, None, None)
        self.assertTrue(self.app.settings.activate_helper.isEnabled())
        with patch('rumps.alert', return_value=1):
            self.app.settings.actions.activateHelper_(None)
            self.until(lambda: self.lifecycle.privileged.called and not self.app.settings.lifecycle_busy)
        self.lifecycle.privileged.assert_called_once_with(True)
        self.lifecycle.background.assert_not_called()

    def test_signed_core_waits_for_explicit_service_approval(self):
        from AppKit import NSTextField
        self.state['signed_service_required'] = True
        for registration in ('not_registered', 'requires_approval'):
            self.state['background']['state'] = registration
            self.app.lifecycle_done(self.state, None, None)
            self.app.workspace.show('settings')
            self.assertFalse(self.app.settings.start_core.isEnabled())
            self.assertTrue(self.app.settings.background.isEnabled())
            self.assertTrue(self.app.settings.system_settings.isEnabled())
            labels = [str(child.stringValue()) for child in self.app.settings.document.subviews()
                      if isinstance(child, NSTextField)]
            self.assertIn('签名核心：等待启用并批准登录服务', labels)
        self.capture_lifecycle_modes('signed-service')
        self.lifecycle.background.assert_not_called()
        self.state['background']['state'] = 'enabled'
        self.app.lifecycle_done(self.state, None, None)
        self.assertTrue(self.app.settings.start_core.isEnabled())
        self.app.settings.actions.startCore_(None)
        self.until(lambda: self.lifecycle.start.call_count == 2 and not self.app.settings.lifecycle_busy)
        self.lifecycle.start.assert_called_with(explicit=True)
        self.lifecycle.background.assert_not_called()

    def capture_lifecycle_modes(self, prefix):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG, NSTextField
        from relay.panel import text_height
        self.app.workspace.show('settings')
        self.app.workspace.window.setContentSize_((760, 560))
        for mode in ('light', 'dark'):
            self.app.workspace.window.setAppearance_(NSAppearance.appearanceNamed_(
                'NSAppearanceNameAqua' if mode == 'light' else 'NSAppearanceNameDarkAqua'))
            self.app.workspace.layout()
            if prefix == 'helper-approval':
                self.app.settings.document.scrollPoint_((0, 200))
            for child in self.app.settings.document.subviews():
                frame = child.frame()
                self.assertLessEqual(frame.origin.x + frame.size.width, self.app.settings.document.frame().size.width)
                if isinstance(child, NSTextField):
                    self.assertGreaterEqual(frame.size.height + 3, text_height(child, frame.size.width))
            self.app.workspace.window.displayIfNeeded()
            root = self.app.workspace.root
            bitmap = root.bitmapImageRepForCachingDisplayInRect_(root.bounds())
            root.cacheDisplayInRect_toBitmapImageRep_(root.bounds(), bitmap)
            colors = {round(bitmap.colorAtX_y_(x, y).redComponent(), 2)
                      for x in range(164, 480, 2) for y in range(60, 160, 2)}
            self.assertGreater(len(colors), 6, 'Offline lifecycle controls are not visible')
            path = ROOT / 'build/ui-preview' / f'{prefix}-{mode}.png'
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))


if __name__ == '__main__':
    unittest.main()
