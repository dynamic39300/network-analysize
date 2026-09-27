"""Actual five-view native workspace, fake network boundary and real local IPC."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeWorkspaceTests(unittest.TestCase):
    def until(self, condition):
        from Foundation import NSDate, NSRunLoop
        deadline = time.monotonic() + 6
        while not condition() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.04))
        self.assertTrue(condition())

    def setUp(self):
        from AppKit import NSApplication
        from relay.core import CoreRuntime
        from relay.core_service import CoreService
        from relay.remote_desktop import build_connected_app_class
        from test_diagnostics import FakeMac, config
        from test_windows import FakeEvents
        from desktop_fixture import offline_desktop
        self.native = NSApplication.sharedApplication()
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.mac = FakeMac()
        self.runtime = CoreRuntime(Path(self.temp.name) / 'core', platform='darwin', runner=self.mac,
            config=config(self.mac, company=True), events_factory=FakeEvents, guard_threaded=False,
            execution_directory=Path(self.temp.name) / 'execution')
        self.service = CoreService(self.runtime)
        self.service.start()
        self.app = build_connected_app_class(self.runtime.data_dir, desktop_factory=offline_desktop)()
        self.host = self.app.workspace
        self.addCleanup(self.close)
        self.until(lambda: self.app.controller.ui_state()['ready'])

    def close(self):
        self.app.controller.close()
        self.app.controller.thread.join(6)
        for view in (self.host, self.app.review, self.app.receipt, self.app.report, self.app.task_report):
            view.window.setDelegate_(None)
            view.window.orderOut_(None)
        self.service.close()
        self.temp.cleanup()

    def show(self, name):
        self.host.show(name)
        self.until(lambda: name in self.host.loaded)
        return self.host.pages[name]

    def test_five_pages_share_one_window_navigation_does_not_probe_or_write(self):
        for name in ('overview', 'records', 'profiles', 'privacy', 'settings', 'overview'):
            page = self.show(name)
            self.assertIs(page.window, self.host.window)
            self.assertEqual(self.host.current, name)
            self.assertFalse(page.root.isHidden())
            self.assertEqual(page.root.superview(), self.host.content)
            self.assertEqual(sum(not p.root.isHidden() for p in self.host.pages.values()), 1)
        self.assertFalse(self.mac.calls)
        self.assertFalse(self.mac.mutations)
        self.assertFalse(self.runtime.guard.schedule.enabled)

    def test_netcare_brand_titles_and_sidebar_fit_without_changing_navigation(self):
        from AppKit import NSTextField
        self.host.window.setContentSize_((760, 560))
        self.host.layout()
        for window in (self.host.window, self.app.review.window, self.app.receipt.window,
                       self.app.report.window, self.app.task_report.window):
            self.assertTrue(str(window.title()).startswith('NetCare · '))
        brands = [view for view in self.host.root.subviews()
                  if isinstance(view, NSTextField) and str(view.stringValue()) == 'NetCare']
        self.assertEqual(len(brands), 1)
        self.assertLessEqual(brands[0].cell().cellSize().width, brands[0].frame().size.width)
        self.assertLessEqual(brands[0].frame().origin.x + brands[0].frame().size.width, 144)
        self.assertFalse(self.mac.calls)

    def test_navigation_keeps_profile_draft_and_field_focus_and_close_only_hides(self):
        page = self.show('profiles')
        page.name_field.setStringValue_('Unsaved profile')
        page.mark_dirty()
        self.host.window.makeFirstResponder_(page.name_field)
        self.show('settings')
        self.app.settings.key.setStringValue_('only-in-form')
        self.show('profiles')
        self.assertEqual(str(page.name_field.stringValue()), 'Unsaved profile')
        self.assertTrue(page.dirty)
        responder = self.host.window.firstResponder()
        self.assertTrue(responder == page.name_field or responder.delegate() == page.name_field)
        self.assertNotEqual(self.runtime.profiles.active['name'], 'Unsaved profile')
        self.host.window.performClose_(None)
        self.assertFalse(self.host.window.isVisible())
        self.assertFalse(self.runtime.closed)
        self.assertEqual(str(self.app.settings.key.stringValue()), '')
        self.assertFalse(self.mac.mutations)

    def test_late_read_response_does_not_navigate_away_or_drop_draft(self):
        profile = self.show('profiles')
        profile.name_field.setStringValue_('Draft remains here')
        profile.mark_dirty()
        self.show('overview')
        payload = {'profiles': copy.deepcopy(self.runtime.profiles.profiles), 'active_id': self.runtime.profiles.active['id'],
                   'assessment': self.runtime.profiles.evaluate(self.runtime.engine.snapshot())}
        self.host.receive('profiles', payload)
        self.assertEqual(self.host.current, 'overview')
        self.assertEqual(str(profile.name_field.stringValue()), 'Draft remains here')
        self.assertFalse(self.mac.calls)

    def test_current_task_links_to_record_evidence_and_redacted_report(self):
        run, _ = self.runtime.propose(['dns_mixed_on_vpn'])
        self.runtime.execute_proposal(run, self.runtime.agent._proposal_hash(run))
        self.service.refresh()
        self.until(lambda: self.app.controller.ui_state().get('run_id') == run.id)
        self.app.panel.actions.task_(None)
        self.until(lambda: self.app.records.current_detail is not None)
        self.assertEqual(self.host.current, 'records')
        self.assertEqual(self.app.records.selected()['id'], run.id)
        self.app.records.detail_mode.setSelectedSegment_(2)
        self.app.records.actions.detailMode_(None)
        self.assertIn('回读与计划值一致', str(self.app.records.detail.string()))
        from AppKit import NSBitmapImageFileTypePNG
        self.host.layout()
        self.host.window.displayIfNeeded()
        bitmap = self.host.root.bitmapImageRepForCachingDisplayInRect_(self.host.root.bounds())
        self.host.root.cacheDisplayInRect_toBitmapImageRep_(self.host.root.bounds(), bitmap)
        path = ROOT / 'build/ui-preview/workspace-changes.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))
        self.app.records.actions.report_(None)
        self.assertTrue(self.app.task_report.window.isVisible())
        self.assertNotIn('8.8.8.8', self.app.task_report.current_text)
        self.assertEqual(self.app.task_report.snapshot['record']['id'], run.id)
        old = copy.deepcopy(self.app.records.current_detail)
        self.app.records.show_detail({'record': {'id': 'old-selected-task'}})
        self.assertEqual(self.app.records.current_detail, old)

    def test_unknown_pending_outage_and_disconnection_states_preserve_actions(self):
        page = self.app.panel
        state = self.app.controller.ui_state()
        state.update(run_id='task', run_stage='awaiting_authorization')
        page.render(state)
        self.assertTrue(page.review.isEnabled())
        self.assertFalse(page.check.isEnabled())
        self.assertFalse(page.investigate.isEnabled())
        page.render({**state, 'ready': False, 'connection': 'disconnected'})
        self.assertFalse(page.review.isEnabled())
        self.assertFalse(page.guard.isEnabled())
        self.assertFalse(page.stop.isEnabled())
        page.render({**state, 'busy': '执行中'})
        self.assertTrue(page.stop.isEnabled())
        self.assertFalse(page.review.isEnabled())

    def test_minimum_width_all_pages_light_dark_and_responsive_forms(self):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG, NSTextField
        from Foundation import NSDate, NSRunLoop
        from relay.panel import text_height
        self.host.window.setContentSize_((760, 560))
        for name in self.host.pages:
            page = self.show(name)
            if name == 'profiles':
                page.name_field.setStringValue_('长档案名称与受保护目标的窗口适配测试')
                page.mark_dirty()
            for appearance, suffix in [('NSAppearanceNameAqua', 'light'), ('NSAppearanceNameDarkAqua', 'dark')]:
                self.host.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
                self.host.layout()
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.03))
                self.assertEqual(page.root.frame().size.width, 616)
                for child in page.root.subviews():
                    frame = child.frame()
                    self.assertGreaterEqual(frame.origin.x, -1)
                    self.assertLessEqual(frame.origin.x + frame.size.width, 617)
                    self.assertLessEqual(frame.origin.y + frame.size.height, 561)
                if hasattr(page, 'document'):
                    for child in page.document.subviews():
                        frame = child.frame()
                        self.assertGreaterEqual(frame.origin.x, -1)
                        self.assertLessEqual(frame.origin.x + frame.size.width, page.document.frame().size.width + 1)
                        if isinstance(child, NSTextField) and not child.isEditable() and child.maximumNumberOfLines() != 1:
                            self.assertGreaterEqual(frame.size.height + 3, text_height(child, frame.size.width))
                self.host.window.displayIfNeeded()
                bitmap = self.host.root.bitmapImageRepForCachingDisplayInRect_(self.host.root.bounds())
                self.host.root.cacheDisplayInRect_toBitmapImageRep_(self.host.root.bounds(), bitmap)
                for top, bottom in ((16, 55), (86, 210)):
                    colors = {round(bitmap.colorAtX_y_(x, y).redComponent(), 2)
                              for x in range(18, 135, 3) for y in range(top, bottom, 3)}
                    self.assertGreater(len(colors), 4, 'Brand/navigation was painted over by the content background')
                path = ROOT / 'build/ui-preview' / f'workspace-{name}-{suffix}.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))
        self.assertFalse(self.mac.calls)

    def test_long_targets_checks_and_inline_notice_remain_bounded(self):
        from test_panel import sample_state
        state = sample_state()
        state['agent'] = {'profile': {'health': 'unknown', 'name': '保护目标', 'total': 32, 'covered': 0,
            'observed_at': time.time(), 'targets': [{'id': str(i), 'name': '需要保护的公司业务服务' * 8,
                'state': 'unknown', 'label': '未确认', 'summary': '预期路径尚未确认' * 10, 'expected_path': 'vpn'} for i in range(32)]}}
        self.host.window.setContentSize_((760, 560))
        self.app.panel.render(state)
        self.app.panel.expanded.add('dns')
        self.app.panel.layout()
        self.assertGreater(self.app.panel.document.frame().size.height, 2000)
        self.app.panel.scroll.contentView().scrollToPoint_((0, 200))
        self.app.panel.layout()
        self.assertEqual(self.app.panel.scroll.contentView().bounds().origin.y, 200)
        self.host.notice('请求未完成', '当前核心不可用，未自动重发修改请求。' * 3)
        self.assertTrue(self.host.message)
        self.assertLess(self.host.content.frame().size.height, 560)
        self.host.actions.dismissNotice_(None)
        self.assertEqual(self.host.content.frame().size.height, 560)


if __name__ == '__main__':
    unittest.main()
