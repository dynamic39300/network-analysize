"""Actual Cocoa controls, rendering and connected management workflow."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


def settings_payload():
    return {'core_instance': 'c' * 32, 'model': {'configured': True, 'name': 'network-model',
        'endpoint': 'https://example.invalid/v1/responses', 'credential_present': True,
        'credential_storage': 'core_memory', 'revision': 'a' * 32, 'binding': 'b' * 64,
        'local': False, 'consented': False, 'data_schema': 'relay-minimal-evidence-v2'},
        'preferences': {'revision': 2, 'values': {'history_days': 30, 'history_limit': 1000, 'notifications_enabled': False}},
        'data_directory': '/Users/example/Library/Application Support/Relay-core-preview',
        'privacy_events': [], 'recovery_pending': True}


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from AppKit import NSApplication
        cls.app = NSApplication.sharedApplication()

    def setUp(self):
        from relay.management_window import PrivacyWindow, SettingsWindow
        from relay.receipt_window import ReceiptWindow
        from relay.records_window import RecordsWindow
        self.controller = Mock()
        self.settings = SettingsWindow(self.controller)
        self.privacy = PrivacyWindow(self.controller)
        self.receipt = ReceiptWindow(self.controller.confirm_receipt, self.controller.confirm_recovery)
        self.records = RecordsWindow(self.controller)
        self.views = [self.settings, self.privacy, self.receipt, self.records]
        self.payload = settings_payload()
        self.state = {'ready': True, 'busy': '', 'core_instance': self.payload['core_instance'],
                      'model': copy.deepcopy(self.payload['model']), 'run_stage': 'finished'}
        for view in self.views:
            view.update_state(self.state)
        self.settings.show(self.payload)
        self.privacy.show(self.payload)
        self.addCleanup(self.close)

    def close(self):
        for view in self.views:
            view.window.setDelegate_(None)
            view.window.orderOut_(None)

    def consent_review(self):
        return {'model': self.payload['model'], 'binding': 'b' * 64, 'revision': 'a' * 32,
                'review_token': 'd' * 32, 'core_instance': 'c' * 32, 'expires_at': time.time() + 300}

    def receipt_review(self):
        return {'run_id': 'run-private', 'receipt_hash': 'e' * 64, 'review_token': 'd' * 32,
                'core_instance': 'c' * 32, 'expires_at': time.time() + 300, 'can_acknowledge': True,
                'stage': 'needs_reconciliation', 'outcome': 'needs_review', 'receipt': {
                    'kind': 'dynamic_command', 'job': {'stdout': '\n'.join('Private line ' + str(i) for i in range(150))}}}

    def test_model_save_uses_secure_field_clears_key_and_does_not_grant_consent(self):
        from AppKit import NSSecureTextField
        self.assertIsInstance(self.settings.key, NSSecureTextField)
        self.settings.key_action.selectItemAtIndex_(0)
        self.settings.key.setStringValue_('new-test-key')
        self.settings.save_model()
        self.controller.configure_model.assert_called_once()
        values = self.controller.configure_model.call_args.kwargs
        self.assertEqual(values['api_key'], 'new-test-key')
        self.assertEqual(values['credential_action'], 'replace')
        self.assertEqual(str(self.settings.key.stringValue()), '')
        self.controller.confirm_consent.assert_not_called()
        self.settings.key.setStringValue_('discard-on-close')
        self.settings.actions.windowShouldClose_(self.settings.window)
        self.assertEqual(str(self.settings.key.stringValue()), '')

    def test_model_endpoint_change_cannot_keep_previous_key(self):
        self.settings.endpoint.setStringValue_('https://another.invalid/responses')
        self.settings.save_model()
        self.controller.configure_model.assert_not_called()
        self.assertIn('换地址', str(self.settings.error.stringValue()))
        self.settings.update_state({**self.state, 'ready': False})
        self.assertFalse(self.settings.save_model_button.isEnabled())

    def test_preference_validation_and_no_authority_fields(self):
        self.settings.days.setStringValue_('0')
        self.settings.save_preferences()
        self.controller.save_preferences.assert_not_called()
        self.settings.days.setStringValue_('7')
        self.settings.limit.setStringValue_('100')
        self.settings.notifications.setState_(1)
        self.settings.save_preferences()
        self.controller.save_preferences.assert_called_once_with(
            {'history_days': 7, 'history_limit': 100, 'notifications_enabled': True}, 2)

    def test_upload_consent_requires_current_review_and_checkbox_and_expires(self):
        self.assertFalse(self.privacy.allow_button.isEnabled())
        self.privacy.show_consent(self.consent_review())
        self.privacy.allow()
        self.controller.confirm_consent.assert_not_called()
        self.privacy.acknowledge.setState_(1)
        self.privacy.actions.edit_(None)
        self.assertTrue(self.privacy.allow_button.isEnabled())
        self.privacy.allow()
        self.controller.confirm_consent.assert_called_once()
        self.assertFalse(self.privacy.allow_button.isEnabled())
        self.privacy.show_consent({**self.consent_review(), 'expires_at': time.time() - 1})
        self.privacy.acknowledge.setState_(1)
        self.privacy.update_enabled()
        self.assertFalse(self.privacy.allow_button.isEnabled())

    def test_upload_consent_invalidates_on_disconnect_model_change_and_window_close(self):
        for state in ({**self.state, 'ready': False},
                      {**self.state, 'model': {**self.state['model'], 'revision': 'f' * 32}}):
            self.privacy.show_consent(self.consent_review())
            self.privacy.acknowledge.setState_(1)
            self.privacy.update_state(state)
            self.assertFalse(self.privacy.allow_button.isEnabled())
            self.assertIsNone(self.privacy.review)
        self.privacy.show_consent(self.consent_review())
        self.privacy.actions.windowShouldClose_(self.privacy.window)
        self.assertIsNone(self.privacy.review)
        self.controller.confirm_consent.assert_not_called()

    def test_receipt_acknowledgment_requires_note_and_explicit_ack_never_double_submits(self):
        self.receipt.show(self.receipt_review())
        self.assertFalse(self.receipt.confirm.isEnabled())
        self.receipt.note.setStringValue_('Reviewed temporary command effects.')
        self.receipt.update_enabled()
        self.assertFalse(self.receipt.confirm.isEnabled())
        self.receipt.acknowledge.setState_(1)
        self.receipt.update_enabled()
        self.assertTrue(self.receipt.confirm.isEnabled())
        self.receipt.decide()
        self.receipt.decide()
        self.controller.confirm_receipt.assert_called_once()
        self.assertIn('Private line 149', self.receipt.text.string())
        self.receipt.show(self.receipt_review())
        self.receipt.update_state({**self.state, 'ready': False})
        self.assertFalse(self.receipt.note.isEnabled())
        self.receipt.show({**self.receipt_review(), 'can_acknowledge': False})
        self.assertFalse(self.receipt.confirm.isEnabled())

    def test_records_search_and_all_pending_recovery_entry(self):
        payload = {'records': [{'id': 'task-1', 'stage': 'finished', 'outcome': 'verified', 'has_receipt': True,
                               'events': [{'time': '2026-09-27T10:00:00', 'message': '网络恢复测试'}]}],
                   'pending': [{'id': 'old-task', 'stage': 'needs_reconciliation', 'outcome': 'needs_review', 'has_receipt': True}]}
        self.records.show(payload)
        self.assertEqual(self.records.selected()['id'], 'task-1')
        self.records.search.setStringValue_('不存在')
        self.records.filter()
        self.assertEqual(self.records.rows, [])
        self.assertFalse(self.records.inspect.isEnabled())
        self.records.search.setStringValue_('')
        self.records.scope.selectItemAtIndex_(1)
        self.records.filter()
        self.records.actions.inspect_(None)
        self.controller.review_receipt.assert_called_once_with('old-task')
        self.records.update_state({**self.state, 'ready': False})
        self.assertFalse(self.records.inspect.isEnabled())

    def recovery_review(self):
        return {**self.receipt_review(), 'can_acknowledge': False,
            'receipt': {'actions': [{'field': 'dns', 'service': 'Wi-Fi', 'desired': ['1.1.1.1']}], 'outcome': 'rollback_failed'},
            'recovery': {
            'state': 'pending', 'eligible': True, 'can_restore': True, 'can_retain': True, 'fields': [{
                'field': 'dns', 'state': 'desired', 'target': {'name': 'Wi-Fi', 'interface': 'en0', 'service_id': 'EXACT-SERVICE-ID'},
                'expected': ['10.0.0.53'], 'actual_known': True, 'actual': ['1.1.1.1']}]}}

    def test_native_recovery_needs_checkbox_current_review_and_never_human_ack(self):
        review = self.recovery_review()
        self.receipt.show(review)
        self.receipt.recover('restore')
        self.controller.confirm_recovery.assert_not_called()
        self.assertIn('原值：["10.0.0.53"]', self.receipt.text.string())
        self.receipt.acknowledge.setState_(1)
        self.receipt.recover('restore')
        self.receipt.recover('retain')
        self.controller.confirm_recovery.assert_called_once_with(review, 'restore')
        self.controller.confirm_receipt.assert_not_called()
        for state in ({**self.state, 'ready': False}, {**self.state, 'busy': 'check'},
                      {**self.state, 'core_instance': 'changed'}):
            self.receipt.show(review)
            self.receipt.acknowledge.setState_(1)
            self.receipt.update_state(state)
            self.assertFalse(self.receipt.restore.isEnabled())
            self.assertFalse(self.receipt.retain.isEnabled())
        self.receipt.show({**review, 'expires_at': time.time() - 1})
        self.receipt.acknowledge.setState_(1)
        self.receipt.update_enabled()
        self.assertFalse(self.receipt.restore.isEnabled())
        self.receipt.show(review)
        self.receipt.actions.windowShouldClose_(self.receipt.window)
        self.assertFalse(self.receipt.valid)

    def test_native_recovery_disabled_states_and_minimum_light_dark_render(self):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG
        from Foundation import NSDate, NSRunLoop
        review = self.recovery_review()
        self.receipt.window.setContentSize_((760, 560))
        for state in ('pending', 'unavailable', 'finished'):
            payload = {**review, 'recovery': {**review['recovery'], 'state': state, 'can_restore': False, 'can_retain': False}}
            self.receipt.show(payload)
            self.receipt.acknowledge.setState_(1)
            self.receipt.update_enabled()
            self.assertFalse(self.receipt.restore.isEnabled())
            self.assertFalse(self.receipt.retain.isEnabled())
        self.receipt.show(review)
        self.receipt.acknowledge.setState_(1)
        for appearance, suffix in [('NSAppearanceNameAqua', 'light'), ('NSAppearanceNameDarkAqua', 'dark')]:
            self.receipt.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            self.receipt.layout()
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.03))
            children = list(self.receipt.root.subviews())
            for child in children:
                frame = child.frame()
                self.assertGreaterEqual(frame.origin.x, 0)
                self.assertLessEqual(frame.origin.x + frame.size.width, 761)
                self.assertLessEqual(frame.origin.y + frame.size.height, 561)
            for left, right in zip(sorted(children, key=lambda v: (v.frame().origin.y, v.frame().origin.x)),
                                   sorted(children, key=lambda v: (v.frame().origin.y, v.frame().origin.x))[1:]):
                a, b = left.frame(), right.frame()
                self.assertTrue(a.origin.y + a.size.height <= b.origin.y + 1 or
                                a.origin.x + a.size.width <= b.origin.x + 1)
            self.receipt.window.displayIfNeeded()
            bitmap = self.receipt.root.bitmapImageRepForCachingDisplayInRect_(self.receipt.root.bounds())
            self.receipt.root.cacheDisplayInRect_toBitmapImageRep_(self.receipt.root.bounds(), bitmap)
            path = ROOT / 'build/ui-preview' / ('recovery-' + suffix + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))

    def test_minimum_window_light_dark_long_content_no_overlap(self):
        from AppKit import NSAppearance, NSBitmapImageFileTypePNG, NSTextField
        from Foundation import NSDate, NSRunLoop
        from relay.panel import text_height
        self.privacy.show_consent(self.consent_review())
        self.receipt.show(self.receipt_review())
        self.records.show({'records': [{'id': 'run-12', 'stage': 'finished', 'outcome': 'reviewed',
            'has_receipt': True, 'events': [{'time': '2026-09-27T17:23:45', 'message': '用户人工核对；非自动验证。'}]}], 'pending': []})
        for view, name in zip(self.views, ('settings', 'privacy', 'receipt', 'records')):
            view.window.setContentSize_((760, 560))
            for appearance, suffix in [('NSAppearanceNameAqua', 'light'), ('NSAppearanceNameDarkAqua', 'dark')]:
                view.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
                view.layout()
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.03))
                for child in view.root.subviews():
                    frame = child.frame()
                    self.assertGreaterEqual(frame.origin.x, 0)
                    self.assertLessEqual(frame.origin.x + frame.size.width, 761)
                    self.assertLessEqual(frame.origin.y + frame.size.height, 561)
                if hasattr(view, 'document'):
                    self.assertGreater(view.document.frame().size.height, view.scroll.contentSize().height)
                    for child in view.document.subviews():
                        if isinstance(child, NSTextField) and not child.isEditable():
                            frame = child.frame()
                            self.assertGreaterEqual(frame.size.height + 2, text_height(child, frame.size.width))
                view.window.displayIfNeeded()
                bitmap = view.root.bitmapImageRepForCachingDisplayInRect_(view.root.bounds())
                view.root.cacheDisplayInRect_toBitmapImageRep_(view.root.bounds(), bitmap)
                path = ROOT / 'build/ui-preview' / (name + '-' + suffix + '.png')
                path.parent.mkdir(parents=True, exist_ok=True)
                self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True))

    def test_connected_desktop_settings_and_receipt_workflow_uses_same_core(self):
        from Foundation import NSDate, NSRunLoop
        from relay.core import CoreRuntime
        from relay.core_service import CoreService
        from relay.remote_desktop import build_connected_app_class
        from desktop_fixture import offline_desktop
        from test_diagnostics import FakeMac, config
        from test_dynamic import request
        from test_windows import FakeEvents
        def until(condition):
            deadline = time.monotonic() + 6
            while not condition() and time.monotonic() < deadline:
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            self.assertTrue(condition())
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            mac = FakeMac()
            runtime = CoreRuntime(Path(directory) / 'core', platform='darwin', runner=mac, config=config(mac, company=True),
                events_factory=FakeEvents, guard_threaded=False, execution_directory=Path(directory) / 'execution')
            service = CoreService(runtime)
            service.start()
            app = build_connected_app_class(runtime.data_dir, desktop_factory=offline_desktop)()
            try:
                until(lambda: app.controller.ui_state()['ready'])
                app.controller.show_settings()
                until(lambda: app.settings.payload is not None)
                app.settings.name.setStringValue_('test')
                app.settings.endpoint.setStringValue_('http://127.0.0.1:9999/v1/responses')
                app.settings.key_action.selectItemAtIndex_(2)
                app.settings.save_model()
                until(lambda: runtime.model is not None and app.settings.payload['model']['name'] == 'test')
                self.assertIsNone(runtime.model_consent)
                app.controller.show_settings('privacy')
                until(lambda: app.privacy.payload is not None)
                app.controller.review_consent()
                until(lambda: app.privacy.review is not None)
                app.privacy.acknowledge.setState_(1)
                app.privacy.allow()
                until(lambda: runtime.model_consent is not None and runtime.model_consent.allows(runtime.model))
                app.controller.revoke_model()
                until(lambda: not runtime.model_consent.allows(runtime.model))
                self.assertFalse(mac.calls)
                run = runtime.prepare_command(request(runtime.profiles.active['targets'][0]['id'], "print('temporary UI receipt')"))
                runtime.execute_command(run, runtime.agent._proposal_hash(run), acknowledge_unrestricted=True)
                service.refresh()
                app.controller.show_agent_records()
                until(lambda: bool(app.records.rows))
                app.controller.review_receipt(run.id)
                until(lambda: app.receipt.review is not None)
                app.receipt.note.setStringValue_('Reviewed temporary test output and effects.')
                app.receipt.acknowledge.setState_(1)
                app.receipt.decide()
                until(lambda: run.outcome == 'reviewed')
                self.assertFalse(mac.mutations)
                self.assertFalse(run.manual_review['command_effects_verified'])
            finally:
                app.controller.close()
                app.controller.thread.join(6)
                for view in (app.panel, app.review, app.records, app.report, app.profiles, app.settings, app.privacy, app.receipt):
                    view.window.setDelegate_(None)
                    view.window.orderOut_(None)
                service.close()


if __name__ == '__main__':
    unittest.main()
