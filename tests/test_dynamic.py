"""Actual temporary-file jobs with FakeMac networking; never alter host network settings."""
import copy
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import selectors
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent_store import AgentStore
from relay.core import CoreRuntime
from relay.dynamic import digest
from relay.jobs import current_identity, executable_identity, job_environment, run_job
from relay.models import ModelConsent, ResponsesModel
from relay.private_files import create_private_directory, execution_slot
from test_diagnostics import FakeMac, config
from test_reasoning import Script
from test_windows import FakeEvents
import relay_core


SUPPORTED = os.name == 'posix' and os.geteuid() != 0


def request(target, source="print('temporary diagnostic')"):
    return {'argv': [sys.executable, '-I', '-c', source], 'stdin': '',
            'purpose': 'Run a temporary diagnostic', 'expected_effect': 'Print a local test result',
            'recovery_notes': 'Inspect the private receipt and temporary files; no automatic undo.',
            'target_ids': [target], 'timeout': 3, 'output_limit': 8192}


@unittest.skipUnless(SUPPORTED, 'Unprivileged POSIX job runner required')
class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'work'
        create_private_directory(self.work)
        executable = executable_identity(sys.executable)
        self.spec = {'argv': [executable['path'], '-I', '-c', "print('hello')"], 'stdin': '',
                     'identity': current_identity(), 'executable': executable, 'cwd': str(self.work),
                     'environment': job_environment(self.work), 'coordination_dir': str(self.root / 'coordination'),
                     'timeout': 2, 'output_limit': 4096, 'receipt_path': str(self.root / 'terminal.json')}

    def tearDown(self):
        self.assertFalse(any(p.name == 'NetCare job supervisor' for p in multiprocessing.active_children()))

    def test_actual_argv_stdin_environment_and_streams(self):
        self.spec['argv'][-1] = "import os,sys,json; print(json.dumps(dict(os.environ))); print(sys.stdin.read()); print('error',file=sys.stderr)"
        self.spec['stdin'] = 'inline content; $(not-a-shell-command)'
        with patch.dict(os.environ, {'RELAY_MODEL_API_KEY': 'private-secret', 'PYTHONSTARTUP': '/private/startup'}):
            result = run_job(self.spec)
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['reason'], 'exited')
        self.assertIn(self.spec['stdin'], result['stdout'])
        self.assertNotIn('private-secret', result['stdout'])
        self.assertNotIn('PYTHONSTARTUP', result['stdout'])
        self.assertIn('error', result['stderr'])
        self.assertTrue(result['terminal_record_saved'])
        record = json.loads(Path(self.spec['receipt_path']).read_text())
        self.assertEqual(record['job_hash'], digest(self.spec))
        self.assertEqual(record['job']['stdout'], result['stdout'])

    def test_timeout_retains_partial_output_and_stops_background_child(self):
        marker = self.root / 'should-not-exist'
        child = f"import time,pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).touch()"
        self.spec['argv'][-1] = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',{child!r}]); print('started',flush=True); time.sleep(10)"
        self.spec['timeout'] = 0.3
        result = run_job(self.spec)
        self.assertEqual(result['reason'], 'timeout')
        self.assertIn('started', result['stdout'])
        time.sleep(2.1)
        self.assertFalse(marker.exists())

    def test_output_is_bounded_and_terminates_the_job(self):
        self.spec['argv'][-1] = "import os;\nwhile True: os.write(1,b'x'*8192)"
        result = run_job(self.spec)
        self.assertEqual(result['reason'], 'output_limit')
        self.assertTrue(result['output_truncated'])
        self.assertEqual(result['captured_bytes'], 4096)
        self.assertEqual(len(result['stdout']), 4096)

    def test_foreground_success_also_stops_ordinary_background_children(self):
        marker = self.root / 'background-write'
        child = f"import time,pathlib; time.sleep(.6); pathlib.Path({str(marker)!r}).touch()"
        self.spec['argv'][-1] = f"import subprocess,sys; subprocess.Popen([sys.executable,'-I','-c',{child!r}]); print('parent finished')"
        result = run_job(self.spec)
        self.assertEqual(result['reason'], 'exited')
        self.assertEqual(result['returncode'], 0)
        time.sleep(.7)
        self.assertFalse(marker.exists())

    def test_cancel_during_job_preserves_a_receipt(self):
        self.spec['argv'][-1] = 'import time; time.sleep(10)'
        stop = threading.Event()
        result = run_job(self.spec, allowed=lambda: not stop.is_set(), on_started=lambda _info: stop.set())
        self.assertEqual(result['reason'], 'cancelled')
        self.assertIsNotNone(result['pid'])

    def test_preflight_exception_never_starts_command_and_releases_coordination(self):
        def fail():
            raise PermissionError('changed')
        with self.assertRaises(PermissionError):
            run_job(self.spec, on_ready=fail)
        with execution_slot(self.spec['coordination_dir']):
            pass

    def test_terminal_record_failure_is_explicit_and_does_not_overwrite_an_existing_file(self):
        path = Path(self.spec['receipt_path'])
        path.write_text('existing receipt')
        result = run_job(self.spec)
        self.assertFalse(result['terminal_record_saved'])
        self.assertEqual(path.read_text(), 'existing receipt')

    def test_other_runtime_write_lock_blocks_launch_before_preflight(self):
        seen = []
        with execution_slot(self.spec['coordination_dir']):
            result = run_job(self.spec, on_ready=lambda: seen.append(True))
        self.assertEqual(result['reason'], 'coordination_unavailable')
        self.assertFalse(seen)
        self.assertIsNone(result['pid'])

    def test_core_process_death_stops_job_and_releases_supervisor_owned_lock(self):
        heartbeat = self.root / 'heartbeat'
        self.spec['timeout'] = 15
        self.spec['argv'][-1] = f"import pathlib,time; p=pathlib.Path({str(heartbeat)!r});\nwhile True: p.write_text(str(time.time())); time.sleep(.05)"
        source = (f"import sys,json; sys.path.insert(0,{str(Path(__file__).resolve().parents[1] / 'code')!r}); "
                  f"from relay.jobs import run_job; run_job({self.spec!r},on_started=lambda info: print(json.dumps(info),flush=True))")
        parent = subprocess.Popen([sys.executable, '-I', '-c', source], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        observer = selectors.DefaultSelector()
        observer.register(parent.stdout, selectors.EVENT_READ)
        try:
            self.assertTrue(observer.select(5), 'Harness did not start its job')
            info = json.loads(parent.stdout.readline())
            deadline = time.monotonic() + 3
            while not heartbeat.exists() and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertTrue(heartbeat.exists())
            parent.kill()
            parent.wait(3)
            released = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    with execution_slot(self.spec['coordination_dir']):
                        released = True
                    break
                except OSError:
                    time.sleep(.025)
            self.assertTrue(released)
            with self.assertRaises(ProcessLookupError):
                os.kill(info['pid'], 0)
            stamp = heartbeat.read_text()
            time.sleep(.15)
            self.assertEqual(heartbeat.read_text(), stamp)
            terminal = json.loads(Path(self.spec['receipt_path']).read_text())
            self.assertEqual(terminal['job']['pid'], info['pid'])
            self.assertEqual(terminal['job']['reason'], 'cancelled')
            self.assertEqual(terminal['job_hash'], digest(self.spec))
        finally:
            observer.close()
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)


@unittest.skipUnless(SUPPORTED, 'Unprivileged POSIX job runner required')
class DynamicAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mac = FakeMac()
        self.core = CoreRuntime(self.root / 'core', platform='darwin', runner=self.mac,
            config=config(self.mac, company=True), events_factory=FakeEvents, guard_threaded=False,
            execution_directory=self.root / 'coordination')
        self.addCleanup(self.core.close)
        self.target = self.core.profiles.active['targets'][0]['id']

    def plan(self, source="print('temporary diagnostic')"):
        return self.core.prepare_command(request(self.target, source))

    def execute(self, run):
        return self.core.execute_command(run, self.core.agent._proposal_hash(run), acknowledge_unrestricted=True)

    def test_exact_plan_requires_separate_risk_acknowledgment_and_never_self_authorizes(self):
        run = self.plan()
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertIsNone(run.grant)
        with self.assertRaises(PermissionError):
            self.core.agent.authorize(run)
        with self.assertRaises(PermissionError):
            self.core.execute_command(run, 'different', acknowledge_unrestricted=True)
        self.assertFalse(self.mac.mutations)

    def test_invalid_arguments_external_authority_or_targets_cannot_prepare_a_job(self):
        original = request(self.target)
        variants = [dict(original, grant='approved'), dict(original, target_ids=['forged']),
                    dict(original, argv=['python', '-c', 'print(1)']), dict(original, timeout=True),
                    dict(original, recovery_notes=''), dict(original, timeout=999)]
        for document in variants:
            with self.subTest(document=document), self.assertRaises((ValueError, ValidationError)):
                self.core.prepare_command(document)
        self.assertFalse(self.mac.mutations)

    def test_real_job_has_before_evidence_exact_receipt_and_manual_not_automatic_review(self):
        marker = self.root / 'job-result'
        run = self.plan(f"from pathlib import Path; Path({str(marker)!r}).write_text('done'); print('private-output')")
        run, report = self.execute(run)
        self.assertEqual(marker.read_text(), 'done')
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertEqual(run.receipt['job']['returncode'], 0)
        self.assertIn('before_snapshot', run.receipt['recovery'])
        self.assertFalse(run.receipt['verification']['command_effects_verified'])
        self.assertIsNone(report['network_writes'])
        self.assertNotIn('private-output', json.dumps(report))
        stored = self.core.store.get(run.id)
        self.assertEqual(stored['receipt']['job_hash'], digest(stored['receipt']['spec']))
        with self.assertRaises(PermissionError):
            self.plan()
        with self.assertRaises(PermissionError):
            self.core.review_command(run.id, 'forged', 'Inspected')
        reviewed = self.core.review_command(run.id, digest(stored['receipt']), 'Inspected temporary file and diagnostic evidence.')
        self.assertEqual(reviewed['outcome'], 'reviewed')
        self.assertFalse(reviewed['command_effects_verified'])
        self.assertFalse(self.core.store.needs_reconciliation())
        self.assertFalse(self.mac.mutations)

    def test_plan_and_script_tamper_expiry_forgery_and_replay_are_rejected(self):
        run = self.plan()
        grant = self.core.agent.authorize(run, acknowledge_unrestricted=True)
        with self.assertRaises(PermissionError):
            self.core.agent.execute(run, replace(grant, id='forged'))
        run.proposals[0].actions[0]['spec']['stdin'] = 'changed'
        with self.assertRaises(PermissionError):
            self.core.agent.execute(run, grant)
        run = self.plan()
        grant = self.core.agent.authorize(run, acknowledge_unrestricted=True)
        with patch.object(self.core.agent, 'clock', return_value=grant.expires_at + 1), self.assertRaises(PermissionError):
            self.core.agent.execute(run, grant)
        self.core.agent.execute(run, grant)
        with self.assertRaises(PermissionError):
            self.core.agent.execute(run, grant)

    def test_configuration_change_after_confirmation_blocks_launch(self):
        marker = self.root / 'not-written'
        run = self.plan(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.mac.manual_dns = ['203.0.113.53']
        run, _ = self.execute(run)
        self.assertEqual(run.outcome, 'not_started')
        self.assertFalse(marker.exists())

    def test_guarded_target_regression_is_recorded_without_automatic_undo(self):
        run = self.plan()
        actual_runner = self.core.agent.dynamic.runner
        def deteriorate(spec, **kwargs):
            result = actual_runner(spec, **kwargs)
            self.mac.curl_exit = 28
            return result
        self.core.agent.dynamic.runner = deteriorate
        self.execute(run)
        self.assertTrue(run.receipt['verification']['protected_target_regressions'])
        self.assertEqual(run.stage, 'needs_reconciliation')
        self.assertFalse(self.mac.mutations)

    def test_close_during_job_stops_it_before_releasing_the_core_journal(self):
        run = self.plan('import time; time.sleep(10)')
        started = threading.Event()
        runner = self.core.agent.dynamic.runner
        def observe_start(spec, **kwargs):
            callback = kwargs['on_started']
            def on_started(info):
                callback(info)
                started.set()
            return runner(spec, **{**kwargs, 'on_started': on_started})
        self.core.agent.dynamic.runner = observe_start
        errors = []
        def execute():
            try:
                self.execute(run)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(OSError):
                AgentStore(self.root / 'core' / 'agent')
            other_mac = FakeMac()
            other = CoreRuntime(self.root / 'other-core', platform='darwin', runner=other_mac,
                config=config(other_mac, company=True), events_factory=FakeEvents, guard_threaded=False,
                execution_directory=self.root / 'coordination')
            try:
                proposal = other.agent.propose(['dns_mixed_on_vpn'])
                grant = other.agent.authorize(proposal)
                with self.assertRaises(OSError):
                    other.agent.execute(proposal, grant)
                self.assertFalse(other_mac.mutations)
            finally:
                other.close()
            self.core.close()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(run.receipt['job']['reason'], 'cancelled')
            self.assertEqual(run.stage, 'needs_reconciliation')
            reopened = AgentStore(self.root / 'core' / 'agent')
            self.assertTrue(reopened.needs_reconciliation())
            reopened.close()
        finally:
            self.core.close()
            worker.join(5)

    def test_executable_content_change_blocks_launch(self):
        program = self.root / 'test-script'
        program.write_text('#!/bin/sh\nprintf first\n')
        program.chmod(0o700)
        document = request(self.target)
        document['argv'] = [str(program)]
        run = self.core.prepare_command(document)
        program.write_text('#!/bin/sh\nprintf changed\n')
        self.execute(run)
        self.assertEqual(run.outcome, 'not_started')

    def test_journal_failure_before_launch_prevents_temporary_write(self):
        marker = self.root / 'not-written'
        run = self.plan(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        grant = self.core.agent.authorize(run, acknowledge_unrestricted=True)
        with patch.object(self.core.store, 'save', side_effect=OSError('full')):
            self.core.agent.execute(run, grant)
        self.assertFalse(marker.exists())
        self.assertEqual(run.outcome, 'not_started')

    def test_revocation_during_actual_execution_stops_job_but_keeps_review_required(self):
        run = self.plan('import time; print("partial",flush=True); time.sleep(10)')
        grant = self.core.agent.authorize(run, acknowledge_unrestricted=True)
        actual_runner = self.core.agent.dynamic.runner
        def revoke_when_started(spec, **kwargs):
            callback = kwargs['on_started']
            def started(info):
                callback(info)
                self.core.agent.revoke(grant)
            return actual_runner(spec, **{**kwargs, 'on_started': started})
        self.core.agent.dynamic.runner = revoke_when_started
        self.core.agent.execute(run, grant)
        self.assertEqual(run.receipt['job']['reason'], 'cancelled')
        self.assertEqual(run.stage, 'needs_reconciliation')

    def test_restart_does_not_replay_or_claim_unknown_command_effects_recovered(self):
        run = self.plan()
        self.execute(run)
        before = len(self.mac.mutations)
        self.core.close()
        reopened = CoreRuntime(self.root / 'core', platform='darwin', runner=self.mac,
            config=config(self.mac, company=True), events_factory=FakeEvents, guard_threaded=False,
            execution_directory=self.root / 'coordination')
        self.addCleanup(reopened.close)
        reopened.check()
        self.assertTrue(reopened.store.needs_reconciliation())
        self.assertEqual(reopened.store.get(run.id)['reconciliation']['outcome'], 'needs_attention')
        self.assertEqual(len(self.mac.mutations), before)

    def test_terminal_receipt_recovers_after_lost_core_result_without_clearing_review(self):
        run = self.plan("print('retained after core interruption')")
        self.execute(run)
        run.receipt['job'] = {'state': 'running'}
        self.core.agent._save(run, required=True)
        record = self.core.command_record(run.id)
        self.assertIn('retained after core interruption', record['receipt']['job']['stdout'])
        self.assertEqual(record['stage'], 'needs_reconciliation')
        self.assertFalse(record['receipt']['verification']['command_effects_verified'])

    def test_corrupt_or_mismatched_terminal_receipt_cannot_change_existing_audit(self):
        run = self.plan()
        self.execute(run)
        existing = self.core.store.get(run.id)['receipt']['job']
        path = Path(run.receipt['spec']['receipt_path'])
        for raw in ('{"partial":', json.dumps({'schema': 'relay-job-receipt-v1', 'job_hash': 'forged', 'job': {}})):
            path.write_text(raw)
            with self.assertRaises(ValueError):
                self.core.agent.dynamic.inspect_receipt(run.receipt)
            self.assertEqual(self.core.command_record(run.id)['receipt']['job'], existing)

    def test_model_can_prepare_dynamic_content_but_never_execute_or_get_authority(self):
        document = request(self.target)
        document['targets'] = ['t1']
        document.pop('target_ids')
        script = Script([('propose_command', document)])
        model = ResponsesModel('test', 'http://127.0.0.1:9999/responses', transport=script)
        self.core.model, self.core.model_consent = model, ModelConsent(model)
        run, report = self.core.investigate()
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertEqual(run.proposals[0].actions[0]['origin'], 'model')
        self.assertEqual(run.proposals[0].actions[0]['spec']['argv'][1:], document['argv'][1:])
        self.assertIsNone(run.grant)
        self.assertIsNone(run.receipt)
        self.assertEqual(report['network_writes'], 0)
        self.assertFalse(self.mac.mutations)


@unittest.skipUnless(SUPPORTED, 'Unprivileged POSIX job runner required')
class DynamicCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mac = FakeMac()
        self.created = []
        core = self.factory(data_dir=self.root / 'core')
        self.document = request(core.profiles.active['targets'][0]['id'],
                                f"from pathlib import Path; Path({str(self.root / 'result')!r}).touch()")
        core.close()
        self.file = self.root / 'command.json'
        self.file.write_text(json.dumps(self.document))

    def factory(self, **kwargs):
        core = CoreRuntime(platform='darwin', runner=self.mac, config=config(self.mac, company=True),
            events_factory=FakeEvents, guard_threaded=False, execution_directory=self.root / 'coordination', **kwargs)
        self.created.append(core)
        self.addCleanup(core.close)
        return core

    def call(self, *arguments):
        out = io.StringIO()
        with redirect_stdout(out):
            result = relay_core.main(['--data-dir', str(self.root / 'core'), *arguments], runtime_factory=self.factory)
        return result, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_command_document_prepares_full_review_but_does_not_execute_by_default(self):
        code, lines = self.call('command', str(self.file))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / 'result').exists())
        action = lines[0]['command_review']['proposal']['actions'][0]
        self.assertEqual(action['spec']['argv'][1:], self.document['argv'][1:])
        self.assertFalse(lines[0]['command_review']['executed'])

    def test_piped_input_or_declined_confirmation_never_executes(self):
        code, _ = self.call('command', str(self.file), '--interactive')
        self.assertEqual(code, 1)
        self.assertFalse((self.root / 'result').exists())
        with patch.object(relay_core, 'confirm_exact', return_value=False):
            code, _ = self.call('command', str(self.file), '--interactive')
        self.assertEqual(code, 2)
        self.assertFalse((self.root / 'result').exists())

    def test_explicit_confirmation_executes_once_and_review_never_reexecutes(self):
        tokens = []
        def confirm(token, _warning):
            tokens.append(token)
            return True
        with patch.object(relay_core, 'confirm_exact', side_effect=confirm):
            code, lines = self.call('command', str(self.file), '--interactive')
        self.assertEqual(code, 2)
        self.assertTrue((self.root / 'result').exists())
        self.assertEqual(tokens[0], 'EXECUTE ' + lines[0]['command_review']['proposal_hash'])
        run_id = lines[0]['command_review']['run_id']
        code, lines = self.call('review-command', run_id)
        self.assertEqual(code, 2)
        receipt = copy.deepcopy(lines[0]['receipt_review']['receipt'])
        with patch.object(relay_core, 'confirm_exact', side_effect=confirm), patch('builtins.input', return_value='Reviewed test output.'):
            code, lines = self.call('review-command', run_id, '--interactive')
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]['outcome'], 'reviewed')
        self.assertFalse(lines[-1]['command_effects_verified'])
        self.assertEqual(lines[0]['receipt_review']['receipt'], receipt)
        self.assertFalse(self.mac.mutations)


if __name__ == '__main__':
    unittest.main()
