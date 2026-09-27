"""Native scope selection and revocation connected to the test core, not host networking."""
import copy
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
import test_workspace_native as workspace_fixture


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeTrustTests(unittest.TestCase):
    setUp = workspace_fixture.NativeWorkspaceTests.setUp
    close = workspace_fixture.NativeWorkspaceTests.close
    until = workspace_fixture.NativeWorkspaceTests.until
    show = workspace_fixture.NativeWorkspaceTests.show

    def prepare(self):
        self.app.controller.prepare_fix(['dns_mixed_on_vpn'])
        self.until(lambda: self.app.review.review is not None)
        return self.app.review

    def choose(self, view, mode):
        view.mode.selectItemAtIndex_(mode)
        view.actions.mode_(view.mode)

    def test_default_single_confirmation_and_explicit_scope_acknowledgment(self):
        view = self.prepare()
        self.assertEqual(view.mode.indexOfSelectedItem(), 0)
        self.assertTrue(view.confirm.isEnabled())
        self.choose(view, 1)
        self.assertFalse(view.confirm.isEnabled())
        self.assertIn('trust_scope', str(view.text.string()))
        self.assertIn('scope_hash', str(view.text.string()))
        view.decide(True)
        self.assertIsNone(self.runtime.trust.current)
        view.trust_ack.setState_(1)
        view.update_enabled()
        self.assertTrue(view.confirm.isEnabled())
        self.choose(view, 2)
        self.assertFalse(view.trust_ack.state())
        self.assertFalse(view.confirm.isEnabled())
        self.assertFalse(self.mac.mutations)

    def test_native_confirm_and_privacy_revoke_with_real_ipc(self):
        view = self.prepare()
        self.choose(view, 2)
        view.trust_ack.setState_(1)
        view.decide(True)
        self.until(lambda: self.runtime.agent._last_run.outcome == 'verified')
        self.until(lambda: self.app.controller.pending is None)
        policy = self.runtime.trust.status()['grant']
        self.assertEqual(policy['mode'], 'continuous')
        self.assertFalse(self.runtime.guard.schedule.enabled)
        privacy = self.show('privacy')
        self.assertEqual(privacy.payload['trust']['grant']['id'], policy['id'])
        privacy.actions.revokeTrust_(None)
        self.until(lambda: self.runtime.trust.status()['grant']['state'] == 'revoked')
        self.until(lambda: privacy.payload['trust']['grant']['state'] == 'revoked')
        self.assertFalse(self.runtime.task_cancelled.is_set())
        self.assertIsNone(self.runtime.model_consent)

    def test_cancel_scope_selection_does_not_create_authority(self):
        view = self.prepare()
        self.choose(view, 1)
        view.trust_ack.setState_(1)
        view.decide(False)
        self.until(lambda: self.runtime.agent._last_run.stage == 'cancelled')
        self.assertIsNone(self.runtime.trust.current)
        self.assertFalse(self.mac.mutations)

    def test_disconnect_revocation_and_new_review_reset_trust_choice(self):
        view = self.prepare()
        review = copy.deepcopy(view.review)
        self.choose(view, 2)
        view.trust_ack.setState_(1)
        view.update_state({**self.app.controller.ui_state(), 'trust': {'revision': 'changed'}})
        self.assertFalse(view.confirm.isEnabled())
        view.show(review)
        self.assertEqual(view.mode.indexOfSelectedItem(), 0)
        self.assertFalse(view.trust_ack.state())
        self.choose(view, 1)
        view.trust_ack.setState_(1)
        view.update_state({'ready': False})
        self.assertFalse(view.confirm.isEnabled())
        self.assertFalse(view.mode.isEnabled())

    def test_scope_review_and_active_privacy_at_minimum_size_light_dark(self):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG, NSTextField
        from Foundation import NSDate, NSRunLoop
        from relay.panel import text_height
        view = self.prepare()
        self.choose(view, 2)
        view.window.setContentSize_((760, 560))
        for appearance, suffix in [('NSAppearanceNameAqua', 'light'), ('NSAppearanceNameDarkAqua', 'dark')]:
            view.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            view.layout()
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.03))
            for child in view.root.subviews():
                frame = child.frame()
                self.assertLessEqual(frame.origin.x + frame.size.width, 761)
                self.assertLessEqual(frame.origin.y + frame.size.height, 561)
                if isinstance(child, NSTextField):
                    self.assertGreaterEqual(frame.size.height + 3, text_height(child, frame.size.width))
            self.assertGreater(view.text.frame().size.height, view.scroll.contentSize().height)
            self.assertLessEqual(view.scroll.frame().origin.y + view.scroll.frame().size.height, view.mode.frame().origin.y)
            view.window.displayIfNeeded()
            bitmap = view.root.bitmapImageRepForCachingDisplayInRect_(view.root.bounds())
            view.root.cacheDisplayInRect_toBitmapImageRep_(view.root.bounds(), bitmap)
            path = ROOT / 'build/ui-preview' / f'trust-review-{suffix}.png'
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))
        view.trust_ack.setState_(1)
        view.decide(True)
        self.until(lambda: self.runtime.agent._last_run.outcome == 'verified')
        self.until(lambda: self.app.controller.pending is None)
        self.host.window.setContentSize_((760, 560))
        privacy = self.show('privacy')
        self.host.window.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
        self.host.layout()
        for child in privacy.document.subviews():
            frame = child.frame()
            self.assertLessEqual(frame.origin.x + frame.size.width, privacy.document.frame().size.width + 1)
            if isinstance(child, NSTextField):
                self.assertGreaterEqual(frame.size.height + 3, text_height(child, frame.size.width))
        self.host.window.displayIfNeeded()
        bitmap = self.host.root.bitmapImageRepForCachingDisplayInRect_(self.host.root.bounds())
        self.host.root.cacheDisplayInRect_toBitmapImageRep_(self.host.root.bounds(), bitmap)
        path = ROOT / 'build/ui-preview/trust-privacy-dark.png'
        self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))


if __name__ == '__main__':
    unittest.main()
