"""Opt-in real Cocoa profile editor tests; controller operations are simulated."""
import copy
from datetime import datetime
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.profiles import normalize_document
from test_profiles import document

ROOT = Path(__file__).resolve().parents[1]


class ProfileController:
    def __init__(self):
        self.state = {'ready': True, 'busy': '', 'agent': {}}
        self.calls = []

    def ui_state(self):
        return copy.deepcopy(self.state)

    def change_profile(self, operation, **values):
        self.calls.append((operation, values))
        return True


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native UI checks')
class NativeProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from AppKit import NSApplication
        cls.app = NSApplication.sharedApplication()

    def setUp(self):
        from relay.profiles_window import ProfilesWindow
        self.controller = ProfileController()
        self.editor = ProfilesWindow(self.controller)
        self.profile = {**normalize_document(document()), 'id': 'profile-1', 'revision': 2, 'source': 'user'}
        self.assessment = {'id': 'profile-1', 'name': self.profile['name'], 'revision': 2, 'health': 'healthy',
                           'covered': 1, 'total': 1, 'last_verified': datetime(2026, 9, 27, 10, 15).timestamp(), 'targets': [
                               {'id': self.profile['targets'][0]['id'], 'state': 'healthy', 'label': '符合预期',
                                'summary': '访问路径与成功条件均符合预期'}]}
        self.payload = {'profiles': [self.profile], 'active_id': 'profile-1', 'assessment': self.assessment}
        self.editor.show(self.payload)
        self.addCleanup(self.close)

    def close(self):
        self.editor.window.setDelegate_(None)
        self.editor.window.orderOut_(None)

    def capture(self, name):
        from AppKit import NSBitmapImageFileTypePNG
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        self.editor.window.displayIfNeeded()
        view = self.editor.root
        bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
        output = ROOT / 'build/ui-preview' / (name + '.png')
        output.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(output), True))

    def assert_layout(self):
        for parent in (self.editor.root, self.editor.document):
            for child in parent.subviews():
                frame = child.frame()
                self.assertGreaterEqual(frame.origin.x, 0)
                self.assertGreaterEqual(frame.origin.y, -1)
                self.assertLessEqual(frame.origin.x + frame.size.width, parent.bounds().size.width + 1)
                self.assertLessEqual(frame.origin.y + frame.size.height, parent.bounds().size.height + 1)

    def test_current_profile_and_controls_render_light_dark_and_narrow(self):
        from AppKit import NSAppearance
        self.assertFalse(self.editor.dirty)
        self.assertEqual(self.editor.profile_id, 'profile-1')
        self.assertIn('用户确认', self.editor.status_label.stringValue())
        self.assertIn('最近验证', self.editor.status_label.stringValue())
        self.assertIn('符合预期', self.editor.rows[0]['result'].stringValue())
        self.assertFalse(self.editor.remove_button.isEnabled())
        for appearance, name in [('NSAppearanceNameAqua', 'profiles-light'), ('NSAppearanceNameDarkAqua', 'profiles-dark')]:
            self.editor.window.setAppearance_(NSAppearance.appearanceNamed_(appearance))
            self.editor.layout()
            self.assert_layout()
            self.capture(name)
        self.editor.window.setContentSize_((760, 560))
        self.editor.layout()
        self.assert_layout()
        self.capture('profiles-narrow')

    def test_save_passes_revision_and_every_field_without_system_commands(self):
        row = self.editor.rows[0]
        row['name'].setStringValue_('代码仓库')
        row['url'].setStringValue_('https://code.example.test')
        row['expected_path'].selectItemAtIndex_(3)
        row['when'].selectItemAtIndex_(1)
        row['requirement'].selectItemAtIndex_(1)
        row['timeout'].setStringValue_('12')
        self.editor.actions.edit_(row['expected_path'])
        self.assertNotIn('符合预期', row['result'].stringValue())
        self.assertTrue(self.editor.save_button.isEnabled())
        self.editor.save_button.performClick_(None)
        operation, values = self.controller.calls[-1]
        self.assertEqual(operation, 'save')
        self.assertEqual(values['profile_id'], 'profile-1')
        self.assertEqual(values['revision'], 2)
        target = values['document']['targets'][0]
        self.assertEqual(target['expected_path'], 'vpn')
        self.assertEqual(target['when'], 'vpn_connected')
        self.assertEqual(target['requirement'], 'service')
        self.assertEqual(target['timeout'], 12)

    def test_invalid_timeout_and_url_are_inline_errors(self):
        self.editor.rows[0]['timeout'].setStringValue_('n/a')
        self.editor.mark_dirty()
        self.editor.save_button.performClick_(None)
        self.assertEqual(self.controller.calls, [])
        self.assertIn('超时', self.editor.error_label.stringValue())
        self.editor.rows[0]['timeout'].setStringValue_('8')
        self.editor.rows[0]['url'].setStringValue_('file:///etc/hosts')
        self.editor.save_button.performClick_(None)
        self.assertIn('HTTP(S)', self.editor.error_label.stringValue())
        self.assertEqual(self.controller.calls, [])

    def test_busy_and_state_updates_preserve_unsaved_edits(self):
        self.editor.name_field.setStringValue_('未保存的名称')
        self.editor.mark_dirty()
        self.controller.state.update(busy='守护检查')
        self.editor.update_state(self.controller.ui_state())
        self.assertFalse(self.editor.save_button.isEnabled())
        self.assertFalse(self.editor.name_field.isEnabled())
        self.editor.show(self.payload)
        self.assertEqual(self.editor.name_field.stringValue(), '未保存的名称')
        self.editor.show({**self.payload, 'replace': True})
        self.assertFalse(self.editor.dirty)
        self.assertEqual(self.editor.name_field.stringValue(), self.profile['name'])

    def test_dirty_close_and_selection_can_be_cancelled(self):
        self.editor.mark_dirty()
        with patch.object(self.editor, 'confirm', return_value=False):
            self.assertFalse(self.editor.actions.windowShouldClose_(self.editor.window))
            self.editor.actions.choose_(self.editor.picker)
            self.assertTrue(self.editor.dirty)
        with patch.object(self.editor, 'confirm', return_value=True):
            self.assertTrue(self.editor.actions.windowShouldClose_(self.editor.window))

    def test_duplicate_profile_names_have_distinct_selectable_entries(self):
        profiles = [{**self.profile, 'id': 'profile-' + str(index)} for index in range(1, 4)]
        self.editor.show({**self.payload, 'profiles': profiles})
        self.assertEqual(self.editor.picker.numberOfItems(), 3)
        self.editor.picker.selectItemAtIndex_(2)
        self.editor.actions.choose_(self.editor.picker)
        self.assertEqual(self.editor.profile_id, 'profile-3')
        self.assertTrue(self.editor.activate_button.isEnabled())
        self.editor.activate_button.performClick_(None)
        self.assertEqual(self.controller.calls[-1], ('activate', {'profile_id': 'profile-3'}))

    def test_import_and_duplicate_remain_drafts_until_saved(self):
        self.editor.import_draft(normalize_document(document(name='导入的服务')))
        self.assertTrue(self.editor.imported)
        self.assertIsNone(self.editor.profile_id)
        self.assertEqual(self.controller.calls, [])
        self.editor.actions.save_(None)
        self.assertTrue(self.controller.calls[0][1]['imported'])
        self.editor.actions.duplicate_(None)
        self.assertFalse(self.editor.imported)
        self.assertIsNone(self.editor.profile_id)

    def test_many_long_targets_scroll_and_removal_preserves_other_edits(self):
        self.editor.window.setContentSize_((760, 560))
        self.editor.rows[0]['name'].setStringValue_('较长的公司网络服务名称' * 4)
        self.editor.rows[0]['url'].setStringValue_('https://work.example.test/' + 'long-path/' * 40)
        for _ in range(7):
            self.editor.actions.add_(None)
        self.assertEqual(len(self.editor.rows), 8)
        self.assertGreater(self.editor.document.bounds().size.height, self.editor.scroll.contentSize().height)
        self.assert_layout()
        self.editor.scroll.contentView().scrollToPoint_((0, 0))
        self.editor.scroll.reflectScrolledClipView_(self.editor.scroll.contentView())
        self.capture('profiles-many-targets')
        self.editor.rows[-1]['delete'].performClick_(None)
        self.assertEqual(len(self.editor.rows), 7)
        self.assertIn('较长', self.editor.rows[0]['name'].stringValue())
        self.assertIn('long-path', self.editor.rows[0]['url'].stringValue())


if __name__ == '__main__':
    unittest.main()
