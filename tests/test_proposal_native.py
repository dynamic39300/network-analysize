"""Actual Cocoa rendering for exact private proposal review; no network writes."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from AppKit import NSApplication
        cls.app = NSApplication.sharedApplication()

    def setUp(self):
        from relay.proposal_window import ProposalWindow
        self.decisions = []
        self.view = ProposalWindow(lambda *value: self.decisions.append(value))
        self.review = {'review_token': 'a' * 32, 'proposal_hash': 'b' * 64, 'core_instance': 'c' * 32,
                       'run_id': 'run-example', 'expires_at': time.time() + 300, 'unrestricted': True,
                       'proposal': {'plans': ['检查完整命令及其作用范围'], 'actions': [
                           {'argv': ['/bin/sh', '-c', 'printf "preview only"'],
                            'stdin': '\n'.join('Visible line ' + str(i) for i in range(120)),
                            'impact': 'Unrestricted current-user effects', 'recovery': 'Manual review required'}]}}
        self.view.show(self.review)
        self.addCleanup(self.close)

    def close(self):
        self.view.window.setDelegate_(None)
        self.view.window.orderOut_(None)

    def test_dynamic_explicit_risk_acknowledgment_and_single_decision(self):
        self.assertFalse(self.view.confirm.isEnabled())
        self.view.decide(True)
        self.assertFalse(self.decisions)
        self.view.acknowledge.setState_(1)
        self.view.actions.acknowledge_(self.view.acknowledge)
        self.assertTrue(self.view.confirm.isEnabled())
        self.view.actions.confirm_(None)
        self.view.decide(True)
        self.assertEqual(len(self.decisions), 1)
        self.assertTrue(self.decisions[0][1])
        self.assertEqual(self.decisions[0][0]['proposal_hash'], self.review['proposal_hash'])

    def test_disconnect_expiry_and_changed_core_disable_confirmation(self):
        self.view.acknowledge.setState_(1)
        self.view.update_state({'ready': False})
        self.assertFalse(self.view.confirm.isEnabled())
        self.view.show({**self.review, 'expires_at': time.time() - 1})
        self.assertFalse(self.view.confirm.isEnabled())
        self.view.show(self.review)
        self.view.update_state({'ready': True, 'core_instance': 'another', 'run_id': self.review['run_id'],
                                'run_stage': 'awaiting_authorization', 'busy': ''})
        self.assertFalse(self.view.confirm.isEnabled())
        self.assertFalse(self.decisions)

    def test_mature_review_and_cancel_do_not_require_dynamic_acknowledgment(self):
        self.view.show({**self.review, 'unrestricted': False})
        self.assertTrue(self.view.confirm.isEnabled())
        self.view.actions.cancel_(None)
        self.assertEqual(len(self.decisions), 1)
        self.assertFalse(self.decisions[0][1])

    def test_long_private_content_minimum_width_light_dark_and_scroll(self):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG, NSTextField
        from Foundation import NSDate, NSRunLoop
        from relay.panel import text_height
        self.view.window.setContentSize_((760, 560))
        for appearance, name in [('NSAppearanceNameAqua', 'proposal-review-light'),
                                 ('NSAppearanceNameDarkAqua', 'proposal-review-dark')]:
            self.view.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            self.view.layout()
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            self.assertIn('Visible line 119', self.view.text.string())
            self.assertFalse(self.view.text.isEditable())
            self.assertGreater(self.view.text.frame().size.height, self.view.scroll.contentSize().height)
            self.assertLessEqual(self.view.text.frame().size.width, self.view.scroll.contentSize().width + 1)
            for child in self.view.root.subviews():
                frame = child.frame()
                self.assertGreaterEqual(frame.origin.x, 0)
                self.assertLessEqual(frame.origin.x + frame.size.width, 761)
                self.assertLessEqual(frame.origin.y + frame.size.height, 561)
                if isinstance(child, NSTextField):
                    self.assertGreaterEqual(frame.size.height + 2, text_height(child, frame.size.width))
            self.view.window.displayIfNeeded()
            bitmap = self.view.root.bitmapImageRepForCachingDisplayInRect_(self.view.root.bounds())
            self.view.root.cacheDisplayInRect_toBitmapImageRep_(self.view.root.bounds(), bitmap)
            path = ROOT / 'build/ui-preview' / (name + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))

    def test_connected_desktop_renders_real_ipc_state_and_closing_does_not_close_core(self):
        from Foundation import NSDate, NSRunLoop
        from relay.core import CoreRuntime
        from relay.core_service import CoreService
        from relay.remote_desktop import build_connected_app_class
        from desktop_fixture import offline_desktop
        from relay.ipc import LocalClient
        from test_diagnostics import FakeMac, config
        from test_windows import FakeEvents
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            mac = FakeMac()
            runtime = CoreRuntime(Path(directory) / 'core', platform='darwin', runner=mac, config=config(mac, company=True),
                events_factory=FakeEvents, guard_threaded=False, execution_directory=Path(directory) / 'execution')
            service = CoreService(runtime)
            service.start()
            app = build_connected_app_class(runtime.data_dir, desktop_factory=offline_desktop)()
            try:
                deadline = time.monotonic() + 5
                while not app.controller.ui_state()['ready'] and time.monotonic() < deadline:
                    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
                self.assertTrue(app.controller.ui_state()['ready'])
                self.assertEqual(app.controller.ui_state()['core_instance'], service.instance)
                self.assertFalse(mac.calls)
                app.controller.check()
                deadline = time.monotonic() + 5
                while not app.controller.ui_state()['snapshot'].get('last_check') and time.monotonic() < deadline:
                    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
                self.assertIsNotNone(app.controller.ui_state()['snapshot']['last_check'])
                self.assertFalse(mac.mutations)
                app.controller.close()
                app.controller.thread.join(6)
                self.assertFalse(app.controller.thread.is_alive())
                self.assertTrue(LocalClient(runtime.data_dir).call('status')['ready'])
                self.assertFalse(runtime.closed)
            finally:
                app.controller.close()
                app.controller.thread.join(6)
                for view in (app.panel, app.review, app.records, app.report, app.profiles, app.receipt, app.settings, app.privacy):
                    view.window.setDelegate_(None)
                    view.window.orderOut_(None)
                service.close()
