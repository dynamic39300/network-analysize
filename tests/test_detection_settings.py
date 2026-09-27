"""Core-owned detection settings preserve policy, authorization and external edits."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.core import CoreRuntime
from relay.core_service import CoreService
from relay.ipc import LocalClient, RpcError
from test_diagnostics import FakeMac, config
from test_windows import FakeEvents


@unittest.skipUnless(sys.platform == 'darwin', 'macOS detection configuration')
class DetectionSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.mac = FakeMac()
        cfg = config(self.mac, company=True)
        cfg.config_path = str(self.data / 'config.json')
        cfg.save()
        self.runtime = CoreRuntime(self.data, config=cfg, platform='darwin', runner=self.mac,
            events_factory=FakeEvents, guard_threaded=False, execution_directory=self.data / 'execution')
        self.service = CoreService(self.runtime)
        self.service.start()
        self.addCleanup(self.service.close)
        self.client = LocalClient(self.data)

    def wait(self, operation):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.client.call('operation', {'id': operation['id']})
            if result['state'] not in ('accepted', 'running') and not self.client.call('status')['busy']:
                return result
            time.sleep(0.005)
        self.fail('Configuration operation did not settle')

    def revision(self):
        return self.client.call('settings')['detection']['revision']

    def test_preset_change_is_core_owned_no_probe_and_clears_old_observation(self):
        self.runtime.check()
        self.service.refresh()
        before = len(self.mac.calls)
        binding = self.runtime.profiles.binding()
        done = self.wait(self.client.call('detection_preset', {'revision': self.revision(), 'name': 'minimal'}))
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(len(self.mac.calls), before)
        self.assertFalse(self.mac.mutations)
        state = self.client.call('status')
        self.assertEqual(state['detection']['preset'], 'minimal')
        self.assertIsNone(state['snapshot']['last_check'])
        self.assertNotIn('ipv6', state['enabled_checks'])
        self.assertEqual(self.runtime.profiles.binding(), binding)
        self.assertFalse(state['guard']['enabled'])

    def test_pending_proposal_and_arbitrary_config_fields_cannot_cross_gate(self):
        revision = self.revision()
        with self.assertRaises(RpcError):
            self.client.call('detection_preset', {'revision': revision, 'name': 'company', 'auto_fix': True})
        self.runtime.propose()
        self.service.refresh()
        with self.assertRaises(RpcError) as error:
            self.client.call('detection_preset', {'revision': revision, 'name': 'observe'})
        self.assertEqual(error.exception.reason, 'pending_proposal')
        self.assertFalse(self.mac.mutations)

    def test_stale_revision_and_external_edits_are_not_overwritten(self):
        stale = self.revision()
        self.wait(self.client.call('detection_preset', {'revision': stale, 'name': 'observe'}))
        self.assertEqual(self.wait(self.client.call('detection_preset', {'revision': stale, 'name': 'minimal'}))['state'], 'failed')
        path = self.data / 'config.json'
        external = json.loads(path.read_text())
        external['vpn']['company_dns'] = ['10.0.0.77']
        path.write_text(json.dumps(external))
        done = self.wait(self.client.call('detection_preset', {'revision': self.revision(), 'name': 'minimal'}))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(json.loads(path.read_text()), external)
        self.assertEqual(self.runtime.config.get('general.preset'), 'observe')

    def test_redetect_preserves_health_profile_and_explicit_company_policy(self):
        profiles = copy.deepcopy(self.runtime.profiles.profiles)
        done = self.wait(self.client.call('detection_redetect', {'revision': self.revision()}))
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(self.runtime.profiles.profiles, profiles)
        self.assertTrue(self.runtime.config.get('dns.enforce_company_dns_on_vpn'))
        self.assertEqual(self.runtime.config.get('vpn.company_dns'), ['10.0.0.66', '10.0.0.68'])
        self.assertIsNotNone(self.client.call('status')['snapshot']['last_check'])
        self.assertFalse(self.mac.mutations)

    def test_save_failure_keeps_memory_disk_and_restores_previous_guard_choice(self):
        old = copy.deepcopy(self.runtime.config.config)
        self.runtime.start_guard()
        with patch.object(type(self.runtime.config), 'save', side_effect=OSError('disk full')):
            result = self.wait(self.client.call('detection_preset', {'revision': self.revision(), 'name': 'minimal'}))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.runtime.config.config, old)
        self.assertEqual(json.loads((self.data / 'config.json').read_text()), old)
        self.assertTrue(self.runtime.guard.schedule.enabled)

    def test_reconfiguration_suspends_existing_scope_but_does_not_clear_model_consent(self):
        model = self.runtime.configure_model('test', 'http://127.0.0.1:9999/v1/responses', '',
                                             'forget', self.runtime.model_revision)
        self.runtime.grant_model_consent(model['revision'], model['binding'])
        run, _ = self.runtime.propose()
        offer = self.runtime.trust.offer(run)
        self.runtime.confirm_trust(run, self.runtime.agent._proposal_hash(run), 'continuous', offer['scope_hash'],
                                   self.runtime.trust.revision, 'test-user')
        self.service.refresh()
        count = len(self.mac.mutations)
        self.assertEqual(self.runtime.trust.status()['grant']['state'], 'active')
        done = self.wait(self.client.call('detection_preset', {'revision': self.revision(), 'name': 'observe'}))
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(self.runtime.trust.status()['grant']['state'], 'suspended')
        self.assertEqual(self.runtime.trust.status()['grant']['reason'], 'permissions_changed')
        self.assertTrue(self.runtime.model_consent.allows(self.runtime.model))
        self.assertEqual(len(self.mac.mutations), count)

    def test_cancel_during_detection_prevents_configuration_commit_and_guard_restart(self):
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.config.runner
        def held(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        self.runtime.config.runner = held
        old = (self.data / 'config.json').read_bytes()
        self.runtime.start_guard()
        operation = self.client.call('detection_redetect', {'revision': self.revision()})
        self.assertTrue(entered.wait(2))
        self.client.call('cancel')
        release.set()
        self.assertEqual(self.wait(operation)['state'], 'failed')
        self.assertEqual((self.data / 'config.json').read_bytes(), old)
        self.assertFalse(self.runtime.guard.schedule.enabled)
        self.assertFalse(self.mac.mutations)


if __name__ == '__main__':
    unittest.main()
