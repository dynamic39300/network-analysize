"""Native report regression: long content must not push close controls off-screen."""
import os
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_menu import menu
from test_panel import sample_state


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class ReportWindowTests(unittest.TestCase):
    def make_app(self):
        from AppKit import NSApplication
        NSApplication.sharedApplication()
        state = sample_state()
        state['snapshot']['status']['checks'] = [{'result': 'ok', 'name': '长报告检查'}] * 160
        app = object.__new__(menu.build_app_class())
        app.controller = types.SimpleNamespace(ui_state=lambda: state)
        app.report_window = None
        self.addCleanup(lambda: app.report_window.window.orderOut_(None) if app.report_window else None)
        return app, state

    def test_long_report_stays_on_screen_and_can_close(self):
        import rumps
        from AppKit import NSAlert, NSApplication, NSScreen
        NSApplication.sharedApplication()
        state = sample_state()
        state['snapshot']['status']['checks'] = [{'result': 'ok', 'name': '长报告检查'}] * 160
        app = object.__new__(menu.build_app_class())
        app.controller = types.SimpleNamespace(ui_state=lambda: state)
        app.report_window = None
        legacy_frames = []

        class AlertProxy:
            def __init__(self, alert):
                self.alert = alert
            def __getattr__(self, key):
                return getattr(self.alert, key)
            def runModal(self):
                self.alert.layout()
                legacy_frames.append(self.alert.window().frame())
                return 1

        class AlertFactory:
            @staticmethod
            def alertWithMessageText_defaultButton_alternateButton_otherButton_informativeTextWithFormat_(*args):
                return AlertProxy(NSAlert.alertWithMessageText_defaultButton_alternateButton_otherButton_informativeTextWithFormat_(*args))

        with patch.dict(rumps.alert.__globals__, NSAlert=AlertFactory):
            app.on_show_report(None)
        screen = NSScreen.mainScreen().visibleFrame()
        if legacy_frames:
            self.assertLessEqual(legacy_frames[0].size.height, screen.size.height,
                                 '长报告弹窗超出屏幕，关闭按钮无法保持可见')
        else:
            report = app.report_window
            self.addCleanup(report.window.orderOut_, None)
            self.assertLessEqual(report.window.frame().size.height, screen.size.height)
            self.assertIsNone(NSApplication.sharedApplication().modalWindow())
            self.assertTrue(report.window.isVisible())
            report.close_button.performClick_(None)
            self.assertFalse(report.window.isVisible())

    def capture(self, report, name):
        from AppKit import NSBitmapImageFileTypePNG
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        report.window.displayIfNeeded()
        bitmap = report.root.bitmapImageRepForCachingDisplayInRect_(report.root.bounds())
        report.root.cacheDisplayInRect_toBitmapImageRep_(report.root.bounds(), bitmap)
        output = Path(__file__).resolve().parents[1] / 'build/ui-preview' / (name + '.png')
        output.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(output), True))

    def test_full_redacted_data_scrolls_without_moving_close_controls(self):
        from AppKit import NSAppearance
        app, _ = self.make_app()
        app.on_show_report(None)
        report = app.report_window
        for appearance, name in [('NSAppearanceNameAqua', 'report-light'), ('NSAppearanceNameDarkAqua', 'report-dark')]:
            report.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            self.capture(report, name)
        report.tabs.setSelectedSegment_(1)
        report.update_body()
        self.assertGreater(len(report.current_text), 6500)
        self.assertEqual(len(json.loads(report.current_text)['status']['checks']), 160)
        self.assertNotIn('192.168.1.8', report.current_text)
        report.window.setContentSize_((640, 440))
        report.layout()
        report.update_body()
        self.assertGreater(report.text_view.frame().size.height, report.scroll.contentSize().height)
        frame = report.close_button.frame()
        self.assertLessEqual(frame.origin.y + frame.size.height, report.root.bounds().size.height)
        self.assertLessEqual(report.scroll.frame().origin.y + report.scroll.frame().size.height, frame.origin.y)
        self.capture(report, 'report-data-narrow')
        report.close_button.performClick_(None)
        self.assertFalse(report.window.isVisible())

    def test_escape_command_w_and_reopen_reuse_one_nonmodal_window(self):
        from AppKit import NSEvent, NSEventTypeKeyDown, NSEventModifierFlagCommand, NSApplication
        app, state = self.make_app()
        app.on_show_report(None)
        report = app.report_window
        escape = NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
            NSEventTypeKeyDown, (0, 0), 0, 0, report.window.windowNumber(), None, '\x1b', '\x1b', False, 53)
        self.assertTrue(report.window.performKeyEquivalent_(escape))
        self.assertFalse(report.window.isVisible())

        app.on_show_report(None)
        self.assertIs(app.report_window, report)
        self.assertTrue(report.window.isVisible())
        event = NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
            NSEventTypeKeyDown, (0, 0), NSEventModifierFlagCommand, 0, report.window.windowNumber(), None, 'w', 'w', False, 13)
        self.assertTrue(report.window.performKeyEquivalent_(event))
        self.assertFalse(report.window.isVisible())
        app.on_show_report(None)
        self.assertIsNone(NSApplication.sharedApplication().modalWindow())
        captured = report.snapshot['status']['wifi']
        state['snapshot']['status']['wifi'] = 'error'
        self.assertEqual(report.snapshot['status']['wifi'], captured)
        report.window.performClose_(None)
        self.assertFalse(report.window.isVisible())

    def test_handle_returns_to_live_panel_without_repairing_report_snapshot(self):
        app, state = self.make_app()
        app.panel = Mock()
        app.on_show_report(None)
        report = app.report_window
        state['snapshot']['issues'] = []
        report.handle_button.performClick_(None)
        self.assertFalse(report.window.isVisible())
        app.panel.show.assert_called_once_with()
        self.assertTrue(report.snapshot['issues'])

    def test_agent_records_timeline_is_bounded_and_can_reopen(self):
        from AppKit import NSApplication
        from relay.agent_records import records_summary
        from relay.report_window import ReportWindow
        NSApplication.sharedApplication()
        report = ReportWindow(Path(__file__).resolve().parents[1] / 'code/app_icon.png',
                              title='处理记录', summary_builder=records_summary)
        self.addCleanup(report.window.orderOut_, None)
        payload = {'records': [{'stage': 'finished', 'outcome': 'verified',
                               'events': [{'time': '2026-09-27T12:00:00', 'message': text}
                                          for text in ('检测到日常网页访问异常', '用户授权本次处理方案',
                                                       '保存原配置并执行获准修改', '目标恢复，其他连接未退化')],
                               'proposals': [{'plans': ['Wi-Fi · 恢复保存的日常上网地址设置']}]}] * 20}
        report.show(payload)
        report.window.setContentSize_((640, 440))
        report.layout()
        report.update_body()
        self.assertIn('已验证恢复', report.current_text)
        self.assertEqual(report.window.title(), 'NetCare · 处理记录')
        self.assertGreater(report.text_view.frame().size.height, report.scroll.contentSize().height)
        self.assertLessEqual(report.close_button.frame().origin.y + report.close_button.frame().size.height, 440)
        self.capture(report, 'agent-records-narrow')
        report.close_button.performClick_(None)
        self.assertFalse(report.window.isVisible())
        report.show(payload)
        self.assertTrue(report.window.isVisible())


if __name__ == '__main__':
    unittest.main()
