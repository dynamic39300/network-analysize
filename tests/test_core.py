"""Headless entry, guard ownership, redacted output and existing macOS integration."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent_store import AgentStore
from relay.core import CoreRuntime, default_directory
from relay.profiles import SCHEMA
import relay_core
from test_diagnostics import FakeMac, config
from test_guard import Clock
from test_windows import FakeEvents, FakeWindows, SCOPE, SYSTEM


class CoreCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'core'
        self.windows = FakeWindows()
        self.created = []

    def factory(self, **kwargs):
        runtime = CoreRuntime(platform='win32', runner=self.windows, config=self.windows.config(),
            observer=self.windows.observer, directory=SYSTEM, scope=SCOPE, events_factory=FakeEvents,
            guard_threaded=False, **kwargs)
        self.created.append(runtime)
        self.addCleanup(runtime.close)
        return runtime

    def call(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = relay_core.main(['--data-dir', str(self.data), *args], runtime_factory=self.factory)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_default_check_redacts_and_closes_runtime(self):
        code, lines = self.call('check')
        self.assertEqual(code, 0)
        self.assertTrue(lines[0]['redacted'])
        self.assertNotIn('private.example.test', json.dumps(lines))
        self.assertTrue(self.created[0].closed)
        self.assertIsNone(self.created[0].store._lock_fd)

    def test_raw_is_explicit_and_failure_exit_is_nonzero(self):
        code, lines = self.call('check', '--raw')
        self.assertEqual(code, 0)
        self.assertIn('private.example.test', json.dumps(lines))
        self.windows.exit = 28
        code, lines = self.call('check')
        self.assertEqual(code, 2)
        self.assertEqual(lines[0]['health'], 'degraded')

    def test_successful_http_does_not_return_zero_when_record_cannot_be_saved(self):
        with patch.object(AgentStore, 'save', side_effect=OSError('full')):
            code, lines = self.call('check')
        self.assertEqual(code, 2)
        self.assertEqual(lines[0]['persistence']['journal'], 'unavailable')

    def test_profile_import_activate_export_and_remove_never_probe_network(self):
        code, lines = self.call('profiles', 'list')
        original = lines[0]['active']['id']
        path = Path(self.temp.name) / 'profile.json'
        document = {'schema': SCHEMA, 'name': 'Team service', 'targets': [
            {'name': 'Internal', 'url': 'https://private.example.test', 'expected_path': 'direct', 'requirement': 'service'}]}
        path.write_text(json.dumps(document), encoding='utf-8')
        code, lines = self.call('profiles', 'import', str(path))
        self.assertEqual(code, 0)
        imported = lines[0]['id']
        self.assertNotEqual(original, imported)
        self.assertEqual(lines[0]['network_writes'], 0)
        code, lines = self.call('profiles', 'export', imported)
        self.assertEqual(lines[0]['name'], document['name'])
        self.assertEqual(lines[0]['targets'][0]['expected_path'], 'direct')
        code, _ = self.call('profiles', 'remove', imported)
        self.assertEqual(code, 1)
        code, _ = self.call('profiles', 'activate', original)
        self.assertEqual(code, 0)
        code, _ = self.call('profiles', 'remove', imported)
        self.assertEqual(code, 0)
        self.assertFalse(self.windows.calls)

    def test_import_rejects_authority_fields_and_does_not_replace_profile(self):
        _, before = self.call('profiles', 'list')
        path = Path(self.temp.name) / 'bad.json'
        path.write_text(json.dumps({'schema': SCHEMA, 'name': 'Rejected', 'grant': 'untrusted', 'targets': []}), encoding='utf-8')
        code, _ = self.call('profiles', 'import', str(path))
        self.assertEqual(code, 1)
        _, after = self.call('profiles', 'list')
        self.assertEqual(before, after)
        path.write_text('x' * (256 * 1024 + 1), encoding='utf-8')
        code, _ = self.call('profiles', 'import', str(path))
        self.assertEqual(code, 1)

    def test_watch_foreground_start_and_interrupt_release_store_without_persisting_enablement(self):
        def interrupted():
            raise KeyboardInterrupt()
        with patch.object(relay_core, 'threading', SimpleNamespace(Event=lambda: SimpleNamespace(wait=interrupted))):
            code, lines = self.call('watch')
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]['lifetime'], 'foreground_process')
        self.assertTrue(self.created[0].events.closed)
        self.assertFalse(self.windows.calls)
        self.call('profiles', 'list')
        self.assertFalse(self.created[-1].guard.schedule.enabled)
        self.assertFalse(self.created[-1].events.started)
        store = AgentStore(self.data / 'agent')
        try:
            self.assertFalse(store.core_guard_enabled())
        finally:
            store.close()

    def test_cli_has_no_authorize_execute_or_arbitrary_shell_endpoint(self):
        for command in ('execute', 'authorize', 'shell', 'sudo'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.call(command)
        self.assertFalse(self.created)

    def test_model_option_without_upload_consent_does_not_initialize_runtime(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.call('investigate', '--model', 'test-model')
        self.assertFalse(self.created)
        self.assertFalse(self.windows.calls)

    def test_explicit_model_investigation_uses_environment_key_but_never_prints_it(self):
        from relay.models import ResponsesModel
        from test_reasoning import Script, conclusion
        self.windows.status = '503'
        script = Script([('finish', conclusion('unresolved'))])
        endpoint = 'http://127.0.0.1:9999/v1/responses'
        model = ResponsesModel('test-model', endpoint, api_key='test-secret-key', transport=script)
        with patch.dict('os.environ', {'RELAY_MODEL_API_KEY': 'test-secret-key'}), \
             patch('relay.models.ResponsesModel', return_value=model) as constructor:
            code, lines = self.call('investigate', '--model', 'test-model', '--model-endpoint', endpoint, '--allow-model-upload')
        self.assertEqual(code, 2)
        constructor.assert_called_once_with('test-model', endpoint, 'test-secret-key')
        self.assertEqual(lines[0]['model']['state'], 'complete')
        self.assertEqual(lines[0]['network_writes'], 0)
        self.assertEqual(len(script.requests), 1)
        self.assertNotIn('test-secret-key', json.dumps(lines))

    def test_api_key_alone_does_not_enable_model_or_upload(self):
        with patch.dict('os.environ', {'RELAY_MODEL_API_KEY': 'test-secret-key'}):
            self.call('investigate')
        self.assertIsNone(self.created[0].model)

    @unittest.skipUnless(sys.platform == 'darwin', 'native local transport')
    def test_serve_starts_idle_and_interrupt_releases_core_without_installing_guard(self):
        signal_before = relay_core.signal.getsignal(relay_core.signal.SIGTERM)
        def interrupted():
            raise KeyboardInterrupt()
        with patch.object(relay_core, 'threading', SimpleNamespace(
                Event=lambda: SimpleNamespace(wait=interrupted, set=lambda: None))):
            code, lines = self.call('serve')
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]['schema'], 'relay-local-v1')
        self.assertFalse(lines[0]['guard_enabled'])
        self.assertFalse(self.windows.calls)
        self.assertTrue(self.created[0].closed)
        self.assertEqual(relay_core.signal.getsignal(relay_core.signal.SIGTERM), signal_before)
        self.assertFalse((self.data / 'ipc/connection.json').exists())

    def test_desktop_entry_never_creates_an_inprocess_core_or_probes(self):
        with patch('relay.remote_desktop.run_desktop', return_value=0) as run:
            code, _ = self.call('desktop')
        self.assertEqual(code, 0)
        run.assert_called_once_with(self.data)
        self.assertFalse(self.created)
        self.assertFalse(self.windows.calls)

    def test_initialization_failure_never_leaks_paths_or_hostnames(self):
        def fail(**_kwargs):
            raise OSError('secret/path/private.example.test')
        out = io.StringIO()
        with redirect_stdout(out):
            result = relay_core.main(['check'], runtime_factory=fail)
        self.assertEqual(result, 1)
        self.assertNotIn('secret', out.getvalue())
        self.assertNotIn('private.example.test', out.getvalue())

    def test_default_windows_directory_partitions_user_and_session(self):
        with patch.dict('os.environ', {'LOCALAPPDATA': self.temp.name}):
            one = default_directory('win32', SCOPE)
            another_session = default_directory('win32', {**SCOPE, 'session_id': 3})
            another_user = default_directory('win32', {**SCOPE, 'user_sid': 'S-1-5-21-100-200-300-1002'})
            self.assertEqual(len({one, another_session, another_user}), 3)
            self.assertNotIn(SCOPE['user_sid'], str(one))
        with patch.dict('os.environ', {'LOCALAPPDATA': ''}), self.assertRaises(ValueError):
            default_directory('win32', SCOPE)


class MacHeadlessCoreTests(unittest.TestCase):
    def test_failed_schema_initialization_releases_journal_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent'
            with patch.object(AgentStore, '_initialize', side_effect=OSError('full')), self.assertRaises(OSError):
                AgentStore(path)
            store = AgentStore(path)
            store.close()

    def test_readonly_core_reuses_agent_and_cancels_previous_unexecuted_guard_plan(self):
        mac, clock = FakeMac(), Clock()
        with tempfile.TemporaryDirectory() as directory:
            runtime = CoreRuntime(Path(directory) / 'core', platform='darwin', runner=mac,
                config=config(mac, company=True), events_factory=FakeEvents, clock=clock, guard_threaded=False)
            try:
                run, report = runtime.check()
                self.assertEqual(report['platform'], 'macos')
                self.assertEqual(run.stage, 'observed')
                self.assertFalse(mac.mutations)
                runtime.start_guard()
                clock.advance(5)
                runtime.guard.tick()
                old = runtime._guard_run
                self.assertEqual(old.stage, 'awaiting_authorization')
                clock.advance(300)
                runtime.guard.tick()
                self.assertEqual(old.stage, 'cancelled')
                current = runtime._guard_run
                self.assertEqual(current.stage, 'awaiting_authorization')
                self.assertFalse(mac.mutations)
            finally:
                runtime.close()
            self.assertEqual(current.stage, 'cancelled')
            restored = AgentStore(Path(directory) / 'core/agent')
            try:
                self.assertFalse(any(row['stage'] == 'awaiting_authorization' for row in restored.recent()))
            finally:
                restored.close()

    def test_core_closes_worker_on_exit_and_does_not_start_it_by_loading_configuration(self):
        mac = FakeMac()
        with tempfile.TemporaryDirectory() as directory:
            configuration = config(mac)
            configuration.set('guard.enabled', True)
            runtime = CoreRuntime(Path(directory) / 'core', platform='darwin', runner=mac,
                config=configuration, events_factory=FakeEvents)
            self.assertFalse(runtime.guard.schedule.enabled)
            self.assertFalse(mac.calls)
            runtime.close()
            self.assertFalse(runtime.guard.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
