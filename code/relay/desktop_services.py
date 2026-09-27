"""Optional account and local-history desktop work, independent of the core."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid
import webbrowser

try:
    from .commercial.client import AccountClient, CommercialConfig
except Exception:
    AccountClient = CommercialConfig = None
try:
    from .commercial.history import HistoryStore, redact
except Exception:
    HistoryStore = redact = None

APP_NAME, APP_VERSION = 'NetCare', '3.0.0'


def resource_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parents[1] / 'Resources'
    return Path(__file__).resolve().parents[1]


def commercial_config_path():
    override = os.environ.get('RELAY_COMMERCIAL_CONFIG') if not getattr(sys, 'frozen', False) else None
    return Path(override).expanduser() if override else resource_directory() / 'relay-commercial.json'


def redacted_snapshot(snapshot):
    payload = {key: snapshot.get(key) for key in ('last_check', 'status', 'issues', 'check_errors')}
    if redact is not None:
        return redact(payload)
    allowed = {'ok', 'error', 'unknown', 'warning', 'off', 'on', 'skipped', 'ignored', 'running'}
    return {'status': {key: value for key, value in snapshot.get('status', {}).items()
            if key in {'wifi', 'vpn', 'dns', 'ipv6', 'proxy', 'reachability'}
            and isinstance(value, str) and value in allowed},
            'message': '脱敏组件不可用，仅包含基础状态。'}


def safe_message(error):
    return str(redact(str(error)[:400])) if redact is not None else '操作未完成，基础诊断仍可使用。'


def history_text(payload):
    from .presentation import SECTIONS
    names = {('proxy_app' if key == 'proxy' else 'proxy' if key == 'system_proxy' else key): title
             for key, title, _ in SECTIONS}
    values = {'ok': '正常', 'warning': '需要关注', 'error': '异常', 'unknown': '未确认',
              'on': '开启', 'off': '关闭', 'running': '运行中', 'skipped': '未检测', 'ignored': '未检测'}
    def value_text(value):
        return values.get(value, value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    def stamp(value):
        return datetime.fromtimestamp(value).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    if 'records' not in payload:
        if not payload.get('available'):
            return payload.get('message', '暂无可比较的记录')
        lines = ['最近两次不同诊断的对比']
        if payload.get('before') is not None and payload.get('after') is not None:
            lines += [stamp(payload['before']) + ' → ' + stamp(payload['after']), '']
        for item in payload.get('changes', []):
            lines += [names.get(item['field'], '问题列表' if item['field'] == 'issues' else str(item['field'])),
                      '之前：' + value_text(item['before']), '之后：' + value_text(item['after']), '']
        return '\n'.join(lines) if payload.get('changes') else '没有检测项或问题变化'
    lines = []
    for row in payload['records']:
        lines.append(stamp(row['createdAt']) + ' · 记录 #' + str(row['id']))
        status = row.get('status', {})
        lines.extend(title + '：' + value_text(status[key]) for key, title in names.items() if key in status)
        issues = row.get('issues', [])
        lines.append(f'记录问题：{len(issues)} 项')
        lines.extend(str(issue[2]) for issue in issues if isinstance(issue, (list, tuple)) and len(issue) >= 3)
        errors = row.get('check_errors') or {}
        if errors:
            lines.append('未完成的检查：' + '、'.join(names.get(key, key) for key in errors))
        lines.append('')
    return '\n'.join(lines) or '暂无本地诊断历史'


class SerialWorker:
    """Daemon worker with bounded pending work; never join it on the UI thread."""
    def __init__(self, name, max_pending=8):
        self.queue = queue.Queue(maxsize=max_pending)
        self.stopped = threading.Event()
        self.on_stopped = None
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def submit(self, callback):
        if self.stopped.is_set():
            return False
        try:
            self.queue.put_nowait(callback)
            return True
        except queue.Full:
            return False

    def _run(self):
        try:
            self._work_loop()
        finally:
            if self.on_stopped:
                self.on_stopped()

    def _work_loop(self):
        while not self.stopped.is_set():
            try:
                callback = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if callback is not None and not self.stopped.is_set():
                    callback()
            except Exception:
                # Every public controller action reports its own error; one
                # unexpected optional task must not terminate the worker.
                pass
            finally:
                self.queue.task_done()

    def close(self):
        self.stopped.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


class DesktopServices:
    def __init__(self, on_event, dispatch, data_dir, get_snapshot, *,
                 account_factory=AccountClient, commercial_loader=None, history_factory=HistoryStore,
                 open_browser=webbrowser.open, monotonic=time.monotonic, login_poll_interval=3):
        self.on_event, self.dispatch, self.data_dir = on_event, dispatch, Path(data_dir)
        self.get_snapshot = get_snapshot
        self.account_factory, self.commercial_loader, self.history_factory = account_factory, commercial_loader, history_factory
        self.open_browser, self.monotonic, self.login_poll_interval = open_browser, monotonic, login_poll_interval
        self.account = self.history = None
        self.state_lock = threading.Lock()
        self.stopped = threading.Event()
        self._login_cancel = None
        self._auth_generation = 0
        self._refresh_pending = self._logout_pending = False
        self._cache = {'account': {'email': None, 'tier': 'Free', 'configured': False, 'origin': '',
            'message': '基础诊断无需登录'}, 'login_active': False, 'history_error': ''}
        self.accounts = SerialWorker('NetCare account', 4)
        self.histories = SerialWorker('NetCare history', 8)
        self.files = SerialWorker('NetCare report files', 4)
        self.timer = None
        self._snapshot_key = None

    def start(self, periodic=True):
        self.accounts.submit(self._initialize_account)
        if periodic and self.timer is None:
            self.timer = threading.Thread(target=self._periodic, name='NetCare account refresh', daemon=True)
            self.timer.start()

    def _periodic(self):
        while not self.stopped.wait(900):
            self.refresh_account()

    def ui_state(self):
        with self.state_lock:
            return {**copy.deepcopy(self._cache), 'logout_pending': self._logout_pending,
                    'refresh_pending': self._refresh_pending}

    def _emit(self, event, payload=None):
        if not self.stopped.is_set():
            self.dispatch(self.on_event, event, payload or {})

    def _state_changed(self):
        self._emit('state')

    def _alert(self, title, message):
        self._emit('alert', {'title': title, 'message': message})

    def record_snapshot(self, snapshot):
        if self.stopped.is_set() or not snapshot.get('last_check'):
            return False
        return self.histories.submit(lambda value=copy.deepcopy(snapshot): self._record_history(value))

    def observe_state(self, state):
        snapshot = state.get('snapshot', {})
        key = (state.get('core_instance'), str(snapshot.get('last_check')))
        with self.state_lock:
            eligible = self._cache['account'].get('tier') == 'Pro'
        if not eligible or not state.get('ready') or not snapshot.get('last_check') or key == self._snapshot_key:
            return
        if self.record_snapshot(snapshot):
            self._snapshot_key = key

    def _account_summary(self):
        if self.account is None:
            return {'email': None, 'tier': 'Free', 'message': '账号服务尚未开放；基础诊断无需登录'}
        try:
            return self.account.summary()
        except Exception:
            return {'email': None, 'tier': 'Free', 'message': '账号状态不可用；基础诊断无需登录'}

    def _cache_account(self, message=None, generation=None):
        # Called only on optional background workers. summary/allows can wait
        # for an HTTP request holding AccountClient's lock.
        summary = self._account_summary()
        if message is not None:
            summary['message'] = message
        config = self.account.config if self.account is not None else None
        summary.update(configured=bool(config and config.configured), origin=config.origin if config else '')
        with self.state_lock:
            if generation is not None and generation != self._auth_generation:
                return
            if summary.get('tier') == 'Pro' and self._cache['account'].get('tier') != 'Pro':
                self._snapshot_key = None
            self._cache['account'] = summary
        self._state_changed()

    def _initialize_account(self):
        try:
            if self.account_factory is None or (CommercialConfig is None and self.commercial_loader is None):
                raise RuntimeError('账号组件暂不可用，基础诊断仍可使用。')
            commercial = self.commercial_loader() if self.commercial_loader else CommercialConfig.load(commercial_config_path())
            if self.stopped.is_set():
                return
            self.account = self.account_factory(commercial)
            self.account.restore()
            if self.stopped.is_set():
                return
            self._cache_account()
            if commercial.configured:
                self.account.refresh_entitlement()
            self._cache_account()
        except Exception as exc:
            self._cache_account(safe_message(exc))

    def refresh_account(self):
        with self.state_lock:
            if self.stopped.is_set() or self._refresh_pending or self._cache['login_active'] or self._logout_pending or self.account is None:
                return False
            self._refresh_pending = True
        def refresh():
            try:
                if self.stopped.is_set():
                    return
                self.account.refresh_entitlement()
                self._cache_account()
            except Exception as exc:
                self._cache_account(safe_message(exc))
            finally:
                with self.state_lock:
                    self._refresh_pending = False
                self._state_changed()
        if not self.accounts.submit(refresh):
            with self.state_lock:
                self._refresh_pending = False
            return False
        return True

    def login(self):
        with self.state_lock:
            if self.stopped.is_set() or self._cache['login_active'] or self._logout_pending:
                return False
            if not self._cache['account']['configured'] or self.account is None:
                configured = False
            else:
                configured = True
                if self._cache['account'].get('email'):
                    return False
                self._auth_generation += 1
                generation = self._auth_generation
                cancel = self._login_cancel = threading.Event()
                self._cache['login_active'] = True
        if not configured:
            self._alert('账号服务尚未开放', '基础检测、报告和安全修复无需账号。')
            return False
        self._state_changed()
        if not self.accounts.submit(lambda: self._login_flow(generation, cancel)):
            with self.state_lock:
                self._cache['login_active'] = False
            return False
        return True

    def _login_flow(self, generation, cancel):
        try:
            if cancel.is_set() or self.stopped.is_set():
                return
            attempt = self.account.begin_login()
            if cancel.is_set() or self.stopped.is_set():
                return
            if self.open_browser(attempt['authorizeUrl']) is False:
                raise RuntimeError('未能打开浏览器，请重试登录。')
            deadline = self.monotonic() + min(300, max(1, int(attempt.get('expiresIn', 300))))
            while not cancel.wait(self.login_poll_interval) and not self.stopped.is_set() and self.monotonic() < deadline:
                if self.account.poll_login(attempt):
                    if not cancel.is_set() and not self.stopped.is_set():
                        self._cache_account(generation=generation)
                        self._alert('登录完成', '账号状态已同步，基础诊断继续在本机运行。')
                    return
            if not cancel.is_set() and not self.stopped.is_set():
                self._cache_account('登录已超时，请重新开始。', generation)
        except Exception as exc:
            if not cancel.is_set() and not self.stopped.is_set():
                self._cache_account(safe_message(exc), generation)
                self._alert('登录未完成', safe_message(exc))
        finally:
            if cancel.is_set() or self.stopped.is_set():
                # A poll may have succeeded while Cancel/Logout was clicked.
                # Clear/revoke that late session on this same serial worker.
                try:
                    self.account.logout()
                except Exception:
                    pass
                self._cache_account('登录已取消；基础诊断无需登录。', generation)
            with self.state_lock:
                if self._login_cancel is cancel:
                    self._cache['login_active'] = False
                    self._login_cancel = None
            self._state_changed()

    def cancel_login(self):
        with self.state_lock:
            cancel = self._login_cancel
        if cancel:
            cancel.set()
            return True
        return False

    def logout(self):
        with self.state_lock:
            if self.stopped.is_set() or self._logout_pending or self.account is None:
                return False
            self._auth_generation += 1
            generation = self._auth_generation
            self._logout_pending = True
            if self._login_cancel:
                self._login_cancel.set()
            self._cache['account']['message'] = '正在退出账号…'
        self._state_changed()
        def leave():
            try:
                message = self.account.logout()
                self._cache_account(message, generation)
            except Exception as exc:
                self._cache_account(safe_message(exc), generation)
                self._alert('退出账号需要关注', safe_message(exc))
            finally:
                with self.state_lock:
                    self._logout_pending = False
                self._state_changed()
        if not self.accounts.submit(leave):
            with self.state_lock:
                self._logout_pending = False
            return False
        return True

    def open_account_page(self):
        origin = self.ui_state()['account'].get('origin')
        if not origin:
            self._alert('账号服务尚未开放', '基础诊断无需登录；暂不能在线购买或开启试用。')
            return False
        return self.files.submit(lambda: self.open_browser(origin + '/account/'))

    def _get_history(self):
        if self.history is None:
            if self.history_factory is None:
                raise RuntimeError('本地历史组件不可用，基础诊断仍可使用。')
            self.history = self.history_factory(self.data_dir / 'history.sqlite3')
        return self.history

    def _allowed(self, feature):
        try:
            allowed = bool(self.account and self.account.allows(feature))
        except Exception:
            allowed = False
        return allowed

    def _record_history(self, snapshot):
        if self.stopped.is_set() or not self._allowed('history'):
            return
        try:
            self._get_history().record(snapshot)
            with self.state_lock:
                self._cache['history_error'] = ''
        except Exception as exc:
            with self.state_lock:
                self._cache['history_error'] = safe_message(exc)
            self._state_changed()

    def _pro_action(self, feature, action):
        def work():
            if self.stopped.is_set():
                return
            if not self._allowed(feature):
                self._cache_account()
                self._alert('此功能需要 NetCare Pro', '请在账户页查看试用或订阅。已有本地历史会保留；基础诊断和单次报告继续免费。')
                return
            try:
                action()
            except Exception as exc:
                self._alert('本地历史操作未完成', safe_message(exc))
        accepted = self.histories.submit(work)
        if not accepted:
            self._alert('历史操作繁忙', '请稍后重试；基础诊断继续运行。')
        return accepted

    def show_history(self):
        def show():
            rows = self._get_history().list(20)
            self._emit('history', {'records': rows, 'redacted': True})
        return self._pro_action('history', show)

    def compare_latest(self):
        def show():
            result = self._get_history().compare_latest()
            self._emit('history', {**result, 'redacted': True})
        return self._pro_action('compare', show)

    def prepare_pro_export(self):
        return self._pro_action('export_bundle', lambda: self._emit('choose_export_directory', {'kind': 'pro'}))

    def export_pro(self, directory):
        def export():
            paths = self._get_history().export(directory)
            self._emit('export_complete', {'paths': [str(path) for path in paths]})
        return self._pro_action('export_bundle', export)

    def export_basic(self, directory):
        snapshot = self.get_snapshot()
        if not snapshot.get('last_check'):
            self._alert('暂无报告', '请先完成一次检测。')
            return False
        def export():
            try:
                payload = {'product': APP_NAME, 'version': APP_VERSION, 'schema': 'basic-diagnostic-v1',
                           'redacted': True, 'snapshot': redacted_snapshot(snapshot)}
                name = 'relay-basic-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6] + '.json'
                path = Path(directory) / name
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                self._emit('export_complete', {'paths': [str(path)]})
            except Exception as exc:
                self._alert('基础报告导出失败', safe_message(exc))
        return self.files.submit(export)


    def close(self):
        self.stopped.set()
        self.cancel_login()
        for worker in (self.accounts, self.histories, self.files):
            worker.close()
