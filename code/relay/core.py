"""Headless assurance runtime with optional, explicitly consented model investigation."""
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from relay_config import ConfigManager
from .agent import NetworkAssuranceAgent
from .agent_store import AgentStore
from .commands import run_command
from .engine import DetectionEngine, FixEngine
from .guard import GuardPolicy, GuardService, ProbeBudget
from .private_files import create_private_directory
from .profiles import HealthProfiles, ProfileConfig
from .models import EVIDENCE_SCHEMA, ModelConsent, ResponsesModel


def default_directory(platform=None, scope=None):
    platform = platform or sys.platform
    if platform == 'win32':
        if scope is None:
            from .platforms.windows import current_scope
            scope = current_scope()
        local = os.environ.get('LOCALAPPDATA')
        if not local or not Path(local).is_absolute():
            raise ValueError('LOCALAPPDATA is unavailable')
        owner = hashlib.sha256(scope['user_sid'].encode()).hexdigest()[:16]
        return Path(local) / 'Relay' / f"core-{owner}-session-{scope['session_id']}"
    if platform == 'darwin':
        return Path.home() / 'Library/Application Support/Relay'
    raise OSError('NetCare currently supports macOS and Windows only')


def public_report(run, assessment):
    """An explicit export allowlist: no interface names, SID, domains, URLs or raw errors."""
    environment = run.snapshot.get('environment', {})
    targets = assessment.get('profile', {}).get('targets', [])
    return {'schema': 'relay-core-report-v1', 'redacted': True, 'run_id': run.id,
            'captured_at': run.snapshot.get('last_check').isoformat() if run.snapshot.get('last_check') else None,
            'platform': environment.get('platform'), 'health': run.health, 'stage': run.stage,
            'profile_binding': copy.deepcopy(run.snapshot.get('health_profile')),
            'scope': environment.get('scope', {}).get('kind', 'unknown'),
            'interfaces': [{'type': i.get('type') if i.get('type') in
                            ('Ethernet', 'Wireless80211', 'Loopback', 'Ppp', 'Tunnel') else 'other',
                            'state': i.get('state') if i.get('state') in
                            ('Up', 'Down', 'Testing', 'Unknown', 'Dormant', 'NotPresent', 'LowerLayerDown',
                             'ok', 'off', 'observed') else 'unknown',
                            'address_count': len(i.get('addresses', []))} for i in environment.get('interfaces', [])],
            'route_count': len(environment.get('routes', [])),
            'resolver_scope_counts': {scope: sum(r.get('scope') == scope for r in environment.get('resolvers', []))
                                      for scope in ('interface', 'namespace', 'legacy_summary')},
            'targets': [{'number': index + 1, 'state': row['state'], 'expected_path': row['expected_path'],
                         'http_status': row.get('observation', {}).get('http_status'),
                         'transport': row.get('observation', {}).get('transport', 'unknown')}
                        for index, row in enumerate(targets)],
            'incomplete_sections': sorted(environment.get('errors', {})),
            'diagnostic_errors': sorted(run.snapshot.get('check_errors', {})),
            'capabilities': copy.deepcopy(environment.get('capabilities', {})),
            'budget': copy.deepcopy(run.budget), 'network_writes': 0}


class CoreRuntime:
    def __init__(self, data_dir=None, platform=None, runner=run_command, config=None,
                 observer=None, directory=None, scope=None, events_factory=None,
                 on_report=None, guard_policy=None, clock=time.monotonic, guard_threaded=True,
                 model=None, model_consent=None, reasoning_policy=None, execution_directory=None,
                 model_factory=ResponsesModel):
        self.platform = platform or sys.platform
        if self.platform not in ('darwin', 'win32'):
            raise OSError('Unsupported platform')
        if self.platform == 'win32' and scope is None:
            from .platforms.windows import current_scope
            scope = current_scope()
        self.scope = scope
        self.data_dir = Path(data_dir) if data_dir else default_directory(self.platform, scope)
        create_private_directory(self.data_dir)
        self.store = AgentStore(self.data_dir / 'agent')
        self.closed = False
        self.task_cancelled = threading.Event()
        self.preserve_pending = False
        self.model, self.model_consent, self.reasoning_policy = model, model_consent, reasoning_policy
        self.model_factory = model_factory
        self.model_lock = threading.RLock()
        self.model_revision = uuid.uuid4().hex
        def read_command(argv, timeout=10):
            if self.closed:
                raise RuntimeError('Core runtime is stopping')
            return runner(argv, timeout=timeout)
        try:
            self.store.recover_interrupted()
            saved_model = self.store.model_configuration()
            if self.model is None and saved_model:
                self.model = self.model_factory(saved_model['name'], saved_model['endpoint'], '')
                self.model_consent = None
            self.config = config or ConfigManager(self.data_dir / 'config.json', runner=runner)
            if config is None:
                self.config.load()
            self._config_disk_revision = self._configuration_disk_revision()
            self.profiles = HealthProfiles(self.store, self.config)
            self.runtime_config = ProfileConfig(self.config, self.profiles)
            if self.platform == 'win32':
                from .platforms.windows import WindowsObserver
                from .platforms.windows_engine import WindowsDetectionEngine, WindowsRepairCapabilities
                from .platforms.windows_events import WindowsNetworkEvents
                observer = observer or WindowsObserver(read_command, directory=directory, expected_scope=scope)
                self.engine = WindowsDetectionEngine(self.runtime_config, read_command, observer, directory)
                self.fix = WindowsRepairCapabilities()
                events_factory = events_factory or WindowsNetworkEvents
            else:
                from .mac_events import MacNetworkEvents
                self.engine = DetectionEngine(self.runtime_config, read_command)
                from .mac_helper import mutation_writer
                writer = mutation_writer() if runner is run_command else None
                self.fix = FixEngine(self.runtime_config, self.engine, snapshot_dir=self.data_dir / 'recovery', mutation_writer=writer)
                events_factory = events_factory or MacNetworkEvents
            self.agent = NetworkAssuranceAgent(self.runtime_config, self.engine, self.fix, store=self.store,
                                               profiles=self.profiles, execution_directory=execution_directory)
            from .trust import RepairTrust
            self.trust = RepairTrust(self.store)
            self.agent.trust = self.trust
            if self.platform == 'darwin' and os.name == 'posix':
                from .dynamic import DynamicCommands
                self.agent.dynamic = DynamicCommands(self.agent, allowed=self.work_allowed,
                                                       coordination_dir=execution_directory)
            self.gate = threading.Lock()
            self.clock = clock
            self.on_report = on_report or (lambda _report: None)
            self.on_task_progress = lambda _event: None
            self._last_report = None
            self._guard_run = None
            self.guard = GuardService(self._submit_guard, lambda: None, guard_policy, clock, threaded=guard_threaded)
            self.guard.schedule.restore_ages(self.store.guard_check_ages())
            self._events_factory = events_factory
            self._events_closed = False
            self.events = events_factory(self.guard.changed)
        except BaseException:
            if hasattr(self, 'guard'):
                self.guard.close()
                if self.guard.thread:
                    self.guard.thread.join(5)
            self.store.close()
            raise

    def work_allowed(self):
        return not self.closed and not self.task_cancelled.is_set()

    def cancel_current(self, reason='user_cancelled'):
        self.task_cancelled.set()
        self.guard.pause()
        self.agent.revoke_all(reason)
        if reason == 'user_cancelled':
            self.store.save_core_guard_enabled(False)

    def check(self, progress=None, *, bounded=False):
        with self.gate:
            if self.closed:
                raise RuntimeError('Core runtime is closed')
            budget = ProbeBudget(GuardPolicy(), allowed=self.work_allowed, clock=self.clock) if bounded else None
            run = self.agent.observe(progress=progress, budget=budget, trigger='manual')
            if budget is not None and self.agent.recovery_pending and self.work_allowed():
                original = getattr(self.fix, 'runner', None)
                try:
                    if original:
                        self.fix.runner = lambda argv, timeout=10: budget.run(original, argv, timeout)
                    self.agent.reconcile(run.snapshot)
                finally:
                    if original:
                        self.fix.runner = original
                run.budget = budget.metrics()
                self.agent._save(run)
            return run, self._report(run)

    def _report(self, run):
        report = public_report(run, self.agent.assess(run.snapshot))
        report['persistence'] = {'journal': 'unavailable' if self.agent.journal_error else 'ok',
                                 'profile_verification': 'unavailable' if self.agent.profile_error else 'ok'}
        report['recovery_pending'] = self.agent.recovery_pending
        report['model'] = {key: run.model_state[key] for key in
            ('state', 'reason', 'calls', 'tools', 'input_tokens', 'output_tokens', 'usage_known', 'reserved_output_tokens')
            if key in run.model_state} if run.model_state else {'state': 'not_configured'}
        report['hypothesis_count'] = len(run.hypotheses)
        report['evidence_count'] = len(run.evidence)
        report['awaiting_authorization'] = run.stage == 'awaiting_authorization'
        report['investigation_suspended'] = self.agent.can_resume_investigation(run)
        report['execution_receipt_count'] = len(run.execution_history) + int(run.receipt is not None)
        report['authorization'] = copy.deepcopy(run.authorization_status)
        if run.receipt:
            report['network_writes'] = 0 if run.receipt.get('outcome') in ('blocked', 'not_started') else None
        if run.receipt and run.receipt.get('kind') == 'dynamic_command':
            report['network_writes'] = None if run.outcome != 'not_started' else 0
            job = run.receipt.get('job') or {}
            report['dynamic_job'] = {key: job.get(key) for key in
                                     ('reason', 'returncode', 'captured_bytes', 'output_truncated')}
            report['dynamic_job']['effects_verified'] = False
        return report

    def investigate(self, progress=None):
        with self.gate:
            if self.closed:
                raise RuntimeError('Core runtime is closed')
            budget = ProbeBudget(GuardPolicy(), allowed=self.work_allowed, clock=self.clock)
            run = self.agent.investigate(budget=budget, model=self.model, consent=self.model_consent,
                                         reasoning_policy=self.reasoning_policy, trigger='manual', progress=progress)
            if self.task_cancelled.is_set():
                self.agent.cancel(run)
            else:
                self._drive_trusted(run, progress=progress)
            return run, self._report(run)

    def propose(self, issue_types=None, progress=None):
        with self.gate:
            if not self.work_allowed():
                raise RuntimeError('Core runtime is stopping')
            run = self.agent.observe(progress=progress, budget=ProbeBudget(GuardPolicy(), allowed=self.work_allowed, clock=self.clock), trigger='manual')
            if self.work_allowed():
                self.agent._propose_run(run, issue_types)
            return run, self._report(run)

    def prepare_command(self, document):
        with self.gate:
            if self.closed or self.agent.dynamic is None:
                raise NotImplementedError('Dynamic jobs are unavailable in this runtime')
            run = self.agent.observe(budget=ProbeBudget(GuardPolicy(), allowed=lambda: not self.closed), trigger='manual')
            return self.agent.dynamic.prepare(run, document)

    def execute_command(self, run, proposal_hash, *, acknowledge_unrestricted=False):
        if not self.agent._is_dynamic(run):
            raise PermissionError('This is not a dynamic command proposal')
        return self.execute_proposal(run, proposal_hash, acknowledge_unrestricted=acknowledge_unrestricted)

    def execute_proposal(self, run, proposal_hash, *, acknowledge_unrestricted=False, progress=None):
        with self.gate:
            if not self.work_allowed() or proposal_hash != self.agent._proposal_hash(run):
                raise PermissionError('The exact current proposal must be confirmed')
            grant = self.agent.authorize(run, acknowledge_unrestricted=acknowledge_unrestricted)
            session = self.agent._reasoning_session
            if session is not None and session.run is run and run.trigger == 'guard':
                session.budget.allowed = self.work_allowed
                run.trigger = 'guard_followup'
                run.event('用户确认后由当前会话继续调查；保留原探测/模型额度，授权仍仅限本次方案')
            self.agent.execute(run, grant, progress=progress)
            return run, self._report(run)

    def resume_investigation(self, run):
        with self.gate:
            if self.closed:
                raise RuntimeError('Core runtime is closed')
            self.agent.resume_investigation(run, self.model, self.model_consent)
            self._drive_trusted(run)
            return run, self._report(run)

    def confirm_trust(self, run, proposal_hash, mode, scope_hash, revision, actor, progress=None):
        with self.gate:
            if (not self.work_allowed() or run.stage != 'awaiting_authorization'
                    or self.agent._proposals.get(run.id) != proposal_hash
                    or self.agent._proposal_hash(run) != proposal_hash
                    or self.agent.clock() >= run.proposals[0].expires_at
                    or self.store.needs_reconciliation() or self.agent.safety_error):
                raise PermissionError('A current concrete repair must be reviewed')
            self.trust.create(run, mode, scope_hash, actor, revision)
            session = self.agent._reasoning_session
            if session is not None and session.run is run and run.trigger == 'guard':
                session.budget.allowed = self.work_allowed
                run.trigger = 'guard_followup'
            self._drive_trusted(run, progress=progress)
            return run, self._report(run)

    def _drive_trusted(self, run, progress=None, allowed=None):
        """Caller owns the runtime gate; every iteration consumes a durable bounded reservation."""
        allowed = allowed or self.work_allowed
        while run.stage == 'awaiting_authorization' and self.trust.current:
            reason = ('cancelled' if not allowed() else 'recovery_pending' if self.agent.recovery_pending
                      else self.trust.reason(run))
            if reason:
                run.authorization_status = {'state': 'requires_review', 'reason': reason}
                self.agent._save(run)
                return
            policy = self.trust.status()['grant']
            grant = self.agent.authorize(run, trust_id=policy['id'], allowed=allowed)
            run.authorization_status = {'state': 'trusted', 'mode': policy['mode'], 'trust_id': policy['id']}
            def relay(event):
                self.on_task_progress({'run_id': run.id, 'mode': policy['mode'],
                                       'phase': event.get('phase'), 'message': event.get('message', '')})
                if progress:
                    progress(event)
            try:
                self.agent.execute(run, grant, progress=relay)
            except BaseException:
                self.trust.revoke('execution_not_verified')
                raise
            finally:
                self.on_task_progress(None)
            if run.outcome != 'verified':
                self.trust.revoke('execution_not_verified')
            if self.agent.can_resume_investigation(run) and allowed():
                self.agent.resume_investigation(run, self.model, self.model_consent)
            else:
                break
        if run.stage not in ('awaiting_authorization', 'authorized', 'executing'):
            self.trust.finish(run)

    def review_command(self, run_id, receipt_hash, note, *, expected_profile=None, expires_at=None):
        from .private_files import execution_slot
        with self.gate, execution_slot(self.agent.execution_directory):
            if self.closed or self.agent.dynamic is None:
                raise NotImplementedError('Dynamic command review is unavailable')
            if expires_at is not None and (time.time() >= expires_at or not self.work_allowed()):
                raise PermissionError('Receipt review expired or cancelled')
            if expected_profile is not None and expected_profile != self.profiles.binding():
                raise PermissionError('Profile changed; inspect the receipt again')
            result = self.agent.dynamic.review(run_id, receipt_hash, note)
            self._sync_reconciled_run(run_id, 'manual_review')
            return result

    def recover_configuration(self, run_id, receipt_hash, review, choice, *, expected_profile, expires_at, progress=None):
        from .agent_store import encode
        from .private_files import execution_slot
        with self.gate, execution_slot(self.agent.execution_directory):
            def authorize():
                if self.closed or not self.work_allowed() or time.time() >= expires_at:
                    raise PermissionError('Recovery review expired or cancelled')
                if expected_profile != self.profiles.binding():
                    raise PermissionError('Profile changed; inspect recovery again')
                record = self.store.get(run_id)
                if (record['stage'] != 'needs_reconciliation' or not record.get('receipt')
                        or record['receipt'].get('kind') == 'dynamic_command'
                        or hashlib.sha256(encode(record['receipt']).encode()).hexdigest() != receipt_hash):
                    raise PermissionError('Recovery receipt changed')
                return record['receipt']
            receipt = authorize()
            self.agent.revoke_all('recovery_pending')
            result, snapshot = self.fix.recover_configuration(receipt, review, choice, authorize, progress)
            self.agent._record_verification(snapshot)
            self.store.reconcile(run_id, result)
            self.agent.recovery_pending = self.store.needs_reconciliation()
            self._sync_reconciled_run(run_id, 'configuration_recovery')
            return result

    def _sync_reconciled_run(self, run_id, reason):
        run = self.agent._last_run
        if run and run.id == run_id:
            record = self.store.get(run_id)
            run.stage, run.outcome, run.stop_reason = record['stage'], record['outcome'], record['stop_reason']
            run.events, run.manual_review = copy.deepcopy(record['events']), copy.deepcopy(record.get('manual_review'))
            run.reconciliation = copy.deepcopy(record.get('reconciliation'))
            run.receipt = copy.deepcopy(record['receipt'])
            run.snapshot = self.engine.snapshot()
            run.health = self.agent._health(run.snapshot)
            run.targets = self.agent.health_profile()
            run.observations = self.agent._observations(run.snapshot, run.targets)
            run.issues = tuple(run.snapshot.get('issues', []))
            self.agent.revoke_all()
            if run.model_state:
                run.model_state.update(state='interrupted', reason=reason)
            self.agent._save(run, required=True)

    def command_record(self, run_id):
        with self.gate:
            return self._command_record(run_id)

    def _command_record(self, run_id):
        if self.closed or self.agent.dynamic is None:
            raise NotImplementedError('Dynamic command review is unavailable')
        record = self.store.get(run_id)
        if (record.get('receipt') or {}).get('kind') != 'dynamic_command':
            raise ValueError('Not a dynamic command receipt')
        try:
            terminal = self.agent.dynamic.inspect_receipt(record['receipt'])
            self.store.attach_job_receipt(run_id, terminal)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        record = self.store.get(run_id)
        if self.agent._last_run and self.agent._last_run.id == run_id:
            self.agent._last_run.receipt = copy.deepcopy(record['receipt'])
        return record

    def model_status(self):
        with self.model_lock:
            return self._model_status()

    def _model_status(self):
        model = self.model
        return {'configured': model is not None, 'name': model.name if model else '',
                'endpoint': model.endpoint if model else 'https://api.openai.com/v1/responses',
                'binding': model.binding if model else '', 'revision': self.model_revision,
                'local': model.local if model else False,
                'credential_present': bool(model and model.credential_present), 'credential_storage': 'core_memory',
                'consented': bool(model and self.model_consent and self.model_consent.allows(model)),
                'data_schema': EVIDENCE_SCHEMA}

    def configure_model(self, name, endpoint, api_key, credential_action, revision):
        with self.gate, self.model_lock:
            if not self.work_allowed() or revision != self.model_revision:
                raise PermissionError('Model configuration changed')
            if credential_action not in ('replace', 'keep', 'forget') or credential_action != 'replace' and api_key:
                raise ValueError('Invalid credential action')
            if credential_action == 'keep':
                if not self.model or self.model.endpoint != endpoint:
                    raise PermissionError('A credential cannot be moved to another endpoint')
                api_key = self.model._api_key
            if credential_action == 'forget':
                api_key = ''
            credentials = [api_key, self.model._api_key if self.model else '']
            if any(key and (key in name or key in endpoint) for key in credentials):
                raise ValueError('Credentials cannot be part of model configuration')
            replacement = self.model_factory(name, endpoint, api_key)
            self.store.save_model_configuration(name, endpoint)
            if self.model_consent:
                self.model_consent.revoke()
            if self.model:
                self.model.forget_credential()
            self.model, self.model_consent = replacement, None
            self.model_revision = uuid.uuid4().hex
            self.agent.revoke_all()
            self.store.privacy_event('configured', replacement.binding)
            return self.model_status()

    def grant_model_consent(self, revision, binding, expires_at=None):
        with self.gate, self.model_lock:
            if (not self.work_allowed() or not self.model or self.model_revision != revision or self.model.binding != binding
                    or not self.model.local and not self.model.credential_present
                    or expires_at is not None and (time.time() >= expires_at or not self.work_allowed())):
                raise PermissionError('Current model and credential must be reviewed')
            self.store.privacy_event('consent_granted', binding)
            if self.model_consent:
                self.model_consent.revoke()
            self.model_consent = ModelConsent(self.model)
            return self.model_status()

    def revoke_model_consent(self, forget=False):
        with self.model_lock:
            if self.model_consent:
                self.model_consent.revoke()
            if forget and self.model:
                self.model.forget_credential()
            self.model_revision = uuid.uuid4().hex
            self.store.privacy_event('credential_forgotten' if forget else 'consent_revoked',
                                     self.model.binding if self.model else '')
            return self.model_status()

    def _publish(self, report):
        identity = json.dumps({key: value for key, value in report.items() if key not in
                               ('run_id', 'captured_at', 'budget')}, sort_keys=True)
        if identity != self._last_report:
            self._last_report = identity
            try:
                self.on_report(report)
            except Exception:
                pass

    def start_guard(self, persist=True):
        if self.closed:
            raise RuntimeError('Core runtime is closed')
        if persist:
            self.store.save_core_guard_enabled(True)
        if self._events_closed:
            self.events = self._events_factory(self.guard.changed)
            self._events_closed = False
        self.events.start()
        self.guard.enable()
        policy = self.trust.status()['grant']
        trusted = bool(policy and policy['state'] == 'active')
        return {'event_source': 'native' if self.events.available else 'periodic_only',
                'network_writes': None if trusted else False, 'lifetime': 'foreground_process',
                'write_authority': policy['mode'] if trusted else 'per_run_confirmation',
                'model': 'configured' if self.model is not None else 'not_configured'}

    def pause_guard(self, persist=True):
        self.guard.pause()
        self.events.close()
        self._events_closed = True
        if persist:
            self.store.save_core_guard_enabled(False)

    def restore_guard(self):
        if self.store.core_guard_enabled():
            self.start_guard(persist=False)

    def _configuration_disk_revision(self):
        try:
            with Path(self.config.config_path).open('rb') as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(65536), b''):
                    digest.update(block)
            return digest.hexdigest()
        except OSError:
            return None

    def configuration_status(self):
        from .agent_store import encode
        return {'revision': hashlib.sha256(encode(self.config.config).encode()).hexdigest(),
                'preset': self.config.get('general.preset', 'observe'),
                'available': self.platform == 'darwin' and not self.config.load_error,
                'load_error': bool(self.config.load_error)}

    def configure_detection(self, revision, *, preset=None, redetect=False):
        with self.gate:
            if (not self.work_allowed() or not self.configuration_status()['available']
                    or revision != self.configuration_status()['revision']
                    or self._config_disk_revision != self._configuration_disk_revision()):
                raise PermissionError('Detection configuration changed or is unavailable')
            self.agent.revoke_all('permissions_changed')
            enabled = self.guard.schedule.enabled
            self.pause_guard(persist=False)
            try:
                candidate = copy.copy(self.config)
                candidate.config = copy.deepcopy(self.config.config)
                if redetect:
                    budget = ProbeBudget(GuardPolicy(), allowed=self.work_allowed, clock=self.clock)
                    original = self.config.runner
                    candidate.runner = lambda argv, timeout=10: budget.run(original, argv, timeout)
                    candidate.auto_detect()
                    candidate.runner = original
                elif not candidate.apply_preset(preset):
                    raise ValueError('Unsupported detection preset')
                if not self.work_allowed() or self._config_disk_revision != self._configuration_disk_revision():
                    raise PermissionError('Configuration update was cancelled or changed on disk')
                candidate.save()
                self.config.config = candidate.config
                self.config.load_error = candidate.load_error
                self._config_disk_revision = self._configuration_disk_revision()
                self.engine.reload_checks(clear_snapshot=True)
            finally:
                if enabled and self.work_allowed() and self.store.core_guard_enabled():
                    self.start_guard(persist=False)

    def _submit_guard(self, ticket):
        if self.closed or not self.gate.acquire(blocking=False):
            return False
        pending = self.agent._last_run
        if self.preserve_pending and pending and pending.stage in ('awaiting_authorization', 'authorized'):
            if pending.proposals and self.agent.clock() >= pending.proposals[0].expires_at:
                self.agent.cancel(pending)
            else:
                self.gate.release()
                return False
        budget = ProbeBudget(self.guard.schedule.policy,
            allowed=lambda: not self.closed and self.guard.schedule.valid(ticket), clock=self.clock)
        failed = False
        report = None
        try:
            if not self.guard.schedule.valid(ticket):
                return True
            if not self.store.reserve_guard_check(self.guard.schedule.policy.hourly_checks):
                self.guard.schedule.restore_ages(self.store.guard_check_ages())
                budget.stop_reason = 'hourly_budget'
                return True
            if self._guard_run is not None:
                self.agent.cancel(self._guard_run)
            kwargs = {'model': self.model, 'consent': self.model_consent, 'reasoning_policy': self.reasoning_policy} if self.model else {}
            run = self.agent.investigate(budget=budget, **kwargs)
            self._guard_run = run
            self._drive_trusted(run, allowed=lambda: self.work_allowed() and self.guard.schedule.valid(ticket))
            failed = bool(run.snapshot.get('check_errors') or run.health in ('unknown', 'degraded'))
            report = self._report(run)
        except Exception:
            failed = True
            report = {'schema': 'relay-core-report-v1', 'redacted': True, 'health': 'unknown',
                      'stage': 'needs_evidence', 'diagnostic_errors': ['core_check'], 'network_writes': 0}
        finally:
            self.guard.complete(ticket, failed, budget.metrics())
            self.gate.release()
        # Client callbacks may close the core; never call them while owning its gate.
        if report is not None:
            self._publish(report)
        return True

    def close(self, before_store_close=None):
        if self.closed:
            return
        self.closed = True
        if self.model_consent:
            self.model_consent.revoke()
        if self.model:
            self.model.forget_credential()
        self.guard.close()
        try:
            self.agent.revoke_all('core_stopped')
        except Exception:
            self.agent.safety_error = '停止时授权记录无法保存；内存执行权限已撤销'
        self.events.close()
        if self.guard.thread and self.guard.thread is not threading.current_thread():
            self.guard.thread.join(65)
        # Keep the journal owned until the last read-only command has settled.
        with self.gate:
            if self._guard_run is not None:
                self.agent.cancel(self._guard_run)
            try:
                if before_store_close:
                    before_store_close()
            finally:
                self.store.close()

    def raw_report(self, run):
        return asdict(run)
