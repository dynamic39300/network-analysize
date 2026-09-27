"""Desktop state cache and asynchronous local-core calls. Never executes OS tools."""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import threading
import uuid

from .ipc import LocalClient, RpcError
from .profiles import expire_assessment, parse_import

LABELS = {'check': '网络检测', 'investigate': '调查网络问题', 'propose': '准备修复方案',
          'detection_preset': '更新检测预设', 'detection_redetect': '重新识别环境',
          'confirm': '执行并验证已确认方案', 'guard': '更新守护状态',
          'confirm_trust': '保存范围信任并处理方案', 'revoke_trust': '撤销范围信任',
          'trusted_execution': '按范围信任自动处理',
          'confirm_receipt': '记录人工核对', 'model_configure': '保存模型配置',
          'confirm_recovery': '处理恢复方案并复验网络',
          'confirm_consent': '更新上传同意', 'preferences_save': '保存设置',
          'profile_save': '保存网络档案', 'profile_activate': '切换网络档案', 'profile_remove': '移除网络档案'}


class RemoteController:
    def __init__(self, data_dir, on_event, dispatch, client_factory=LocalClient):
        self.data_dir = Path(data_dir)
        self.on_event, self.dispatch, self.client_factory = on_event, dispatch, client_factory
        self.client = None
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=8)
        self.pending = None
        self.desktop = None
        self.last_notice = None
        self.state = {'ready': False, 'busy': '', 'overall': 'unknown', 'summary': [],
                      'snapshot': {'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None},
                      'agent': {}, 'repair_options': {}, 'repair': None, 'guard': {}, 'connection': 'connecting'}
        self.thread = threading.Thread(target=self._loop, name='Relay desktop connection', daemon=True)

    def start(self):
        self.thread.start()

    def _emit(self, event, payload=None):
        if not self.stopped.is_set():
            self.dispatch(self.on_event, event, payload or {})

    def ui_state(self):
        with self.lock:
            value = copy.deepcopy(self.state)
        value['busy'] = LABELS.get(value.get('busy'), value.get('busy', ''))
        for snapshot in (value.get('snapshot'), value.get('live_snapshot')):
            if snapshot and isinstance(snapshot.get('last_check'), str):
                snapshot['last_check'] = datetime.fromisoformat(snapshot['last_check'])
        profile = value.get('agent', {}).get('profile')
        if profile:
            expire_assessment(profile)
            if profile.get('health') == 'unknown':
                value['overall'] = 'unknown'
        return value

    def _poll(self):
        if self.client is None:
            self.client = self.client_factory(self.data_dir)
        state = self.client.call('status')
        with self.lock:
            self.state = state
        if self.desktop:
            self.desktop.observe_state(state)
        self._notification(state)
        self._emit('state')
        if self.pending:
            request_id, method = self.pending
            try:
                operation = self.client.call('operation', {'id': request_id})
            except RpcError as exc:
                if exc.reason != 'operation_not_found':
                    raise
                self.pending = None
                self._emit('alert', {'title': '请求结果未确认', 'message': '核心没有该请求记录。未自动重发，请先核对当前任务状态。'})
                return
            if operation['state'] in ('accepted', 'running') or state.get('busy'):
                return
            self.pending = None
            if operation['state'] != 'completed':
                self._emit('alert', {'title': '操作需要核对', 'message': '本次操作未确认完成；未自动重发。请查看当前状态及处理记录。'})
            if method.startswith('profile_'):
                self._emit('profiles', {**self.client.call('profiles'), 'replace': True})
            if method in ('model_configure', 'confirm_consent', 'preferences_save', 'revoke_model', 'forget_credential',
                          'confirm_trust', 'revoke_trust', 'detection_preset', 'detection_redetect'):
                self._emit('settings_refresh', {**self.client.call('settings'), 'completed': operation['state'] == 'completed'})
            if method in ('confirm_receipt', 'confirm_recovery'):
                self.show_agent_records()
            if state.get('run_stage') == 'awaiting_authorization' and method in ('propose', 'investigate', 'confirm', 'confirm_trust'):
                self._review()

    def _notification(self, state):
        if not state.get('ready') or state.get('busy'):
            return
        report = state.get('report') or {}
        key = (state.get('agent', {}).get('incident_id') or report.get('run_id'), report.get('stage'))
        previous, self.last_notice = self.last_notice, key
        if (previous is None or previous == key
                or not state.get('preferences', {}).get('values', {}).get('notifications_enabled')):
            return
        if report.get('stage') == 'awaiting_authorization':
            self._emit('notification', {'title': '网络处理等待确认', 'message': '方案已准备好，尚未执行。'})
        elif report.get('stage') == 'needs_reconciliation':
            self._emit('notification', {'title': '执行结果需要核对', 'message': '请在处理记录中审阅具体收据。'})

    def _loop(self):
        try:
            while not self.stopped.is_set():
                try:
                    action = self.queue.get(timeout=1)
                except queue.Empty:
                    action = None
                try:
                    if action and not self.stopped.is_set():
                        if self.client is None:
                            self._poll()
                        action()
                    if not self.stopped.is_set():
                        self._poll()
                except RpcError as exc:
                    self._emit('alert', {'title': '请求未完成', 'message': {
                        'pending_proposal': '当前有待确认方案，请先查看或取消。',
                        'busy': '核心正在处理当前任务，请稍后再试。',
                        'proposal_expired': '方案已过期，请停止当前任务后重新检测。',
                        'review_required': '确认已失效，请重新查看当前方案。',
                    }.get(exc.reason, '请求未确认完成，未自动重发。请重新连接并核对处理记录。')})
                    if exc.reason in ('core_changed', 'invalid_response'):
                        self._disconnected()
                except Exception:
                    self._disconnected()
        finally:
            if self.client:
                try:
                    self.client.call('detach')
                except Exception:
                    pass

    def _disconnected(self):
        self.client = None
        with self.lock:
            self.state.update(ready=False, busy='', connection='disconnected', overall='unknown')
            self.state['repair_options'] = {}
            self.state['agent']['journal_error'] = '与核心的连接已中断；执行结果待核对，未自动重发'
        self._emit('state')

    def _submit(self, action):
        if self.stopped.is_set():
            return False
        try:
            self.queue.put_nowait(action)
            return True
        except queue.Full:
            return False

    def _command(self, method, params=None):
        def send():
            request_id = uuid.uuid4().hex
            self.pending = (request_id, method)
            try:
                self.client.call(method, params, request_id)
            except RpcError as exc:
                if exc.reason in ('busy', 'pending_proposal', 'invalid_request', 'request_conflict',
                                  'review_required', 'proposal_changed', 'unrestricted_review_required', 'trust_review_required', 'core_stopping'):
                    self.pending = None
                raise
        return self._submit(send)

    def check(self, notify=False):
        return self._command('check')

    def investigate(self):
        return self._command('investigate')

    def prepare_fix(self, issues=None):
        if self.ui_state().get('run_stage') == 'awaiting_authorization':
            return self._submit(self._review)
        return self._command('propose', {'issues': list(issues) if issues is not None else None})

    def _review(self):
        state = self.client.call('status')
        review = self.client.call('review', {'run_id': state['run_id']})
        self._emit('proposal_review', review)

    def confirm_review(self, review, accepted):
        if not self.ui_state()['ready'] or self.ui_state().get('core_instance') != review['core_instance']:
            return False
        if accepted and review.get('authorization_mode', 'single_run') != 'single_run':
            return self._command('confirm_trust', {key: review[key] for key in ('run_id', 'proposal_hash', 'review_token')} |
                                 {'mode': review['authorization_mode'], 'acknowledge': True})
        return self._command('confirm', {key: review[key] for key in ('run_id', 'proposal_hash', 'review_token')} |
                             {'accept': bool(accepted), 'unrestricted': bool(review['unrestricted'] and accepted)})

    def confirm_fix(self, token, confirmed):
        return False  # Only the complete proposal reader may submit confirmation.

    def set_guard_enabled(self, enabled):
        return self._command('guard', {'enabled': bool(enabled)})

    def cancel(self):
        return self._command('cancel')

    def revoke_model(self):
        return self._command('revoke_model')

    def revoke_trust(self):
        return self._command('revoke_trust')

    def forget_credential(self):
        return self._command('forget_credential')

    def show_settings(self, view='settings'):
        return self._submit(lambda: self._emit('settings', {**self.client.call('settings'), 'view': view}))

    def configure_model(self, **values):
        return self._command('model_configure', values)

    def set_preset(self, name, revision):
        return self._command('detection_preset', {'name': name, 'revision': revision})

    def redetect(self, revision):
        return self._command('detection_redetect', {'revision': revision})

    def save_preferences(self, values, revision):
        return self._command('preferences_save', {'values': values, 'revision': revision})

    def review_consent(self):
        return self._submit(lambda: self._emit('consent_review', self.client.call('consent_review')))

    def confirm_consent(self, review):
        if not self.ui_state()['ready'] or self.ui_state().get('core_instance') != review['core_instance']:
            return False
        return self._command('confirm_consent', {key: review[key] for key in
            ('revision', 'binding', 'review_token')} | {'allow_upload': True})

    def review_receipt(self, run_id):
        return self._submit(lambda: self._emit('receipt_review', self.client.call('receipt_review', {'run_id': run_id})))

    def confirm_receipt(self, review, note):
        if not self.ui_state()['ready'] or self.ui_state().get('core_instance') != review['core_instance']:
            return False
        return self._command('confirm_receipt', {key: review[key] for key in
            ('run_id', 'receipt_hash', 'review_token')} | {'note': note, 'acknowledge': True})

    def confirm_recovery(self, review, choice):
        if not self.ui_state()['ready'] or self.ui_state().get('core_instance') != review['core_instance']:
            return False
        return self._command('confirm_recovery', {key: review[key] for key in
            ('run_id', 'receipt_hash', 'review_token')} | {'choice': choice, 'acknowledge': True})

    def show_agent_records(self):
        return self._submit(lambda: self._emit('agent_records', self.client.call('records')))

    def show_task_detail(self, run_id):
        return self._submit(lambda: self._emit('task_detail', self.client.call('task_detail', {'run_id': run_id})))

    def show_profiles(self):
        return self._submit(lambda: self._emit('profiles', self.client.call('profiles')))

    def change_profile(self, operation, **values):
        if operation == 'save':
            values = {key: values.get(key) for key in ('document', 'profile_id', 'revision')} | {'imported': bool(values.get('imported'))}
        return self._command('profile_' + operation, values)

    def import_profile(self, path):
        def read():
            try:
                with Path(path).open('rb') as stream:
                    data = stream.read(256 * 1024 + 1)
                self._emit('profile_draft', parse_import(data.decode('utf-8')))
            except (OSError, ValueError, UnicodeError):
                self._emit('alert', {'title': '档案导入失败', 'message': '文件不可读取或不符合健康档案格式；现有档案未改变。'})
        return self._submit(read)

    def export_profile(self, profile_id, directory):
        def export():
            data = self.client.call('profile_export', {'profile_id': profile_id})
            path = Path(directory) / ('netcare-profile-' + uuid.uuid4().hex[:10] + '.json')
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            self._emit('alert', {'title': '网络档案已导出', 'message': str(path) + '\n包含原始目标地址，不含检测结果或授权。'})
        return self._submit(export)

    def close(self):
        self.stopped.set()
        if self.desktop:
            self.desktop.close()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
