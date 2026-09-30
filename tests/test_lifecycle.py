"""Lifecycle boundaries: real private sockets/processes, fake macOS network/service APIs."""
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
from relay.agent_store import AgentStore
from relay.core import CoreRuntime
from relay.core_service import CoreService
from relay.ipc import LocalClient, RpcError
from relay.lifecycle import CoreLifecycle, core_command, core_environment, inhibited, read_private_json, set_inhibited, write_private_json
from relay.mac_service import LABEL, MacBackgroundService, MacPrivilegedService
from test_diagnostics import FakeMac, config
from test_windows import FakeEvents


def wait_for(condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError('Condition did not settle')


class FakeService:
    def __init__(self, state='not_registered'):
        self.state, self.calls = state, []

    def status(self):
        return {'available': True, 'state': self.state}

    def register(self):
        self.calls.append('register')
        self.state = 'requires_approval'

    def unregister(self):
        self.calls.append('unregister')
        self.state = 'not_registered'

    def start(self):
        self.calls.append('start')

    def open_settings(self):
        self.calls.append('settings')


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged POSIX lifecycle')
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'core'
        self.data.mkdir(mode=0o700)
        self.manager = CoreLifecycle(self.data, command=lambda path, token: [sys.executable,
            str(ROOT / 'tests/lifecycle_fixture.py'), str(path), token], timeout=8)
        self.addCleanup(self.close_child)

    def close_child(self):
        child = self.manager.child
        if child and child.poll() is None:
            try:
                self.manager.stop(timeout=5)
            finally:
                if child.poll() is None:
                    child.terminate()  # Only our disposable fixture, never a discovered process.
                    child.wait(timeout=10)

    def test_start_reconnect_stop_and_explicit_restart_no_network_probes(self):
        first = self.manager.start()
        child = self.manager.child
        self.assertEqual(first['launch_id'], child.args[-1])
        self.assertFalse(first['guard']['enabled'])
        self.assertEqual(self.manager.start()['core_instance'], first['core_instance'])
        other = CoreLifecycle(self.data, spawn=Mock(side_effect=AssertionError('duplicate')))
        self.assertEqual(other.start()['core_instance'], first['core_instance'])
        receipt = self.manager.stop(timeout=8)
        self.assertEqual(receipt['state'], 'stopped')
        self.assertEqual(child.returncode, 0)
        self.assertEqual(json.loads((self.data / 'fixture-result.json').read_text()), {'commands': [], 'writes': []})
        self.assertEqual(self.manager.status()['core'], 'stopped')
        self.assertTrue(inhibited(self.data))
        self.assertIsNone(self.manager.start())
        second = self.manager.start(explicit=True)
        self.assertNotEqual(second['core_instance'], first['core_instance'])

    def test_explicit_guard_intent_restores_but_stop_clears_it(self):
        self.manager.start()
        client = LocalClient(self.data)
        operation = client.call('guard', {'enabled': True})
        wait_for(lambda: client.call('operation', {'id': operation['id']})['state'] == 'completed')
        self.manager.stop(purpose='service_change', timeout=8)
        state = self.manager.start(explicit=True)
        self.assertTrue(state['guard']['enabled'])
        self.assertFalse(state['model']['consented'])
        self.assertIsNone(state['trust']['grant'])
        self.manager.stop(timeout=8)
        self.assertFalse(self.manager.start(explicit=True)['guard']['enabled'])

    def test_second_start_and_stop_refuse_unconnected_legacy_owner(self):
        store = AgentStore(self.data / 'agent')
        self.addCleanup(store.close)
        self.manager.spawn = Mock(side_effect=AssertionError('must not spawn'))
        self.assertEqual(self.manager.status()['core'], 'unavailable')
        with self.assertRaises(RuntimeError):
            self.manager.start(explicit=True)
        with self.assertRaises(RuntimeError):
            self.manager.stop(timeout=0.1)
        self.manager.spawn.assert_not_called()

    def test_uninstall_never_deletes_data_or_app_and_unregisters_after_stopping(self):
        self.manager.start()
        service = FakeService('enabled')
        self.manager.service = service
        original = service.unregister
        def unregister():
            self.assertTrue(self.manager.owners_idle())
            self.assertEqual(read_private_json(self.data / 'lifecycle/stopped.json')['state'], 'stopped')
            original()
        service.unregister = unregister
        result = self.manager.prepare_uninstall()
        self.assertTrue(result['ready_to_remove_app'])
        self.assertTrue(result['data_preserved'])
        self.assertTrue((self.data / 'agent').is_dir())
        self.assertEqual(service.calls, ['unregister'])
        self.assertTrue(inhibited(self.data))

    def test_background_requires_explicit_approval_and_never_falls_back_to_spawn(self):
        service = FakeService()
        self.manager.service = service
        self.assertEqual(self.manager.background(True)['state'], 'requires_approval')
        self.manager.spawn = Mock()
        with self.assertRaises(PermissionError):
            self.manager.start(explicit=True)
        self.manager.spawn.assert_not_called()
        self.assertEqual(self.manager.background(False)['state'], 'not_registered')
        self.assertTrue(inhibited(self.data))

    def test_helper_registration_is_separate_and_stops_core_before_change(self):
        self.manager.start()
        self.manager.helper, self.manager.service = FakeService(), FakeService()
        def register():
            self.assertTrue(self.manager.owners_idle())
            self.assertTrue(inhibited(self.data))
            self.manager.helper.state = 'requires_approval'
        with patch.object(self.manager.helper, 'register', side_effect=register):
            self.assertEqual(self.manager.privileged(True)['state'], 'requires_approval')
        self.assertEqual(self.manager.status()['helper']['state'], 'requires_approval')
        self.assertFalse(self.manager.service.calls)
        self.assertTrue(inhibited(self.data))

    def test_helper_drain_failure_blocks_uninstall_and_preserves_both_registrations(self):
        self.manager.helper, self.manager.service = FakeService('enabled'), FakeService('enabled')
        with patch.object(self.manager.helper, 'unregister', side_effect=TimeoutError('unsettled')):
            with self.assertRaises(TimeoutError):
                self.manager.prepare_uninstall()
        self.assertFalse(self.manager.service.calls)
        self.assertEqual(self.manager.helper.state, 'enabled')
        self.assertTrue(self.data.exists())

    def test_uninstall_removes_helper_before_user_service(self):
        self.manager.helper, self.manager.service = FakeService('enabled'), FakeService('enabled')
        original = self.manager.service.unregister
        def unregister():
            self.assertEqual(self.manager.helper.calls, ['unregister'])
            original()
        with patch.object(self.manager.service, 'unregister', side_effect=unregister):
            self.assertTrue(self.manager.prepare_uninstall()['ready_to_remove_app'])

    def test_helper_invalid_or_unavailable_changes_do_not_stop_core(self):
        with patch.object(self.manager, '_stop') as stop:
            for value in (None, 'enable', 1):
                with self.assertRaises(ValueError):
                    self.manager.privileged(value)
            with self.assertRaises(NotImplementedError):
                self.manager.privileged(True)
            stop.assert_not_called()

    def test_registration_failure_and_unconfirmed_removal_never_claim_success(self):
        self.manager.service = FakeService()
        with patch.object(self.manager.service, 'register', side_effect=OSError('denied')):
            with self.assertRaises(OSError):
                self.manager.background(True)
        self.assertTrue(inhibited(self.data))
        self.manager.service.state = 'enabled'
        with patch.object(self.manager.service, 'unregister'):
            with self.assertRaises(RuntimeError):
                self.manager.prepare_uninstall()
            with self.assertRaises(RuntimeError):
                self.manager.background(False)

    def test_reenable_already_registered_service_restarts_cleanly_stopped_job(self):
        self.manager.service = FakeService('enabled')
        with patch.object(self.manager.service, 'register'):
            state = self.manager.background(True)
        self.assertEqual(state['state'], 'enabled')
        self.assertEqual(self.manager.service.calls, ['start'])
        self.assertFalse(inhibited(self.data))

    def test_unavailable_service_verification_never_claims_ready_to_remove_app(self):
        with self.assertRaises(NotImplementedError):
            self.manager.prepare_uninstall()
        self.assertTrue(inhibited(self.data))
        self.assertTrue(self.data.is_dir())

    def test_lost_shutdown_ack_is_not_replayed_and_exact_receipt_is_accepted(self):
        self.manager.start()
        original = LocalClient.call
        calls = []
        def lost(client, method, *args, **kwargs):
            result = original(client, method, *args, **kwargs)
            if method == 'shutdown':
                calls.append(kwargs['request_id'])
                raise EOFError('response lost')
            return result
        with patch.object(LocalClient, 'call', lost):
            result = self.manager.stop(timeout=8)
        self.assertEqual(calls, [result['request_id']])
        self.assertEqual(result['state'], 'stopped')

    def test_invalid_and_nonprivate_lifecycle_control_fails_closed(self):
        self.assertFalse(inhibited(self.data))
        set_inhibited(self.data, True)
        path = self.data / 'lifecycle/control.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        write_private_json(path, {'version': 1, 'inhibited': 'false'})
        with self.assertRaises(ValueError):
            self.manager.start()
        set_inhibited(self.data, False)
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.manager.start()
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(self.data / 'not-there')
        with self.assertRaises(PermissionError):
            self.manager.start()

    def test_stale_receipt_and_timeout_never_unregister_or_kill(self):
        self.manager.service = FakeService('enabled')
        write_private_json(self.data / 'lifecycle/stopped.json', {'state': 'stopped', 'core_instance': 'old', 'request_id': 'old'})
        self.manager.pending_stop = ('current', 'current-request')
        with patch.object(self.manager, 'connected', return_value={'core_instance': 'current'}), \
             patch.object(self.manager, 'owners_idle', return_value=True):
            with self.assertRaises(TimeoutError):
                self.manager.stop(timeout=0.05)
        self.assertEqual(self.manager.pending_stop, ('current', 'current-request'))
        self.assertFalse(self.manager.service.calls)
        write_private_json(self.data / 'lifecycle/stopped.json', {'state': 'stopped',
            'core_instance': 'current', 'request_id': 'current-request'})
        self.assertEqual(self.manager.stop(timeout=0.1)['state'], 'stopped')

    def test_operation_lock_prevents_overlapping_lifecycle_changes(self):
        other = CoreLifecycle(self.data)
        with self.manager.locked():
            with self.assertRaises(OSError):
                other.start(explicit=True)

    def test_launch_timeout_keeps_owned_child_and_does_not_spawn_again(self):
        child = Mock()
        child.poll.return_value = None
        self.manager.spawn = Mock(return_value=child)
        self.manager.timeout = 0.03
        with self.assertRaises(TimeoutError):
            self.manager.start()
        with self.assertRaises(RuntimeError):
            self.manager.start(explicit=True)
        self.manager.spawn.assert_called_once()
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        self.manager.child = None

    def test_known_command_and_clean_child_environment(self):
        with patch.dict(os.environ, {'RELAY_MODEL_API_KEY': 'secret', 'PYTHONPATH': '/unsafe', 'DYLD_INSERT_LIBRARIES': '/unsafe'}):
            value = core_environment()
        self.assertNotIn('RELAY_MODEL_API_KEY', value)
        self.assertNotIn('PYTHONPATH', value)
        self.assertNotIn('DYLD_INSERT_LIBRARIES', value)
        self.assertEqual(value['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        argv = core_command(self.data, 'a' * 32)
        self.assertEqual(argv[-1], 'a' * 32)
        self.assertIn(str(self.data), argv)
        self.assertEqual(Path(argv[1]), ROOT / 'code/relay_app.py')


@unittest.skipUnless(sys.platform in ('darwin', 'linux') and os.geteuid() != 0, 'unprivileged POSIX lifecycle')
class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'core'
        self.mac = FakeMac()
        self.runtime = CoreRuntime(self.data, platform='darwin', runner=self.mac, config=config(self.mac, company=True),
            events_factory=FakeEvents, guard_threaded=False, execution_directory=Path(self.temp.name) / 'execution')
        self.service = CoreService(self.runtime)
        self.service.start()
        self.addCleanup(self.service.close)
        self.client = LocalClient(self.data)

    def test_shutdown_is_idempotent_draining_and_completion_requires_closed_store(self):
        self.assertIsNone(self.client.call('settings')['persistent_startup'])
        token = uuid.uuid4().hex
        params = {'purpose': 'stop'}
        operation = self.client.call('shutdown', params, request_id=token)
        self.assertEqual(operation['state'], 'accepted')
        self.assertEqual(self.client.call('shutdown', params, request_id=token), operation)
        self.assertTrue(self.client.call('status')['draining'])
        self.assertFalse(self.client.call('status')['ready'])
        with self.assertRaises(RpcError):
            self.client.call('check')
        self.assertFalse((self.data / 'lifecycle/stopped.json').exists())
        self.service.close()
        self.assertIsNone(self.runtime.store._lock_fd)
        receipt = read_private_json(self.data / 'lifecycle/stopped.json')
        self.assertEqual(receipt['request_id'], token)
        store = AgentStore(self.data / 'agent')
        try:
            self.assertEqual(store.ipc_request(token)[1]['state'], 'completed')
        finally:
            store.close()
        self.assertFalse(self.mac.calls)

    def test_stop_during_real_fake_write_waits_for_verified_rollback(self):
        self.runtime.config.config['ipv6']['should_be'] = 'off'
        run, _ = self.runtime.propose()
        self.service.refresh()
        review = self.client.call('review', {'run_id': run.id})
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        old_dns = list(self.mac.manual_dns)
        original = self.runtime.fix._write_field
        def hold(field, value, service):
            original(field, value, service)
            if not entered.is_set():
                entered.set()
                release.wait(5)
        with patch.object(self.runtime.fix, '_write_field', side_effect=hold):
            self.client.call('confirm', {key: review[key] for key in ('run_id', 'proposal_hash', 'review_token')} |
                             {'accept': True, 'unrestricted': False})
            self.assertTrue(entered.wait(5))
            self.client.call('shutdown', {'purpose': 'stop'})
            thread = threading.Thread(target=lambda: (self.service.close(), closed.set()))
            thread.start()
            try:
                self.assertFalse(closed.wait(0.15))
                self.assertFalse((self.data / 'lifecycle/stopped.json').exists())
            finally:
                release.set()
                thread.join(8)
            self.assertTrue(closed.is_set())
        self.assertEqual(self.mac.manual_dns, old_dns)
        self.assertEqual(self.mac.ipv6, 'Automatic')
        self.assertEqual(run.outcome, 'rolled_back')
        self.assertEqual(run.receipt['changes'][0]['after'], old_dns)

    def test_missing_shutdown_journal_still_revokes_work_but_does_not_claim_receipt(self):
        with patch.object(self.runtime.store, 'save_ipc_request', side_effect=OSError('full')):
            with self.assertRaises(RpcError):
                self.client.call('shutdown', {'purpose': 'stop'})
        self.assertTrue(self.service.stop_requested.is_set())
        self.assertFalse(self.runtime.work_allowed())
        self.service.close()
        self.assertFalse((self.data / 'lifecycle/stopped.json').exists())

    def test_receipt_write_failure_does_not_leave_store_owned(self):
        self.client.call('shutdown', {'purpose': 'stop'})
        with patch('relay.lifecycle.write_private_json', side_effect=OSError('full')):
            with self.assertRaises(OSError):
                self.service.close()
        self.assertIsNone(self.runtime.store._lock_fd)
        self.assertFalse((self.data / 'lifecycle/stopped.json').exists())

    def test_normal_close_revokes_before_waiting_for_transport_clients(self):
        original = self.service.server.close
        def close():
            self.assertFalse(self.runtime.work_allowed())
            original()
        with patch.object(self.service.server, 'close', side_effect=close):
            self.service.close()

    def test_invalid_shutdown_parameters_have_no_side_effect(self):
        for params in ({}, {'purpose': 'kill'}, {'purpose': 'stop', 'delete_data': True}):
            with self.assertRaises(RpcError):
                self.client.call('shutdown', params)
        self.assertFalse(self.service.draining)
        self.assertFalse(self.runtime.task_cancelled.is_set())


@unittest.skipUnless(sys.platform == 'darwin', 'macOS service adapter')
class MacServiceTests(unittest.TestCase):
    def helper(self, state):
        helper = MacPrivilegedService('/tmp/unused')
        helper.app, helper.transport = Mock(), Mock()
        helper.app.status.return_value = state
        return helper

    def test_helper_source_status_never_registers_or_connects(self):
        with patch('relay.mac_service.sys.frozen', False, create=True), patch('relay.mac_helper.MacHelperTransport') as transport:
            helper = MacPrivilegedService('/tmp/unused')
            self.assertFalse(helper.status()['available'])
            transport.assert_not_called()
            with self.assertRaises(NotImplementedError):
                helper.register()
            with self.assertRaises(NotImplementedError):
                helper.unregister()

    def test_helper_requires_system_approval_and_only_resumes_after_enabled(self):
        helper = self.helper(2)
        helper.app.registerAndReturnError_.return_value = (True, None)
        helper.register()
        helper.transport.call.assert_not_called()
        helper.app.status.return_value = 1
        helper.transport.call.return_value = {'draining': False}
        helper.register()
        helper.transport.call.assert_called_once_with('resume')
        helper.app.registerAndReturnError_.assert_called_once_with(None)

    def test_helper_unknown_or_active_work_never_unregisters(self):
        helper = self.helper(1)
        helper.transport.call.return_value = {'error': 'batch_unsettled'}
        with self.assertRaises(RuntimeError):
            helper.unregister()
        helper.app.unregisterAndReturnError_.assert_not_called()
        helper.app.status.return_value = 2
        helper.transport.call.side_effect = TimeoutError('approval revoked, activity unknown')
        with self.assertRaises(TimeoutError):
            helper.unregister()
        helper.app.unregisterAndReturnError_.assert_not_called()

    def test_helper_drain_is_required_before_registration_removal(self):
        helper = self.helper(1)
        helper.transport.call.return_value = {'draining': True}
        def unregister(_):
            helper.transport.call.assert_called_once_with('drain')
            helper.app.status.return_value = 0
            return True, None
        helper.app.unregisterAndReturnError_.side_effect = unregister
        helper.unregister()
        self.assertEqual(helper.status()['state'], 'not_registered')
        helper.unregister()
        helper.app.unregisterAndReturnError_.assert_called_once()

    def test_helper_plist_is_fixed_on_demand_native_executable(self):
        value = plistlib.loads((ROOT / 'apps/macos/com.wangxinlei.relay.helper.plist').read_bytes())
        self.assertEqual(value['BundleProgram'], 'Contents/Library/LaunchServices/RelayHelper')
        self.assertEqual(value['MachServices'], {'com.wangxinlei.relay.helper': True})
        self.assertFalse(set(value) & {'ProgramArguments', 'UserName', 'RunAtLoad', 'KeepAlive'})

    def test_source_build_never_registers_or_touches_system_service(self):
        with patch('relay.mac_service.sys.frozen', False, create=True):
            service = MacBackgroundService('/tmp/unused')
        self.assertFalse(service.status()['available'])
        with self.assertRaises(NotImplementedError):
            service.register()
        with self.assertRaises(NotImplementedError):
            service.unregister()

    def test_adhoc_default_directory_uses_temporary_core_without_registration(self):
        from relay.core import default_directory
        with patch('relay.mac_service.sys.platform', 'darwin'), \
                patch('relay.mac_service.sys.frozen', True, create=True), \
                patch('relay.mac_service.platform.mac_ver', return_value=('26.0', (), '')), \
                patch('relay.mac_identity.production_requirement', return_value=None):
            service = MacBackgroundService(default_directory())
        self.assertEqual(service.status(), {'available': False, 'state': 'unavailable',
            'reason': 'signed_bundle_required'})
        with self.assertRaises(NotImplementedError):
            service.register()

    def test_invalid_bundle_identity_does_not_fall_back(self):
        from relay.core import default_directory
        with patch('relay.mac_service.sys.platform', 'darwin'), \
                patch('relay.mac_service.sys.frozen', True, create=True), \
                patch('relay.mac_service.platform.mac_ver', return_value=('26.0', (), '')), \
                patch('relay.mac_identity.production_requirement', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                MacBackgroundService(default_directory())

    def test_bundle_plist_is_user_session_and_contains_no_keepalive_after_clean_exit(self):
        with (ROOT / 'apps/macos' / (LABEL + '.plist')).open('rb') as stream:
            value = plistlib.load(stream)
        self.assertEqual(value['Label'], LABEL)
        self.assertEqual(value['BundleProgram'], 'Contents/MacOS/NetCare')
        self.assertEqual(value['KeepAlive'], {'SuccessfulExit': False})
        self.assertNotIn('UserName', value)
        self.assertEqual(value['MachServices'], {'com.wangxinlei.relay.core': True})
        self.assertEqual(value['ProgramArguments'], ['NetCare', '--core-service', '--managed'])

    def test_framework_status_and_exact_launchctl_do_not_use_force_or_shell(self):
        service = MacBackgroundService('/tmp/unused')
        service.app = Mock()
        for value, name in ((0, 'not_registered'), (1, 'enabled'), (2, 'requires_approval'), (3, 'not_found'), (100, 'unknown')):
            service.app.status.return_value = value
            self.assertEqual(service.status()['state'], name)
        service.app.status.return_value = 1
        with patch('relay.mac_service.subprocess.run') as run:
            service.start()
        self.assertEqual(run.call_args.args[0], ['/bin/launchctl', 'kickstart', f'gui/{os.geteuid()}/{LABEL}'])
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        service.app.status.return_value = 0
        service.app.registerAndReturnError_.return_value = (False, Mock())
        with self.assertRaises(RuntimeError):
            service.register()


if __name__ == '__main__':
    unittest.main()
