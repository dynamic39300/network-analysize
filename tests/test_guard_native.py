"""Opt-in native subscription smoke test; never changes network configuration."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.mac_events import MacNetworkEvents


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1',
                     'opt-in native network subscription')
class NativeGuardTests(unittest.TestCase):
    def test_native_network_subscription_registers_and_stops(self):
        observer = MacNetworkEvents(lambda: None)
        self.addCleanup(observer.close)
        self.assertTrue(observer.start(), observer.error)
        self.assertTrue(observer.available)
        observer.close()
        self.assertFalse(observer.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
