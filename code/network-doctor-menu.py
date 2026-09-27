#!/usr/bin/env python3
"""Relay 3.0: offline diagnostics with optional accounts and local Pro history.

Importing this module does not create an app, read Keychain, load user config or
run system commands. The controller is testable without Cocoa or network access.
"""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
import webbrowser

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from relay_config import ConfigManager, DEFAULT_CONFIG
from relay.agent import NetworkAssuranceAgent
from relay.agent_store import AgentStore
from relay.engine import DetectionEngine, FixEngine
from relay.guard import GuardService, ProbeBudget
from relay.mac_events import MacNetworkEvents
from relay.profiles import HealthProfiles, ProfileConfig, expire_assessment, parse_import

try:
    from relay.commercial.client import AccountClient, CommercialConfig
except Exception:
    AccountClient = CommercialConfig = None
try:
    from relay.commercial.history import HistoryStore, redact
except Exception:
    HistoryStore = None
    redact = None

APP_NAME = 'NetCare'
APP_VERSION = '3.0.0'
DATA_DIR = Path.home() / 'Library/Application Support/Relay'


def resource_directory():
    if getattr(sys, 'frozen', False):
        bundled = Path(sys.executable).resolve().parent.parent / 'Resources'
        if bundled.is_dir():
            return bundled
    return SCRIPT_DIR


def commercial_config_path():
    # Packaged applications always use their pinned, shipped configuration.
    override = os.environ.get('RELAY_COMMERCIAL_CONFIG') if not getattr(sys, 'frozen', False) else None
    return Path(override).expanduser() if override else resource_directory() / 'relay-commercial.json'


def redacted_snapshot(snapshot):
    payload = {key: snapshot.get(key) for key in ('last_check', 'status', 'issues', 'check_errors')}
    if redact is not None:
        return redact(payload)
    # A broken optional module must never cause a raw diagnostic export.
    allowed = {'ok', 'error', 'unknown', 'warning', 'off', 'on', 'skipped', 'ignored', 'running'}
    status = {key: value for key, value in snapshot.get('status', {}).items()
              if key in {'wifi', 'vpn', 'dns', 'ipv6', 'proxy', 'reachability'}
              and isinstance(value, str) and value in allowed}
    return {'status': status, 'message': '脱敏组件不可用，仅显示基础状态；未包含网络地址或凭证。'}


def basic_report(snapshot):
    return json.dumps(redacted_snapshot(snapshot), ensure_ascii=False, indent=2)


def safe_message(error):
    text = str(error)[:400]
    return str(redact(text)) if redact is not None else '操作未完成，基础诊断仍可使用。'


from relay.desktop_services import DesktopServices, SerialWorker

class RelayController:
    def __init__(self, on_event, dispatch, data_dir=None, config_factory=ConfigManager,
                 engine_factory=DetectionEngine, fix_factory=FixEngine,
                 agent_factory=NetworkAssuranceAgent,
                 account_factory=AccountClient, commercial_loader=None,
                 history_factory=HistoryStore, open_browser=webbrowser.open,
                 monotonic=time.monotonic, login_poll_interval=3, start=True,
                 event_source_factory=MacNetworkEvents, guard_policy=None, guard_threaded=True):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.on_event, self.dispatch = on_event, dispatch
        self.config_factory, self.engine_factory, self.fix_factory = config_factory, engine_factory, fix_factory
        self.agent_factory = agent_factory
        self.monotonic = monotonic
        self.state_lock = threading.Lock()  # Held only while copying/updating in-memory UI state.
        self.preferences_lock = threading.Lock()
        self.stopped = threading.Event()
        self.config = self.engine = self.fix_engine = self.agent = None
        self.agent_store = None
        self.profiles = None
        self._profiles_error = ''
        self._diag_token = self._pending_repair = None
        self.event_source_factory = event_source_factory
        self.network_events = None
        self._guard_run = None
        self._guard_notified = ''
        self._cache = {
            'ready': False, 'busy': '', 'overall': 'unknown', 'summary': ['正在准备…'],
            'snapshot': {'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None},
            'preset': 'observe', 'config_path': str(self.data_dir / 'config.json'),
            'enabled_checks': None, 'repair_options': {}, 'scan_progress': None,
            'live_snapshot': None, 'repair': None,
            'agent': {'stage': 'initializing', 'health': 'unknown', 'next_step': 'initialize',
                      'incident_id': '', 'targets': [], 'observations': [], 'repairableIssues': []},
            'account': {'email': None, 'tier': 'Free', 'configured': False, 'origin': '',
                        'message': '基础诊断无需登录'},
            'login_active': False, 'history_error': '',
            'guard': {'enabled': False, 'phase': 'paused', 'changing': False,
                      'event_source': 'off', 'attention': False, 'error': ''},
        }
        self.diagnostics = SerialWorker('Relay diagnostics', 2)
        self.diagnostics.on_stopped = lambda: self.agent_store.close() if self.agent_store else None
        self.desktop = DesktopServices(on_event, dispatch, self.data_dir, lambda: self.ui_state()['snapshot'],
            account_factory=account_factory, commercial_loader=commercial_loader, history_factory=history_factory,
            open_browser=open_browser, monotonic=monotonic, login_poll_interval=login_poll_interval)
        self.accounts, self.histories, self.files = self.desktop.accounts, self.desktop.histories, self.desktop.files
        self.files.on_stopped = lambda: self.network_events.close() if self.network_events else None
        self.guard = GuardService(self._submit_guard, self._guard_state_changed,
                                  policy=guard_policy, clock=monotonic, threaded=guard_threaded)
        self.timer = None
        if start:
            self.start()

    def ui_state(self):
        with self.state_lock:
            state = copy.deepcopy(self._cache)
        state.update(self.desktop.ui_state())
        profile = state['agent'].get('profile')
        if profile:
            expire_assessment(profile)
            if profile['health'] == 'unknown' and state['agent'].get('health') == 'healthy':
                state['agent'].update(health='unknown', stage='needs_evidence', next_step='manual_check')
                state['overall'] = 'unknown'
        return state

    def _emit(self, event, payload=None):
        if not self.stopped.is_set():
            self.dispatch(self.on_event, event, payload or {})

    def _state_changed(self):
        self._emit('state')

    def _alert(self, title, message):
        self._emit('alert', {'title': title, 'message': message})

    def _log(self, event):
        if self.stopped.is_set():
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / 'Relay.log').open('a', encoding='utf-8') as stream:
                stream.write(f'[{datetime.now().isoformat(timespec="seconds")}] {event}\n')
        except OSError:
            pass

    def start(self):
        self._submit_diagnostic('正在准备', self._initialize_diagnostics)
        self.desktop.start(periodic=False)
        self.timer = threading.Thread(target=self._periodic, name='Relay account refresh', daemon=True)
        self.timer.start()

    def _periodic(self):
        # Account entitlement refresh is independent of user-triggered diagnostics.
        while not self.stopped.wait(900):
            self.refresh_account()

    def _reserve_diagnostic(self, name):
        with self.state_lock:
            if self.stopped.is_set() or self._diag_token is not None:
                return None
            token = uuid.uuid4().hex
            self._diag_token = token
            self._cache['busy'] = name
        self._state_changed()
        return token

    def _finish_diagnostic(self, token):
        with self.state_lock:
            if self._diag_token != token:
                return
            self._diag_token = None
            self._pending_repair = None
            self._cache['busy'] = ''
            self._cache['live_snapshot'] = None
            self._cache['scan_progress'] = None
        self._state_changed()

    def _submit_diagnostic(self, name, action):
        token = self._reserve_diagnostic(name)
        if token is None:
            return False
        def work():
            try:
                action()
            except Exception as exc:
                with self.state_lock:
                    self._cache['overall'] = 'unknown'
                    self._cache['summary'] = ['🟡 检查未完成：' + safe_message(exc)]
                self._alert(name + '未完成', safe_message(exc))
                self._log(name + '未完成（' + type(exc).__name__ + '）')
            finally:
                self._finish_diagnostic(token)
        if not self.diagnostics.submit(work):
            self._finish_diagnostic(token)
            return False
        return True

    def _initialize_diagnostics(self):
        path = self.data_dir / 'config.json'
        legacy = Path.home() / '.config/relay/config.json'
        # Preserve existing explicitly configured company settings on upgrade;
        # the legacy file remains untouched.
        source = legacy if not path.exists() and legacy.exists() else path
        config = self.config_factory(config_path=str(source))
        try:
            config.load()
            if source != path and not getattr(config, 'load_error', None):
                config.config_path = str(path)
                config.save()
        except Exception as exc:
            config = self.config_factory(config_path=str(path))
            config.config = copy.deepcopy(DEFAULT_CONFIG)
            config.load_error = '配置存储不可用：' + safe_message(exc)
        self.config = config
        try:
            self.agent_store = AgentStore(self.data_dir / 'agent')
            interrupted = self.agent_store.recover_interrupted()
            self.guard.schedule.restore_ages(self.agent_store.guard_check_ages())
        except Exception as exc:
            interrupted = []
            if self.agent_store:
                self.agent_store.close()
                self.agent_store = None
            self._log('Agent 处理记录不可用（' + type(exc).__name__ + '）')
        if self.agent_store:
            try:
                self.profiles = HealthProfiles(self.agent_store, config)
            except Exception:
                self._profiles_error = '健康档案不可用；修复执行已停用'
        self._rebuild_diagnostics()
        if any(run['stage'] == 'needs_reconciliation' for run in interrupted):
            self._alert('上次处理需要核对', '检测到中断的修改，已保留原配置和处理记录，未重新执行命令。')
        if self.config.get('guard.enabled', False) is True and not getattr(self.config, 'load_error', None):
            self._start_guard()
        self._publish_idle()
        self._log('NetCare ' + APP_VERSION + ' 启动；基础诊断独立于账号服务')

    def _rebuild_diagnostics(self):
        if self.agent:
            if self._guard_run:
                self.agent.cancel(self._guard_run)
            self.agent.revoke_all()
        self._guard_run = None
        self.runtime_config = ProfileConfig(self.config, self.profiles)
        self.engine = self.engine_factory(self.runtime_config)
        self.fix_engine = self.fix_factory(self.runtime_config, self.engine)
        self.agent = self.agent_factory(self.runtime_config, self.engine, self.fix_engine,
                                        store=self.agent_store, profiles=self.profiles)
        if self.agent_store is None or self._profiles_error:
            self.agent.journal_error = self._profiles_error or '处理记录不可用；修复执行已停用'
            self.agent.safety_error = self.agent.journal_error

    def _profiles_payload(self):
        return {'profiles': copy.deepcopy(self.profiles.profiles),
                'active_id': self.profiles.active['id'] if self.profiles.active else None,
                'assessment': self.profiles.evaluate(self.engine.snapshot())}

    def show_profiles(self):
        def read():
            if not self.profiles:
                self._alert('网络档案不可用', self._profiles_error or '本地记录不可用，无法读取或修改档案。')
                return
            self._emit('profiles', self._profiles_payload())
        return self._submit_diagnostic('读取网络档案', read)

    def change_profile(self, operation, **values):
        def change():
            if not self.profiles:
                raise RuntimeError('网络档案不可用')
            if operation == 'save':
                self.profiles.save(values['document'], values.get('profile_id'), values.get('revision'),
                                   source='user_import' if values.get('imported') else 'user')
            elif operation == 'activate':
                self.profiles.activate(values['profile_id'])
            elif operation == 'remove':
                self.profiles.remove(values['profile_id'])
            else:
                raise ValueError('未知的档案操作')
            self._rebuild_diagnostics()
            self._publish_idle()
            self.guard.changed()
            self._emit('profiles', {**self._profiles_payload(), 'replace': True})
        return self._submit_diagnostic('更新网络档案', change)

    def import_profile(self, path):
        def read():
            try:
                with Path(path).open('rb') as stream:
                    contents = stream.read(256 * 1024 + 1)
                if len(contents) > 256 * 1024:
                    raise ValueError('档案文件不能超过 256 KiB')
                self._emit('profile_draft', parse_import(contents.decode('utf-8')))
            except Exception as exc:
                self._alert('档案导入失败', safe_message(exc))
        return self.files.submit(read)

    def export_profile(self, profile_id, directory):
        def export():
            if not self.profiles:
                raise RuntimeError('网络档案不可用')
            document = self.profiles.document(profile_id)
            name = 'netcare-profile-' + uuid.uuid4().hex[:10] + '.json'
            path = Path(directory) / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
            self._alert('网络档案已导出', str(path) + '\n包含原始目标地址；不包含检测结果或授权。')
        return self._submit_diagnostic('导出网络档案', export)

    def _guard_state_changed(self):
        state = self.guard.schedule.state()
        source = self.network_events
        with self.state_lock:
            self._cache['guard'].update(state)
            if state['enabled'] and source:
                self._cache['guard']['event_source'] = 'native' if source.available else 'periodic_only'
        self._state_changed()

    def _start_guard(self):
        if self.stopped.is_set():
            return
        if self.agent_store is None or self._profiles_error:
            with self.state_lock:
                self._cache['guard']['error'] = '本地记录或健康档案不可用，守护未启动；仍可手动检测'
            return
        self.network_events = self.event_source_factory(self.guard.changed)
        available = self.network_events.start()
        if self.stopped.is_set():
            self.network_events.close()
            return
        with self.state_lock:
            self._cache['guard']['event_source'] = 'native' if available else 'periodic_only'
        self.guard.enable()

    def set_guard_enabled(self, enabled):
        enabled = bool(enabled)
        with self.state_lock:
            if not self._cache['ready'] or self.stopped.is_set() or self._cache['guard']['changing']:
                return False
            self._cache['guard'].update(changing=True, error='')
        if not enabled:
            self.guard.pause()
        self._state_changed()
        def save():
            try:
                with self.preferences_lock:
                    old = copy.deepcopy(self.config.config.get('guard', {'enabled': False}))
                    self.config.config.setdefault('guard', {})['enabled'] = enabled
                    try:
                        self.config.save()
                    except Exception:
                        self.config.config['guard'] = old
                        raise
                if self.network_events:
                    self.network_events.close()
                    self.network_events = None
                if enabled:
                    self._start_guard()
                else:
                    with self.state_lock:
                        self._cache['guard']['event_source'] = 'off'
            except Exception as exc:
                self.guard.pause()
                with self.state_lock:
                    self._cache['guard']['error'] = '守护设置未保存：' + safe_message(exc)
            finally:
                with self.state_lock:
                    self._cache['guard']['changing'] = False
                self._guard_state_changed()
        if not self.files.submit(save):
            with self.state_lock:
                self._cache['guard'].update(changing=False, error='守护设置未保存，请重试')
            self._state_changed()
            return False
        return True

    def _submit_guard(self, ticket):
        if not self.ui_state()['ready'] or not self.guard.schedule.valid(ticket):
            return False
        token = self._reserve_diagnostic('守护检查')
        if token is None:
            return False
        def work():
            budget = ProbeBudget(self.guard.schedule.policy,
                                 allowed=lambda: self.guard.schedule.valid(ticket) and not self.stopped.is_set(),
                                 clock=self.monotonic)
            failed = False
            try:
                if not self.guard.schedule.valid(ticket) or self.stopped.is_set():
                    return
                if not self.agent_store.reserve_guard_check(self.guard.schedule.policy.hourly_checks):
                    self.guard.schedule.restore_ages(self.agent_store.guard_check_ages())
                    budget.stop_reason = 'hourly_budget'
                    return
                if self._guard_run and self._guard_run.proposals:
                    self.agent.cancel(self._guard_run)
                run = self.agent.investigate(progress=self._scan_progress, budget=budget)
                self._guard_run = run
                self._publish_snapshot()
                probes = run.snapshot.get('status', {}).get('reachability_results', {})
                failed = (bool(run.snapshot.get('check_errors')) or run.health == 'degraded'
                          or bool(probes and not any(p.get('transport') == 'ok' for p in probes.values())))
                attention = bool(run.incident_id)
                with self.state_lock:
                    self._cache['guard'].update(attention=attention, proposal_ready=bool(run.proposals), error='')
                actionable = any(i[0] in ('high', 'medium') for i in run.issues)
                if self.guard.schedule.valid(ticket) and actionable and run.incident_id != self._guard_notified:
                    self._guard_notified = run.incident_id
                    self._emit('guard_notice', {'message': '检测到需要处理的网络问题，请打开 NetCare 查看。'})
                elif not attention:
                    self._guard_notified = ''
            except Exception as exc:
                failed = True
                with self.state_lock:
                    self._cache['guard']['error'] = '守护检查未完成：' + safe_message(exc)
            finally:
                self.guard.complete(ticket, failed, budget.metrics())
                self._finish_diagnostic(token)
        if not self.diagnostics.submit(work):
            self._finish_diagnostic(token)
            return False
        return True

    def _publish_idle(self):
        snapshot = {'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None}
        with self.state_lock:
            self._cache['guard'].update(attention=False, proposal_ready=False)
            self._cache.update(ready=True, overall='unknown', summary=['尚未检测'],
                snapshot=snapshot,
                repair_options={}, live_snapshot=None, scan_progress=None, repair=None,
                enabled_checks=[check.name for check in self.engine.checks],
                agent=self._agent_state(snapshot),
                preset=self.config.get('general.preset', 'observe'), config_path=self.config.config_path)
        self._state_changed()

    def _publish_snapshot(self):
        snapshot = self.engine.snapshot()
        options = self.agent.repair_options() if self.agent is not None else self.fix_engine.repair_options()
        with self.state_lock:
            self._cache['guard']['attention'] = bool(snapshot.get('issues'))
            if not snapshot.get('issues'):
                self._cache['guard']['proposal_ready'] = False
            self._cache.update(ready=True, snapshot=snapshot,
                repair_options=options, live_snapshot=None, scan_progress=None,
                enabled_checks=[check.name for check in self.engine.checks],
                agent=self._agent_state(snapshot),
                overall=self.engine.get_overall_status(), summary=self.engine.get_status_summary(),
                preset=self.config.get('general.preset', 'observe'),
                config_path=self.config.config_path)
        self._state_changed()
        # No entitlement, database, Keychain or account lock on this worker.
        self.desktop.record_snapshot(snapshot)
        return snapshot

    def _agent_state(self, snapshot):
        state = self.agent.assess(snapshot) if self.agent is not None else dict(self._cache['agent'])
        state['journal_error'] = (self.agent.safety_error or self.agent.journal_error) if self.agent else ''
        state['recovery_pending'] = self.agent.recovery_pending if self.agent else False
        state['profile_error'] = self._profiles_error or (self.agent.profile_error if self.agent else '')
        return state

    def show_agent_records(self):
        def read():
            if self.agent_store is None:
                self._alert('处理记录不可用', '请检查本地数据目录是否可写。基础检测仍可使用。')
                return
            from relay.agent_records import records_report
            self._emit('agent_records', records_report(self.agent_store.recent(), redact))
        return self._submit_diagnostic('读取处理记录', read)

    def _perform_check(self, notify=False):
        if self.engine is None:
            return
        if self.agent is not None:
            self.agent.observe(progress=self._scan_progress)
        else:
            self.engine.run_all(progress=self._scan_progress)
        snapshot = self._publish_snapshot()
        if notify:
            unknown = self.engine.get_overall_status() == 'unknown'
            title = '检测未完全确认' if unknown else '检测完成'
            self._alert(title, f"发现 {len(snapshot['issues'])} 条问题或提示。\n" + ('部分检查未完成或证据不足，请查看基础报告。' if unknown else '结果已更新。'))

    def check(self, notify=False):
        if not self.ui_state()['ready']:
            return False
        return self._submit_diagnostic('网络检测', lambda: self._perform_check(notify))

    def _scan_progress(self, event):
        with self.state_lock:
            self._cache['scan_progress'] = {key: value for key, value in event.items() if key != 'snapshot'}
            self._cache['live_snapshot'] = event.get('snapshot')
        self._state_changed()

    def _repair_progress(self, event):
        with self.state_lock:
            repair = self._cache['repair']
            if repair is None:
                return
            repair.update(event)
            if event.get('message'):
                repair['events'].append({'time': datetime.now().strftime('%H:%M:%S'),
                                         'message': event['message']})
        self._state_changed()

    def prepare_fix(self, issue_types=None):
        if not self.ui_state()['ready']:
            return False
        token = self._reserve_diagnostic('准备安全修复')
        if token is None:
            return False
        selected = frozenset(issue_types) if issue_types is not None else None
        with self.state_lock:
            self._cache['repair'] = {'token': token, 'phase': 'preparing', 'outcome': None,
                                     'events': [], 'plans': [], 'results': []}
        self._repair_progress({'phase': 'preparing', 'message': '重新检查所选问题，准备修复方案'})
        def prepare():
            waiting = False
            try:
                if self._guard_run and self._guard_run.proposals:
                    self.agent.cancel(self._guard_run)
                    self._guard_run = None
                run = self.agent.propose(selected, progress=self._scan_progress) if self.agent is not None else None
                snapshot = self._publish_snapshot()
                if run is None:
                    issues = [issue for issue in snapshot['issues'] if selected is None or issue[1] in selected]
                    plans = self.fix_engine.describe_fixes(issues) if issues else ['没有可安全自动修复的问题']
                else:
                    issues = list(run.issues)
                    plans = [plan for proposal in run.proposals for plan in proposal.plans] or ['没有可安全自动修复的问题']
                if plans == ['没有可安全自动修复的问题']:
                    self._repair_progress({'phase': 'finished', 'outcome': 'blocked',
                                           'message': '所选问题已消失或不满足自动修复条件，未修改网络。'})
                    self._alert('暂无安全自动修复动作', '请查看诊断报告。检查未知或证据不足时，不会修改网络。')
                    return
                with self.state_lock:
                    if self.stopped.is_set() or self._diag_token != token:
                        return
                    self._pending_repair = {'token': token, 'issues': issues, 'run': run}
                waiting = True
                self._repair_progress({'phase': 'awaiting_confirmation', 'plans': plans,
                                       'expires_at': run.proposals[0].expires_at if run else None,
                                       'message': '修复方案已就绪，等待确认'})
                self._emit('repair_confirmation', {'token': token, 'plans': plans})
            except Exception as exc:
                self._repair_progress({'phase': 'finished', 'outcome': 'blocked', 'message': safe_message(exc)})
                self._alert('修复准备失败', safe_message(exc))
            finally:
                if not waiting:
                    self._finish_diagnostic(token)
        if not self.diagnostics.submit(prepare):
            self._repair_progress({'phase': 'finished', 'outcome': 'blocked', 'message': '诊断队列暂不可用，请重新检测。'})
            self._finish_diagnostic(token)
            return False
        return True

    def confirm_fix(self, token, confirmed):
        with self.state_lock:
            pending = self._pending_repair
            if not pending or pending['token'] != token or self._diag_token != token:
                return False
            self._pending_repair = None
            if confirmed:
                self._cache['busy'] = '执行并验证修复'
        if not confirmed or self.stopped.is_set():
            if self.agent is not None and pending.get('run') is not None:
                self.diagnostics.submit(lambda: self.agent.cancel(pending['run']))
            self._repair_progress({'phase': 'finished', 'outcome': 'cancelled', 'message': '已取消，未修改网络。'})
            self._finish_diagnostic(token)
            return True
        self._state_changed()
        self._repair_progress({'phase': 'preflight', 'message': '开始执行获准的修复方案'})
        def apply():
            try:
                if self.agent is not None and pending.get('run') is not None:
                    grant = self.agent.authorize(pending['run'])
                    run = self.agent.execute(pending['run'], grant, progress=self._repair_progress)
                    results = run.results
                else:
                    results = self.fix_engine.fix_all(pending['issues'], progress=self._repair_progress)
                self._publish_snapshot()
                self._repair_progress({'phase': 'finished', 'results': results})
                self._alert('修复结果', '\n'.join(results))
            except Exception as exc:
                self._repair_progress({'phase': 'finished', 'outcome': 'failed', 'message': safe_message(exc)})
                self._alert('修复未完成', safe_message(exc))
            finally:
                self._finish_diagnostic(token)
        if not self.diagnostics.submit(apply):
            self._repair_progress({'phase': 'finished', 'outcome': 'blocked', 'message': '修复队列暂不可用，未执行修改。'})
            self._finish_diagnostic(token)
            return False
        return True

    def set_preset(self, name):
        if not self.ui_state()['ready']:
            return False
        def change():
            with self.preferences_lock:
                if not self.config.apply_preset(name):
                    raise ValueError('未知预设')
                self.config.save()
            self._rebuild_diagnostics()
            self._publish_idle()
            self.guard.changed()
            self._alert('预设已更新', '检测设置已保存，下次检测时生效。系统网络设置未作修改。')
        return self._submit_diagnostic('更新预设', change)

    def redetect(self):
        if not self.ui_state()['ready']:
            return False
        def detect():
            with self.preferences_lock:
                self.config.auto_detect()
                self.config.save()
            self._rebuild_diagnostics()
            self._perform_check()
            self._alert('环境检测完成', '检测设置已更新；系统 DNS、代理和 VPN 未作修改。')
        return self._submit_diagnostic('重新探测环境', detect)

    def refresh_account(self):
        return self.desktop.refresh_account()

    def login(self):
        return self.desktop.login()

    def cancel_login(self):
        return self.desktop.cancel_login()

    def logout(self):
        return self.desktop.logout()

    def open_account_page(self):
        return self.desktop.open_account_page()

    def show_history(self):
        return self.desktop.show_history()

    def compare_latest(self):
        return self.desktop.compare_latest()

    def prepare_pro_export(self):
        return self.desktop.prepare_pro_export()

    def export_pro(self, directory):
        return self.desktop.export_pro(directory)

    def export_basic(self, directory):
        return self.desktop.export_basic(directory)

    def close(self):
        self.stopped.set()
        self.guard.close()
        if self.agent:
            self.agent.revoke_all()
        self.desktop.close()
        for worker in (self.diagnostics, self.accounts, self.histories, self.files):
            worker.close()


def build_app_class():
    import rumps
    from relay.panel import DiagnosticPanel
    from PyObjCTools import AppHelper
    from AppKit import NSApplication, NSImage, NSOpenPanel, NSModalResponseOK, NSWorkspace
    from Foundation import NSURL

    class NetworkDoctorApp(rumps.App):
        def __init__(self):
            icon = resource_directory() / 'assets/menubar/relay-menubar-template.png'
            super().__init__(APP_NAME, icon=str(icon), template=True, title=None, quit_button=None)
            if not getattr(sys, 'frozen', False):
                app_icon = NSImage.alloc().initWithContentsOfFile_(str(SCRIPT_DIR / 'app_icon.png'))
                if app_icon is not None:
                    NSApplication.sharedApplication().setApplicationIconImage_(app_icon)
            self.controller = RelayController(self._on_event, AppHelper.callAfter, start=False)
            logo = resource_directory() / ('Relay.icns' if getattr(sys, 'frozen', False) else 'app_icon.png')
            self.panel = DiagnosticPanel(self.controller, logo, self.on_show_report)
            self.report_window = None
            self.agent_records_window = None
            self.profiles_window = None
            self._update_ui()
            self.controller.start()
            self.ui_timer = rumps.Timer(self._update_ui, 30)
            self.ui_timer.start()
            AppHelper.callAfter(self.panel.show)

        def _on_event(self, event, payload):
            if self.controller.stopped.is_set():
                return
            if event == 'state':
                self._update_ui()
            elif event == 'alert':
                if self.panel.sheet is not None and ('修复' in payload['title']):
                    self.panel.render(self.controller.ui_state())
                    return
                rumps.alert(title=payload['title'], message=payload['message'][:6500], ok='好的')
            elif event == 'repair_confirmation':
                self.panel.render(self.controller.ui_state())
            elif event == 'guard_notice':
                try:
                    rumps.notification('NetCare 网络守护', '网络状态需要关注', payload['message'])
                except Exception:
                    pass
            elif event == 'agent_records':
                from relay.report_window import ReportWindow
                from relay.agent_records import records_summary
                if self.agent_records_window is None:
                    logo = resource_directory() / ('Relay.icns' if getattr(sys, 'frozen', False) else 'app_icon.png')
                    self.agent_records_window = ReportWindow(logo, on_handle=lambda: self.panel.show(),
                                                             title='处理记录', summary_builder=records_summary)
                self.agent_records_window.show(payload)
            elif event in ('profiles', 'profile_draft'):
                from relay.profiles_window import ProfilesWindow
                if self.profiles_window is None:
                    self.profiles_window = ProfilesWindow(self.controller)
                if event == 'profiles':
                    self.profiles_window.show(payload)
                else:
                    self.profiles_window.import_draft(payload)
            elif event == 'choose_export_directory':
                directory = self._choose_directory()
                if directory:
                    self.controller.export_pro(directory)
            elif event == 'export_complete':
                rumps.alert(title='报告已导出', message='\n'.join(payload['paths']) + '\n\n导出内容已脱敏。', ok='好的')
            elif event == 'history':
                from relay.desktop_services import history_text
                rumps.alert(title='本地诊断历史', message=history_text(payload)[:6500], ok='好的')

        def _update_ui(self, _timer=None):
            state = self.controller.ui_state()  # In-memory only; never account.summary/allows.
            idle = state['ready'] and not state['busy']
            self.panel.render(state)
            if self.profiles_window is not None:
                self.profiles_window.update_state(state)
            self.menu.clear()
            self.menu.add(rumps.MenuItem('打开诊断面板', callback=lambda _: self.panel.show()))
            guard = state['guard']
            item = rumps.MenuItem('网络守护', callback=(lambda _: self.controller.set_guard_enabled(not guard['enabled']))
                                 if state['ready'] and not guard['changing'] else None)
            item.state = guard['enabled']
            self.menu.add(item)
            self.menu.add(None)
            if state['busy']:
                self.menu.add(rumps.MenuItem('⏳ ' + state['busy']))
            if state['overall'] == 'unknown' and state['snapshot'].get('last_check'):
                self.menu.add(rumps.MenuItem('🟡 状态尚未完全确认'))
            for line in state['summary']:
                self.menu.add(rumps.MenuItem(line) if line else None)
            self.menu.add(None)
            for title, callback, enabled in (
                ('🔍 一键检测', self.on_manual_check, idle),
                ('🔧 安全修复', self.on_fix, idle and bool(state['repair_options'])),
                ('处理记录', lambda _: self.controller.show_agent_records(), idle),
                ('网络档案', lambda _: self.controller.show_profiles(), idle),
                ('📋 查看基础脱敏报告 · Free', self.on_show_report, bool(state['snapshot'].get('last_check'))),
                ('📤 导出单次基础报告 · Free', self.on_export_basic, bool(state['snapshot'].get('last_check'))),
            ):
                self.menu.add(rumps.MenuItem(title, callback=callback if enabled else None))
            self.menu.add(None)
            account = state['account']
            label = account.get('email') or '未登录'
            account_menu = rumps.MenuItem(f"账号 · {account.get('tier', 'Free')} · {label}")
            account_menu.add(rumps.MenuItem(account.get('message', '基础诊断无需登录')[:100]))
            if state['login_active']:
                account_menu.add(rumps.MenuItem('等待浏览器确认…'))
                account_menu.add(rumps.MenuItem('取消本次登录', callback=lambda _: self.controller.cancel_login()))
            elif not account.get('email'):
                account_menu.add(rumps.MenuItem('通过浏览器登录', callback=lambda _: self.controller.login()))
            account_menu.add(rumps.MenuItem('账户、试用与订阅', callback=lambda _: self.controller.open_account_page()))
            account_menu.add(rumps.MenuItem('同步账号状态', callback=lambda _: self.controller.refresh_account()))
            if account.get('email') or state['login_active']:
                account_menu.add(rumps.MenuItem('退出账号', callback=lambda _: self.controller.logout()))
            self.menu.add(account_menu)
            pro = rumps.MenuItem('本地历史与诊断包 · Pro')
            pro.add(rumps.MenuItem('最近 20 条记录', callback=lambda _: self.controller.show_history()))
            pro.add(rumps.MenuItem('比较最近两次', callback=lambda _: self.controller.compare_latest()))
            pro.add(rumps.MenuItem('导出 HTML / JSON 诊断包', callback=lambda _: self.controller.prepare_pro_export()))
            if state['history_error']:
                pro.add(rumps.MenuItem('历史暂不可用；基础检测不受影响'))
            self.menu.add(pro)
            settings = rumps.MenuItem('⚙️ 检测设置')
            for label, preset in (('中性观察', 'observe'), ('公司档案', 'company'), ('个人代理', 'personal_proxy'), ('极简监测', 'minimal')):
                callback = (lambda _, value=preset: self.controller.set_preset(value)) if idle else None
                settings.add(rumps.MenuItem(label, callback=callback))
            settings.add(None)
            settings.add(rumps.MenuItem('重新探测环境', callback=(lambda _: self.controller.redetect()) if idle else None))
            settings.add(rumps.MenuItem('打开配置所在文件夹', callback=self.on_open_config))
            self.menu.add(settings)
            self.menu.add(None)
            self.menu.add(rumps.MenuItem('ℹ️ 关于 NetCare', callback=self.on_about))
            self.menu.add(rumps.MenuItem('退出 NetCare', callback=self.on_quit))

        def on_manual_check(self, _):
            self.controller.check(notify=True)

        def on_fix(self, _):
            self.controller.prepare_fix()

        def on_show_report(self, _):
            from relay.report_window import ReportWindow
            state = self.controller.ui_state()
            if not state['snapshot'].get('last_check'):
                rumps.alert(title='暂无报告', message='请先完成一次检测。', ok='好的')
                return
            if self.report_window is None:
                logo = resource_directory() / ('Relay.icns' if getattr(sys, 'frozen', False) else 'app_icon.png')
                self.report_window = ReportWindow(logo, on_handle=lambda: self.panel.show())
            self.report_window.show(redacted_snapshot(state['snapshot']))

        def _choose_directory(self):
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setAllowsMultipleSelection_(False)
            panel.setCanCreateDirectories_(True)
            panel.setPrompt_('导出到此文件夹')
            if panel.runModal() == NSModalResponseOK:
                return str(panel.URLs()[0].path())
            return None

        def on_export_basic(self, _):
            directory = self._choose_directory()
            if directory:
                self.controller.export_basic(directory)

        def on_open_config(self, _):
            path = self.controller.ui_state()['config_path']
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(path)])

        def on_about(self, _):
            rumps.alert(title=f'NetCare {APP_VERSION}', message='基础检测、菜单栏监测、脱敏报告和安全修复永久免费，无需账号联网。\n\nPro 提供本地历史、对比与诊断包；诊断数据默认不上传。\n\n数据目录：' + str(self.controller.data_dir), ok='关闭')

        def on_quit(self, _):
            busy = self.controller.ui_state()['busy']
            if '修复' in busy:
                rumps.alert(title='请先完成修复', message='当前修复需要完成验证或回滚后才能退出。', ok='好的')
                return
            if rumps.alert(title='退出 NetCare', message='退出后将停止网络守护和正在进行的登录。', ok='退出', cancel='取消'):
                self.controller.close()
                rumps.quit_application()

    return NetworkDoctorApp


def self_check():
    """Package smoke check: imports and assets only, no user state or commands."""
    from relay.checks import get_all_checks, get_load_errors
    expected = {'wifi', 'vpn', 'proxy', 'dns', 'ipv6', 'system_proxy', 'reachability'}
    checks = get_all_checks()
    icon = resource_directory() / 'assets/menubar/relay-menubar-template.png'
    errors = get_load_errors()
    optional = {}
    try:
        import importlib
        if sys.platform == 'darwin':
            for module in ('rumps', 'PyObjCTools.AppHelper', 'AppKit', 'SystemConfiguration',
                           'relay.panel', 'relay.report_window', 'relay.profiles_window',
                           'relay.agent', 'relay.guard', 'relay.mac_events', 'ServiceManagement',
                           'relay.remote_desktop', 'relay.desktop_services', 'relay.management_window', 'relay.lifecycle',
                           'relay.mac_service', 'relay_app', 'relay_core'):
                importlib.import_module(module)
            try:
                importlib.import_module('keyring.backends.macOS')
                optional['keychainBackend'] = True
            except Exception:
                optional['keychainBackend'] = False
        optional['accountModule'] = AccountClient is not None and HistoryStore is not None and redact is not None
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            key = Ed25519PrivateKey.generate()
            key.public_key().verify(key.sign(b'Relay package self-check'), b'Relay package self-check')
            optional['signatureCrypto'] = True
        except Exception:
            optional['signatureCrypto'] = False
    except Exception as exc:
        errors['dependencies'] = type(exc).__name__ + ': package dependency unavailable'
    if not icon.is_file():
        errors['icon'] = 'missing menu icon'
    if not (resource_directory() / 'app_icon.png').is_file():
        errors['workspaceIcon'] = 'missing workspace icon'
    if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
        try:
            from relay.mac_identity import native_bridge, production_requirement
            native_bridge().interface()
            optional['signedIPC'] = bool(production_requirement())
            optional['signedHelperPair'] = bool(native_bridge().trustedPair())
            if optional['signedIPC'] and not optional['signedHelperPair']:
                errors['helperIdentity'] = 'signed helper pair unavailable'
        except Exception:
            errors['nativeIPC'] = 'native protocol or signing identity unavailable'
        from relay.mac_service import PLIST
        if not (Path(sys.executable).resolve().parents[1] / 'Library/LaunchAgents' / PLIST).is_file():
            errors['backgroundService'] = 'missing bundled launch agent'
        for item in ('Library/LaunchDaemons/com.wangxinlei.relay.helper.plist', 'Library/LaunchServices/RelayHelper'):
            if not (Path(sys.executable).resolve().parents[1] / item).is_file():
                errors['helperBundle'] = 'missing bundled system repair service'
    missing = sorted(expected - set(checks))
    if missing:
        errors['plugins'] = ', '.join(missing)
    result = {'product': APP_NAME, 'version': APP_VERSION, 'ok': not errors, 'desktopEntry': 'agent',
              'plugins': sorted(checks), 'menuIcon': str(icon),
              'optionalAccountModule': AccountClient is not None,
              'optional': optional, 'errors': errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


def main():
    if '--self-check' in sys.argv:
        return self_check()
    args = sys.argv[1:]
    if '--legacy-desktop' not in args:
        from relay_app import main as agent_main
        if not any(arg.split('=', 1)[0] in ('--agent-desktop', '--core-service', '--lifecycle') for arg in args):
            args = ['--agent-desktop', *args]
        return agent_main(args)
    if args != ['--legacy-desktop']:
        print('Legacy desktop does not accept Agent or lifecycle options.', file=sys.stderr)
        return 2
    try:
        import setproctitle
        setproctitle.setproctitle(APP_NAME)
    except ImportError:
        pass
    build_app_class()().run()
    return 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
