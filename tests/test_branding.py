"""Public brand changes must not migrate existing storage or security identities."""
import hashlib
from pathlib import Path
import plistlib
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


class BrandingTests(unittest.TestCase):
    def test_public_name_and_existing_storage_identity(self):
        from relay import __app_name__
        from relay.core import default_directory
        from relay.desktop_services import APP_NAME
        from relay.mac_identity import IDENTIFIER
        from relay.mac_helper import HELPER_LABEL
        from relay.commercial.client import KeychainStore
        self.assertEqual(__app_name__, 'NetCare')
        self.assertEqual(APP_NAME, 'NetCare')
        self.assertEqual(default_directory('darwin'), Path.home() / 'Library/Application Support/Relay')
        self.assertEqual(IDENTIFIER, 'com.wangxinlei.relay')
        self.assertEqual(HELPER_LABEL, 'com.wangxinlei.relay.helper')
        origin = 'https://example.test'
        self.assertEqual(KeychainStore(origin).service,
                         'com.relay.account.' + hashlib.sha256(origin.encode()).hexdigest()[:16])

    def test_launch_entry_matches_renamed_executable_but_service_label_is_stable(self):
        with (ROOT / 'apps/macos/com.wangxinlei.relay.agent.plist').open('rb') as source:
            value = plistlib.load(source)
        self.assertEqual(value['BundleProgram'], 'Contents/MacOS/NetCare')
        self.assertEqual(value['ProgramArguments'][0], 'NetCare')
        self.assertEqual(value['Label'], 'com.wangxinlei.relay.agent')
        with (ROOT / 'apps/macos/native/RelayHelper-Info.plist').open('rb') as source:
            value = plistlib.load(source)
        self.assertEqual(value['CFBundleName'], 'NetCare Helper')
        self.assertEqual(value['CFBundleIdentifier'], 'com.wangxinlei.relay.helper')

    def test_public_web_copy_has_no_legacy_brand(self):
        for name in ('backend/templates/index.html', 'backend/static/app.js'):
            text = (ROOT / name).read_text()
            self.assertIn('NetCare', text)
            self.assertNotIn('Relay', text)


if __name__ == '__main__':
    unittest.main()
