"""Opt-in Cocoa layout/interaction checks; all controller actions are simulated.

RELAY_NATIVE_UI_TESTS=1 python -m unittest discover -s tests -p test_panel_native.py
Native view renders are written to build/ui-preview for visual inspection.
"""
import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_panel import sample_state

ROOT = Path(__file__).resolve().parents[1]


class PreviewController:
    def __init__(self):
        self.state = sample_state()
        self.calls = []

    def ui_state(self):
        return copy.deepcopy(self.state)

    def check(self):
        self.calls.append(('check',))

    def set_guard_enabled(self, enabled):
        self.calls.append(('guard', enabled))
        self.state.setdefault('guard', {})['enabled'] = enabled

    def prepare_fix(self, issues):
        self.calls.append(('repair', issues))

    def confirm_fix(self, token, confirmed):
        self.calls.append(('confirm', token, confirmed))
        if not confirmed:
            self.state['repair']['phase'] = 'finished'
            self.state['repair']['outcome'] = 'cancelled'
            self.state['busy'] = ''


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from AppKit import NSApplication
        cls.app = NSApplication.sharedApplication()

    def setUp(self):
        from relay.panel import DiagnosticPanel
        self.controller = PreviewController()
        self.panel = DiagnosticPanel(self.controller, ROOT / 'code/app_icon.png', lambda _: None)
        self.panel.show()
        self.addCleanup(self.close)

    def close(self):
        if self.panel.sheet:
            self.panel.window.endSheet_(self.panel.sheet)
            self.panel.sheet.orderOut_(None)
        self.panel.window.setDelegate_(None)
        self.panel.window.orderOut_(None)

    def capture(self, name, sheet=False):
        from AppKit import NSBitmapImageFileTypePNG
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        window = self.panel.sheet if sheet else self.panel.window
        view = window.contentView()
        window.displayIfNeeded()
        bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
        output = ROOT / 'build/ui-preview' / (name + '.png')
        output.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(output), True))

    def assert_layout(self):
        from AppKit import NSTextField
        from relay.panel import text_height
        for parent in (self.panel.root, self.panel.document):
            for child in parent.subviews():
                frame = child.frame()
                self.assertGreaterEqual(frame.origin.x, 0)
                self.assertLessEqual(frame.origin.x + frame.size.width, parent.bounds().size.width + 1)
                if isinstance(child, NSTextField):
                    self.assertGreaterEqual(frame.size.height + 2, text_height(child, frame.size.width), child.stringValue())

    def test_light_dark_and_minimum_width(self):
        from AppKit import NSAppearance
        for appearance, name in [('NSAppearanceNameAqua', 'diagnostics-light'), ('NSAppearanceNameDarkAqua', 'diagnostics-dark')]:
            self.panel.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            self.panel.render(self.controller.ui_state())
            self.assert_layout()
            self.capture(name)
        self.panel.window.setContentSize_((760, 560))
        self.controller.state['snapshot']['check_errors']['dns'] = '检测命令没有返回有效数据，需要重新确认。' * 8
        self.panel.render(self.controller.ui_state())
        self.assert_layout()
        self.assertLessEqual(self.panel.rows_height, self.panel.scroll.contentSize().height)
        self.capture('diagnostics-narrow')
        self.panel.expanded.add('dns')
        self.panel.render(self.controller.ui_state())
        self.assertGreater(self.panel.rows_height, self.panel.scroll.contentSize().height)
        self.panel.scroll.contentView().scrollToPoint_((0, self.panel.document.bounds().size.height - self.panel.scroll.contentSize().height))
        self.panel.scroll.reflectScrolledClipView_(self.panel.scroll.contentView())
        self.capture('diagnostics-narrow-bottom')

    def test_guard_switch_and_attention_are_visible_without_modal_confirmation(self):
        self.controller.state['guard'] = {'enabled': True, 'phase': 'waiting', 'event_source': 'native',
                                          'proposal_ready': True, 'attention': True}
        self.panel.window.setContentSize_((760, 560))
        self.panel.render(self.controller.ui_state())
        self.assertEqual(self.panel.guard_switch.state(), 1)
        self.assertEqual(self.panel.optimize_button.title(), '查看方案')
        self.assertIsNone(self.panel.sheet)
        self.assert_layout()
        self.capture('guard-attention-narrow')
        self.panel.guard_switch.setState_(0)
        self.panel.actions.guard_(self.panel.guard_switch)
        self.assertIn(('guard', False), self.controller.calls)
        self.controller.state['busy'] = '守护检查'
        self.panel.render(self.controller.ui_state())
        self.assertTrue(self.panel.guard_switch.isEnabled())

    def test_item_button_passes_only_its_issue_and_window_reopens(self):
        dns = next(control for control in self.panel.row_buttons if self.panel.rows[control.tag()]['id'] == 'dns')
        dns.performClick_(None)
        self.assertEqual(self.controller.calls, [('repair', ['dns_company_leftover'])])
        self.panel.window.performClose_(None)
        self.assertFalse(self.panel.window.isVisible())
        self.panel.show()
        self.assertTrue(self.panel.window.isVisible())

    def test_guided_handling_opens_settings_and_rechecks_without_repair(self):
        self.controller.state['snapshot']['issues'].append(('high', 'wifi_disconnected', '无网络'))
        self.panel.render(self.controller.ui_state())
        control = next(b for b in self.panel.row_buttons if self.panel.rows[b.tag()]['id'] == 'wifi')
        self.assertEqual(control.title(), '立即帮我处理')
        control.performClick_(None)
        self.assertEqual(self.controller.calls, [])
        self.assertIn('wifi', self.panel.handling)
        settings = next(b for b in self.panel.handling_buttons if b.title() == '打开网络设置')
        with patch('relay.panel.open_network_settings', return_value=True) as opened:
            settings.performClick_(None)
            opened.assert_called_once_with()
        recheck = next(b for b in self.panel.handling_buttons if b.title() == '重新检测')
        recheck.performClick_(None)
        self.assertEqual(self.controller.calls, [('check',)])
        self.panel.window.setContentSize_((760, 560))
        self.panel.render(self.controller.ui_state())
        self.assert_layout()
        self.capture('handling-network-narrow')

    def test_changed_issue_is_not_repaired_from_stale_button(self):
        control = next(b for b in self.panel.row_buttons if self.panel.rows[b.tag()]['id'] == 'dns')
        self.controller.state['snapshot']['issues'] = []
        control.performClick_(None)
        self.assertEqual(self.controller.calls, [])

    def test_settings_failure_and_busy_state_are_visible(self):
        self.controller.state['repair_options'] = {}
        control = next(b for b in self.panel.row_buttons if self.panel.rows[b.tag()]['id'] == 'dns')
        control.performClick_(None)
        settings = next(b for b in self.panel.handling_buttons if b.title() == '打开网络设置')
        with patch('relay.panel.open_network_settings', return_value=False):
            settings.performClick_(None)
        self.assertIn('未能打开', self.panel.settings_error)
        self.assertTrue(any('未能打开' in view.stringValue() for view in self.panel.document.subviews()
                            if hasattr(view, 'stringValue')))
        self.controller.state['busy'] = '检测中'
        self.panel.render(self.controller.ui_state())
        self.assertTrue(all(not b.isEnabled() for b in self.panel.handling_buttons + self.panel.row_buttons))
        self.assertEqual(self.controller.calls, [])

    def test_server_problem_never_offers_network_mutation(self):
        self.controller.state['snapshot']['issues'].append(('medium', 'service_example', '网站拒绝访问'))
        self.controller.state['snapshot']['status']['reachability_results']['Example'].update(service='access_denied', http_status=403)
        self.panel.render(self.controller.ui_state())
        control = next(b for b in self.panel.row_buttons if self.panel.rows[b.tag()]['id'] == 'reachability')
        control.performClick_(None)
        self.assertEqual(self.controller.calls, [])
        self.assertEqual([b.title() for b in self.panel.handling_buttons], ['重新检测'])
        self.capture('handling-service')

    def test_repair_confirmation_progress_and_terminal_states(self):
        repair = {'token': 'preview-only', 'phase': 'awaiting_confirmation', 'outcome': None,
                  'plans': ['恢复自动获取 DNS（DHCP）'], 'events': [], 'results': []}
        self.controller.state.update(repair=repair, busy='准备安全修复')
        self.panel.render(self.controller.ui_state())
        self.capture('repair-confirm', sheet=True)
        confirm = next(view for view in self.panel.sheet_root.subviews() if hasattr(view, 'title') and view.title() == '开始优化')
        confirm.performClick_(None)
        self.assertEqual(self.controller.calls, [('confirm', 'preview-only', True)])
        repair.update(phase='verifying', events=[{'time': '10:30:01', 'message': '原有配置已保存'},
                                                {'time': '10:30:02', 'message': '恢复自动获取 DNS'},
                                                {'time': '10:30:03', 'message': '复检目标问题及网络连通性'}])
        self.panel.render(self.controller.ui_state())
        self.assertFalse(self.panel.actions.windowShouldClose_(self.panel.window))
        self.capture('repair-running', sheet=True)
        for outcome in ('verified', 'rolled_back', 'rollback_failed', 'blocked'):
            repair.update(phase='finished', outcome=outcome)
            self.controller.state['busy'] = ''
            self.panel.render(self.controller.ui_state())
            self.capture('repair-' + outcome, sheet=True)
        self.panel.dismiss_repair()
        self.assertIsNone(self.panel.sheet)
        self.panel.render(self.controller.ui_state())
        self.assertIsNone(self.panel.sheet)

    def test_scan_rows_disable_repair_until_fresh_results_complete(self):
        self.controller.state.update(busy='网络检测', live_snapshot={'status': {'wifi': 'ok', 'wifi_ip': '192.168.1.8'},
                                                                 'issues': [], 'check_errors': {}},
                                     scan_progress={'check': 'vpn', 'phase': 'running'})
        self.panel.render(self.controller.ui_state())
        self.assertTrue(all(not control.isEnabled() for control in self.panel.row_buttons))
        self.assertEqual(next(row['state'] for row in self.panel.rows if row['id'] == 'vpn'), '检测中')
        self.capture('diagnostics-scanning')

    def test_one_click_optimization_selects_all_eligible_issues(self):
        self.controller.state['snapshot']['issues'] += [('high', 'proxy_leftover', 'proxy'), ('low', 'vpn_unconfirmed', 'vpn')]
        self.controller.state['repair_options']['proxy_leftover'] = ['关闭失效转发']
        self.panel.render(self.controller.ui_state())
        self.panel.optimize_button.performClick_(None)
        self.assertEqual(self.controller.calls, [('repair', ['dns_company_leftover', 'proxy_leftover'])])
        self.controller.state['busy'] = '准备安全修复'
        self.panel.render(self.controller.ui_state())
        self.assertFalse(self.panel.optimize_button.isEnabled())

    def test_seven_rows_fit_and_details_expand_on_demand(self):
        self.assertEqual(len(self.panel.rows), 7)
        self.assertLessEqual(self.panel.rows_height, self.panel.scroll.contentSize().height)
        index = next(index for index, row in enumerate(self.panel.rows) if row['id'] == 'dns')
        self.panel.details_buttons[index].performClick_(None)
        self.assertIn('dns', self.panel.expanded)
        self.capture('diagnostics-expanded')
        self.panel.details_buttons[index].performClick_(None)
        self.assertNotIn('dns', self.panel.expanded)
        self.assertLessEqual(self.panel.rows_height, self.panel.scroll.contentSize().height)

    def test_opening_panel_waits_for_manual_detection(self):
        self.controller.state.update(snapshot={'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None}, repair_options={})
        self.panel.window.setContentSize_((760, 560))
        self.panel.render(self.controller.ui_state())
        self.panel.window.performClose_(None)
        self.panel.show()
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.panel.check_button.title(), '检测')
        self.assertTrue(self.panel.check_button.isEnabled())
        self.assertFalse(self.panel.optimize_button.isEnabled())
        self.assertEqual(self.panel.row_buttons, [])
        self.assert_layout()
        self.assertLessEqual(self.panel.rows_height, self.panel.scroll.contentSize().height)
        self.capture('diagnostics-manual-idle')
        self.panel.check_button.performClick_(None)
        self.assertEqual(self.controller.calls, [('check',)])
