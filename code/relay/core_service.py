"""Own live tasks across desktop connections; dispatch only explicit versioned requests."""
import copy
from dataclasses import asdict
import hashlib
import json
import threading
import time
import uuid

from jsonschema import Draft202012Validator

from .agent_records import record_identity, records_report
from .agent_store import encode
from .ipc import core_server, RpcError


def schema(properties=None, required=None):
    properties = properties or {}
    return {'type': 'object', 'properties': properties, 'additionalProperties': False,
            'required': list(properties) if required is None else required}


IDENTIFIER = {'type': 'string', 'minLength': 1, 'maxLength': 80}
HASH = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
TOKEN = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
METHODS = {
    'status': schema(), 'records': schema(), 'profiles': schema(), 'check': schema(),
    'investigate': schema(), 'cancel': schema(), 'detach': schema(), 'revoke_model': schema(),
    'operation': schema({'id': TOKEN}),
    'propose': schema({'issues': {'type': ['array', 'null'], 'maxItems': 16,
                                'uniqueItems': True, 'items': IDENTIFIER}}),
    'review': schema({'run_id': IDENTIFIER}),
    'confirm': schema({'run_id': IDENTIFIER, 'proposal_hash': HASH, 'review_token': TOKEN,
                       'accept': {'type': 'boolean'}, 'unrestricted': {'type': 'boolean'}}),
    'confirm_trust': schema({'run_id': IDENTIFIER, 'proposal_hash': HASH, 'review_token': TOKEN,
                            'mode': {'enum': ['task', 'continuous']}, 'acknowledge': {'const': True}}),
    'revoke_trust': schema(),
    'guard': schema({'enabled': {'type': 'boolean'}}),
    'detection_preset': schema({'revision': HASH, 'name': {'enum': ['observe', 'company', 'personal_proxy', 'minimal']}}),
    'detection_redetect': schema({'revision': HASH}),
    'shutdown': schema({'purpose': {'enum': ['stop', 'service_change', 'uninstall']}}),
    'profile_export': schema({'profile_id': IDENTIFIER}),
    'profile_save': schema({'document': {'type': 'object'}, 'profile_id': {'type': ['string', 'null'], 'maxLength': 80},
                            'revision': {'type': ['integer', 'null'], 'minimum': 1}, 'imported': {'type': 'boolean'}}),
    'profile_activate': schema({'profile_id': IDENTIFIER}),
    'profile_remove': schema({'profile_id': IDENTIFIER}),
    'settings': schema(), 'consent_review': schema(), 'forget_credential': schema(),
    'task_detail': schema({'run_id': IDENTIFIER}),
    'receipt_review': schema({'run_id': IDENTIFIER}),
    'confirm_receipt': schema({'run_id': IDENTIFIER, 'receipt_hash': HASH, 'review_token': TOKEN,
                              'note': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                              'acknowledge': {'const': True}}),
    'confirm_recovery': schema({'run_id': IDENTIFIER, 'receipt_hash': HASH, 'review_token': TOKEN,
                               'choice': {'enum': ['restore', 'retain']}, 'acknowledge': {'const': True}}),
    'model_configure': schema({'name': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                              'endpoint': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
                              'api_key': {'type': 'string', 'maxLength': 4096},
                              'credential_action': {'enum': ['replace', 'keep', 'forget']}, 'revision': TOKEN}),
    'confirm_consent': schema({'revision': TOKEN, 'binding': HASH, 'review_token': TOKEN,
                              'allow_upload': {'const': True}}),
    'preferences_save': schema({'revision': {'type': 'integer', 'minimum': 0}, 'values': schema({
        'history_days': {'type': 'integer', 'minimum': 7, 'maximum': 90},
        'history_limit': {'type': 'integer', 'minimum': 100, 'maximum': 1000},
        'notifications_enabled': {'type': 'boolean'}})}),
}
READS = {'status', 'records', 'profiles', 'profile_export', 'review', 'operation', 'detach',
         'settings', 'receipt_review', 'consent_review', 'task_detail'}
URGENT = ('cancel', 'revoke_model', 'forget_credential', 'revoke_trust', 'shutdown')


class CoreService:
    def __init__(self, runtime, *, stop_requested=None, launch_id=None, managed=False, server_factory=core_server):
        self.runtime = runtime
        self.instance = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.busy = None
        self.worker = None
        self.closing = False
        self.draining = False
        self.shutdown_request = None
        self.stop_requested = stop_requested if stop_requested is not None else threading.Event()
        self.launch_id = launch_id
        self.reviews = {}
        self.last_operation = None
        self.state = {}
        self.active_execution = None
        self.runtime.preserve_pending = True
        self.runtime.store.interrupt_ipc_requests()
        self.runtime.on_report = lambda _: self.refresh()
        self.runtime.on_task_progress = self.task_progress
        self.refresh()
        self.server = server_factory(runtime.data_dir, self.dispatch, self.instance, managed=managed)

    def start(self):
        self.server.start()

    def refresh(self):
        runtime = self.runtime
        with runtime.gate:
            snapshot = runtime.engine.snapshot()
            assessment = runtime.agent.assess(snapshot)
            assessment.update(journal_error=runtime.agent.safety_error or runtime.agent.journal_error,
                              profile_error=runtime.agent.profile_error, recovery_pending=runtime.agent.recovery_pending)
            run = runtime.agent._last_run
            state = {'ready': not self.closing, 'snapshot': snapshot, 'agent': assessment,
                     'repair_options': runtime.agent.repair_options(),
                     'enabled_checks': [check.name for check in getattr(runtime.engine, 'checks', [])] or None,
                     'overall': runtime.engine.get_overall_status(), 'summary': runtime.engine.get_status_summary(),
                     'repair': None, 'scan_progress': None, 'live_snapshot': None,
                     'run_id': run.id if run else None, 'run_stage': run.stage if run else None,
                     'report': runtime._report(run) if run else None,
                     'preset': runtime.config.get('general.preset', 'observe'),
                     'config_path': str(runtime.config.config_path)}
            state['preferences'] = runtime.store.preferences()
            state['detection'] = runtime.configuration_status()
        # The transport has no Python object decoding; datetime becomes a documented string.
        with self.lock:
            self.state = json.loads(encode(state))

    def status(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            state.update(core_instance=self.instance, connection='connected', busy=self.busy['method'] if self.busy else '',
                         operation=copy.deepcopy(self.busy or self.last_operation))
            state.update(draining=self.draining, launch_id=self.launch_id)
            if self.draining:
                state['ready'] = False
            state['model'] = self.runtime.model_status()
            state['trust'] = self.runtime.trust.status()
            state['active_execution'] = copy.deepcopy(self.active_execution)
            if self.active_execution:
                state.update(run_id=self.active_execution['run_id'], run_stage='executing')
                state['busy'] = state['busy'] or 'trusted_execution'
            state['guard'] = {**self.runtime.guard.schedule.state(), 'changing': False,
                'event_source': 'native' if self.runtime.events.available else 'periodic_only',
                'attention': bool(state['snapshot'].get('issues')),
                'proposal_ready': state.get('run_stage') == 'awaiting_authorization', 'error': ''}
            return state

    def task_progress(self, event):
        with self.lock:
            self.active_execution = copy.deepcopy(event)

    def progress(self, event):
        with self.lock:
            self.state['scan_progress'] = json.loads(encode({k: v for k, v in event.items() if k not in ('snapshot', 'changes')}))
            if event.get('snapshot'):
                self.state['live_snapshot'] = json.loads(encode(event['snapshot']))

    def _run(self, run_id):
        run = self.runtime.agent._last_run
        if not run or run.id != run_id:
            raise RpcError('task_not_live')
        return run

    def _review_token(self, kind, client, value):
        token = uuid.uuid4().hex
        with self.lock:
            self.reviews = {key: row for key, row in self.reviews.items() if row['expires_at'] > time.time()
                            and not (row['client'] == client and row['kind'] == kind)}
            if len(self.reviews) >= 32:
                raise RpcError('review_limit')
            self.reviews[token] = {**value, 'kind': kind, 'client': client}
        return {**value, 'review_token': token, 'core_instance': self.instance}

    def _require_review(self, kind, client, params, keys):
        review = self.reviews.get(params['review_token'])
        if (not review or review['kind'] != kind or review['client'] != client
                or review['expires_at'] <= time.time() or any(review[key] != params[key] for key in keys)):
            raise RpcError('review_required')
        return review

    def settings(self):
        runtime = self.runtime
        trust = runtime.trust.status()
        with self.lock:
            detection = copy.deepcopy(self.state['detection'])
        return {'core_instance': self.instance, 'model': runtime.model_status(),
                'preferences': runtime.store.preferences(), 'privacy_events': runtime.store.privacy_history(),
                'trust': trust, 'authorization_events': runtime.store.trust_history(),
                'authorization': trust['grant']['mode'] if trust['grant'] and trust['grant']['state'] == 'active' else 'per_run_confirmation',
                'privileged_helper': None,
                'system_mutation_route': 'authenticated_helper' if getattr(runtime.fix, 'mutation_writer', None) else 'ordinary_user',
                'ipc_identity': self.server.identity_mode,
                'persistent_startup': None, 'managed_updates': False, 'platform': runtime.platform,
                'recovery_pending': runtime.store.needs_reconciliation(),
                'data_directory': str(runtime.data_dir), 'detection': detection}

    def _receipt_review(self, run_id, client_id):
        record = self.runtime.store.get(run_id)
        if (record.get('receipt') or {}).get('kind') == 'dynamic_command':
            record = self.runtime._command_record(run_id)
        if not record.get('receipt'):
            raise RpcError('receipt_missing')
        value = {'run_id': run_id, 'receipt_hash': hashlib.sha256(encode(record['receipt']).encode()).hexdigest(),
                 'expires_at': time.time() + 300, 'profile_binding': self.runtime.profiles.binding()}
        recovery = None
        if (record['stage'] == 'needs_reconciliation' and record['receipt'].get('kind') != 'dynamic_command'
                and any('platform_target' in action for action in record['receipt'].get('actions', []))):
            try:
                recovery = self.runtime.fix.recovery_review(record['receipt'])
                value['recovery'] = recovery
            except Exception:
                recovery = {'state': 'unavailable', 'can_restore': False, 'can_retain': False}
        review = self._review_token('receipt', client_id, value)
        # The helper's one-use token and complete manifest remain only in Core memory.
        review.pop('recovery', None)
        public_recovery = ({key: recovery[key] for key in ('state', 'fields', 'eligible', 'can_restore',
            'can_retain', 'lease_expires_at', 'disposition') if key in recovery} if recovery else None)
        return {**review, 'receipt': record['receipt'], 'execution_history': record.get('execution_history', []),
                'recovery': public_recovery,
                'reconciliation': record.get('reconciliation'),
                'manual_review': record.get('manual_review'), 'stage': record['stage'], 'outcome': record.get('outcome'),
                'can_acknowledge': record['stage'] == 'needs_reconciliation'
                    and record['receipt'].get('kind') == 'dynamic_command'}

    def dispatch(self, method, params, request_id, client_id):
        definition = METHODS.get(method)
        if definition is None or not Draft202012Validator(definition).is_valid(params):
            raise RpcError('invalid_request')
        if self.closing:
            raise RpcError('core_stopping')
        if method == 'status':
            return self.status()
        if method == 'operation':
            row = self.runtime.store.ipc_request(params['id'])
            if not row:
                raise RpcError('operation_not_found')
            return row[1]
        if method == 'detach':
            with self.lock:
                self.reviews = {key: value for key, value in self.reviews.items() if value['client'] != client_id}
            return {'detached': True, 'core_running': True}
        # These projections read committed journal state, not live mutable engine objects.
        if method in ('records', 'task_detail'):
            try:
                from .commercial.history import redact
            except ImportError:
                redact = None
            if method == 'task_detail':
                from .task_details import task_detail
                return task_detail(self.runtime.store.get(params['run_id']), redact)
            pending = self.runtime.store.pending_reconciliation()
            return {**records_report(self.runtime.store.recent(), redact, controls=True),
                    'pending': [{**record_identity(row), 'stage': row['stage'],
                                 'outcome': row.get('outcome')} for row in pending]}
        if method == 'settings':
            return self.settings()
        if method in READS:
            if not self.runtime.gate.acquire(blocking=False):
                raise RpcError('busy')
            try:
                if method == 'receipt_review':
                    return self._receipt_review(params['run_id'], client_id)
                if method == 'consent_review':
                    model = self.runtime.model_status()
                    if not model['configured']:
                        raise RpcError('model_not_configured')
                    return {**self._review_token('consent', client_id, {
                        'revision': model['revision'], 'binding': model['binding'], 'expires_at': time.time() + 300}),
                        'model': model}
                if method == 'profiles':
                    return {'profiles': copy.deepcopy(self.runtime.profiles.profiles),
                            'active_id': (self.runtime.profiles.active or {}).get('id'),
                            'assessment': self.runtime.profiles.evaluate(self.runtime.engine.snapshot())}
                if method == 'profile_export':
                    return self.runtime.profiles.document(params['profile_id'])
                if method == 'review':
                    run = self._run(params['run_id'])
                    if run.stage != 'awaiting_authorization' or time.time() >= run.proposals[0].expires_at:
                        raise RpcError('proposal_expired')
                    value = {'run_id': run.id, 'proposal_hash': self.runtime.agent._proposal_hash(run),
                             'expires_at': run.proposals[0].expires_at,
                             'trust_revision': self.runtime.trust.revision,
                             'trust_offer': self.runtime.trust.offer(run)}
                    return {**self._review_token('proposal', client_id, value), 'proposal': asdict(run.proposals[0]),
                            'unrestricted': self.runtime.agent._is_dynamic(run)}
            finally:
                self.runtime.gate.release()
        fingerprint = hashlib.sha256(encode([client_id, method, params]).encode()).hexdigest()
        urgent = method in URGENT or method == 'guard' and params['enabled'] is False
        with self.lock:
            prior = self.runtime.store.ipc_request(request_id)
            if prior:
                if prior[0] != fingerprint:
                    raise RpcError('request_conflict')
                return prior[1]
            if self.draining:
                raise RpcError('core_stopping')
            if self.busy and not urgent:
                raise RpcError('busy')
            if self.runtime.agent.safety_error and method != 'check' and not urgent:
                raise RpcError('journal_unavailable')
            context = None
            if method in ('confirm', 'confirm_trust'):
                context = self._require_review('proposal', client_id, params, ('run_id', 'proposal_hash'))
                run = self._run(params['run_id'])
                if run.stage != 'awaiting_authorization' or self.runtime.agent._proposal_hash(run) != params['proposal_hash']:
                    raise RpcError('proposal_changed')
                if method == 'confirm_trust':
                    if (not context['trust_offer']['available']
                            or context['trust_revision'] != self.runtime.trust.revision):
                        raise RpcError('trust_review_required')
                    context = {**context, 'actor': client_id}
                elif params['accept'] and self.runtime.agent._is_dynamic(run) and params['unrestricted'] is not True:
                    raise RpcError('unrestricted_review_required')
            else:
                if method in ('confirm_receipt', 'confirm_recovery'):
                    context = self._require_review('receipt', client_id, params, ('run_id', 'receipt_hash'))
                    if method == 'confirm_recovery' and (context.get('recovery') or {}).get('can_' + params['choice']) is not True:
                        raise RpcError('review_required')
                    if method == 'confirm_receipt' and not params['note'].strip():
                        raise RpcError('invalid_request')
                elif method == 'confirm_consent':
                    context = self._require_review('consent', client_id, params, ('revision', 'binding'))
                    if self.runtime.model_status()['revision'] != params['revision']:
                        raise RpcError('review_required')
            if method not in (*URGENT, 'guard', 'confirm', 'confirm_trust'):
                run = self.runtime.agent._last_run
                if run and run.stage in ('awaiting_authorization', 'authorized'):
                    raise RpcError('pending_proposal')
            operation = {'id': request_id, 'method': method, 'state': 'accepted', 'core_instance': self.instance}
            try:
                self.runtime.store.save_ipc_request(request_id, fingerprint, operation, create=True)
            except Exception:
                if urgent:
                    self._urgent(method, params)
                raise
            if method == 'shutdown':
                from .lifecycle import set_inhibited
                try:
                    set_inhibited(self.runtime.data_dir, True)
                except Exception:
                    self._urgent(method, params)
                    raise
                self.draining = True
                self.shutdown_request = (operation, fingerprint, params['purpose'])
                self.reviews.clear()
                try:
                    self.runtime.cancel_current('core_stopped')
                    if params['purpose'] != 'service_change':
                        self.runtime.store.save_core_guard_enabled(False)
                finally:
                    self.stop_requested.set()
                return copy.deepcopy(operation)
            if method in ('confirm', 'confirm_trust', 'confirm_receipt', 'confirm_recovery', 'confirm_consent'):
                self.reviews.pop(params['review_token'])
            if urgent:
                try:
                    self._urgent(method, params)
                    operation['state'] = 'completed'
                except Exception:
                    operation.update(state='failed', error='operation_failed')
                self.runtime.store.save_ipc_request(request_id, fingerprint, operation)
                return operation
            self.runtime.task_cancelled.clear()
            self.busy = operation
            self.worker = threading.Thread(target=self._work, args=(operation, fingerprint, method, copy.deepcopy(params), context),
                                           name='NetCare core operation', daemon=True)
            self.worker.start()
            return copy.deepcopy(operation)

    def _urgent(self, method, params=None):
        if method == 'shutdown':
            self.draining = True
            try:
                self.runtime.cancel_current('core_stopped')
            finally:
                self.stop_requested.set()
        if method == 'guard' and params['enabled'] is False:
            self.runtime.pause_guard()
        if method == 'revoke_trust':
            self.reviews = {key: row for key, row in self.reviews.items() if row['kind'] != 'proposal'}
            self.runtime.trust.revoke()
        if method in ('revoke_model', 'forget_credential'):
            self.reviews = {key: row for key, row in self.reviews.items() if row['kind'] != 'consent'}
            self.runtime.revoke_model_consent(forget=method == 'forget_credential')
        if method == 'cancel':
            self.runtime.cancel_current()
            self.reviews.clear()
            run = self.runtime.agent._last_run
            if run:
                self.runtime.agent.cancel(run)
                self.state.update(run_stage=run.stage, report=self.runtime._report(run))

    def _work(self, operation, fingerprint, method, params, context=None):
        runtime = self.runtime
        try:
            operation['state'] = 'running'
            runtime.store.save_ipc_request(operation['id'], fingerprint, operation)
            if not runtime.work_allowed():
                raise RpcError('cancelled')
            if method in ('check', 'investigate', 'propose'):
                if method == 'check':
                    run, _ = runtime.check(progress=self.progress, bounded=True)
                elif method == 'investigate':
                    run, _ = runtime.investigate(progress=self.progress)
                else:
                    run, _ = runtime.propose(params['issues'], progress=self.progress)
                operation['run_id'] = run.id
            elif method == 'model_configure':
                runtime.configure_model(**params)
            elif method in ('detection_preset', 'detection_redetect'):
                runtime.configure_detection(params['revision'], preset=params.get('name'), redetect=method == 'detection_redetect')
                if method == 'detection_redetect' and runtime.work_allowed():
                    runtime.check(progress=self.progress, bounded=True)
            elif method == 'confirm_consent':
                runtime.grant_model_consent(params['revision'], params['binding'], expires_at=context['expires_at'])
            elif method == 'confirm_receipt':
                runtime.review_command(params['run_id'], params['receipt_hash'], params['note'],
                                       expected_profile=context['profile_binding'], expires_at=context['expires_at'])
                operation['run_id'] = params['run_id']
            elif method == 'confirm_recovery':
                runtime.recover_configuration(params['run_id'], params['receipt_hash'], context['recovery'], params['choice'],
                    expected_profile=context['profile_binding'], expires_at=context['expires_at'], progress=self.progress)
                operation['run_id'] = params['run_id']
            elif method == 'confirm':
                run = self._run(params['run_id'])
                if not params['accept']:
                    runtime.agent.cancel(run)
                else:
                    runtime.execute_proposal(run, params['proposal_hash'],
                        acknowledge_unrestricted=params['unrestricted'], progress=self.progress)
                    if runtime.agent.can_resume_investigation(run) and runtime.work_allowed():
                        runtime.resume_investigation(run)
                operation['run_id'] = run.id
            elif method == 'confirm_trust':
                run = self._run(params['run_id'])
                runtime.confirm_trust(run, params['proposal_hash'], params['mode'],
                    context['trust_offer']['scope_hash'], context['trust_revision'], context['actor'], progress=self.progress)
                operation['run_id'] = run.id
            else:
                with runtime.gate:
                    if method == 'guard':
                        if params['enabled']:
                            runtime.start_guard()
                        else:
                            runtime.pause_guard()
                    elif method == 'profile_save':
                        profile = runtime.profiles.save(params['document'], params['profile_id'], params['revision'],
                            source='user_import' if params['imported'] else 'user')
                        operation['profile_id'] = profile['id']
                    elif method == 'profile_activate':
                        runtime.profiles.activate(params['profile_id'])
                    elif method == 'profile_remove':
                        runtime.profiles.remove(params['profile_id'])
                    elif method == 'preferences_save':
                        runtime.store.save_preferences(params['values'], params['revision'])
                    else:
                        raise RpcError('invalid_request')
                    if method.startswith('profile_'):
                        runtime.agent.revoke_all()
                        runtime.guard.changed()
            operation['state'] = 'cancelled' if runtime.task_cancelled.is_set() else 'completed'
        except Exception as exc:
            operation.update(state='failed', error=exc.reason if isinstance(exc, RpcError) else 'operation_failed')
        finally:
            try:
                runtime.store.save_ipc_request(operation['id'], fingerprint, operation)
            except Exception:
                runtime.agent.safety_error = '操作收据未能保存；后续修改已停用，请核对本地记录'
                operation.update(state='unknown', error='journal_unavailable')
            try:
                self.refresh()
            except Exception:
                with self.lock:
                    self.state.update(ready=False, connection='state_unavailable')
            finally:
                with self.lock:
                    self.last_operation, self.busy = copy.deepcopy(operation), None

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.draining = True
        try:
            self.runtime.cancel_current('core_stopped')
        except Exception:
            self.runtime.agent.safety_error = '停止时授权记录无法保存；内存执行权限已撤销'
        self.server.close()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join()
        def record_shutdown():
            if self.shutdown_request:
                operation, fingerprint, purpose = self.shutdown_request
                if purpose != 'service_change':
                    self.runtime.store.save_core_guard_enabled(False)
                operation['state'] = 'completed'
                self.runtime.store.save_ipc_request(operation['id'], fingerprint, operation)
        self.runtime.close(before_store_close=record_shutdown)
        if self.shutdown_request:
            from .lifecycle import write_private_json
            operation, _, purpose = self.shutdown_request
            write_private_json(self.runtime.data_dir / 'lifecycle/stopped.json', {
                'version': 1, 'state': 'stopped', 'core_instance': self.instance,
                'request_id': operation['id'], 'purpose': purpose,
                'data_preserved': True, 'stopped_at': time.time()})
