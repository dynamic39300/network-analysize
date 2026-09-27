"""Opt-in Windows evidence. Read-only OS calls and private temporary files only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1] / 'code'
sys.path.insert(0, str(CODE))


@unittest.skipUnless(sys.platform == 'win32' and os.getenv('RELAY_WINDOWS_NATIVE_TESTS') == '1',
                     'Requires explicit opt-in on a real Windows host')
class WindowsNativeTests(unittest.TestCase):
    def test_private_dacl_and_cross_process_owner_lock(self):
        from relay.agent_store import AgentStore
        from relay.private_files import windows_files
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent'
            store = AgentStore(path)
            try:
                windows_files().check(path, directory=True)
                windows_files().check(store.path)
                script = ('import sys; sys.path.insert(0, sys.argv[1]); from relay.agent_store import AgentStore\n'
                          'try: store = AgentStore(sys.argv[2])\n'
                          'except OSError: sys.exit(12)\n'
                          'else: store.close(); sys.exit(0)\n')
                result = subprocess.run([sys.executable, '-c', script, str(CODE), str(path)],
                                        timeout=10, capture_output=True)
                self.assertEqual(result.returncode, 12)
            finally:
                store.close()
            restored = AgentStore(path)
            restored.close()

    def test_structured_inventory_and_current_process_proxy_scopes(self):
        from relay.platforms.windows import WindowsObserver, current_scope
        scope = current_scope()
        environment = WindowsObserver(expected_scope=scope).observe()
        self.assertEqual(environment.scope['user_sid'], scope['user_sid'])
        self.assertEqual(environment.scope['session_id'], scope['session_id'])
        self.assertNotIn('interfaces', environment.errors)
        self.assertTrue(environment.interfaces)
        self.assertEqual({proxy['scope'] for proxy in environment.proxies},
                         {'process_user_wininet', 'winhttp_static_default'})

    def test_native_network_notifications_register_and_close(self):
        from relay.platforms.windows_events import WindowsNetworkEvents
        events = WindowsNetworkEvents(lambda: None)
        try:
            self.assertTrue(events.start(), events.error)
            self.assertEqual(len(events.handles), 3)
        finally:
            events.close()
        self.assertFalse(events.handles, events.error)


if __name__ == '__main__':
    unittest.main()
