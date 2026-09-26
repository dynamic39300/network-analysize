#!/usr/bin/env python3
"""Relay 3.0: offline diagnostics with optional accounts and local Pro history.

Importing this module does not create an app, read Keychain, load user config or
run system commands. The controller is testable without Cocoa or network access.
"""
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

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from relay_config import ConfigManager, DEFAULT_CONFIG
from relay.engine import DetectionEngine, FixEngine

try:
    from relay.commercial.client import AccountClient, CommercialConfig
except Exception:
    AccountClient = CommercialConfig = None
try:
    from relay.commercial.history import HistoryStore, redact
except Exception:
    HistoryStore = None
    redact = None

APP_NAME = 'Relay'
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


class SerialWorker:
    """Daemon worker with bounded pending work; never join it on the UI thread."""
    def __init__(self, name, max_pending=8):
        self.queue = queue.Queue(maxsize=max_pending)
        self.stopped = threading.Event()
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


class RelayController:
    def __init__(self, on_event, dispatch, data_dir=None, config_factory=ConfigManager,
                 engine_factory=DetectionEngine, fix_factory=FixEngine,
                 account_factory=AccountClient, commercial_loader=None,
                 history_factory=HistoryStore, open_browser=webbrowser.open,
                 monotonic=time.monotonic, login_poll_interval=3, start=True):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.on_event, self.dispatch = on_event, dispatch
        self.config_factory, self.engine_factory, self.fix_factory = config_factory, engine_factory, fix_factory
        self.account_factory, self.commercial_loader = account_factory, commercial_loader
        self.history_factory, self.open_browser, self.monotonic = history_factory, open_browser, monotonic
        self.login_poll_interval = login_poll_interval
        self.state_lock = threading.Lock()  # Held only while copying/updating in-memory UI state.
        self.stopped = threading.Event()
        self.config = self.engine = self.fix_engine = None
        self.account = self.history = None
        self._diag_token = self._pending_repair = None
        self._login_cancel = None
        self._auth_generation = 0
        self._refresh_pending = False
        self._logout_pending = False
        self._cache = {
            'ready': False, 'busy': '', 'overall': 'unknown', 'summary': ['状态检测中…'],
            'snapshot': {'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None},
            'preset': 'observe', 'interval': 30, 'config_path': str(self.data_dir / 'config.json'),
            'account': {'email': None, 'tier': 'Free', 'configured': False, 'origin': '',
                        'message': '基础诊断无需登录'},
            'login_active': False, 'history_error': '',
        }
        self.diagnostics = SerialWorker('Relay diagnostics', 2)
        self.accounts = SerialWorker('Relay account', 4)
        self.histories = SerialWorker('Relay history', 8)
        self.files = SerialWorker('Relay basic reports', 4)
        self.timer = None
        if start:
            self.start()

    def ui_state(self):
        with self.state_lock:
            return copy.deepcopy(self._cache)

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
        self._submit_diagnostic('启动检测', self._initialize_diagnostics)
        self.accounts.submit(self._initialize_account)
        self.timer = threading.Thread(target=self._periodic, name='Relay scheduler', daemon=True)
        self.timer.start()

    def _periodic(self):
        next_account = self.monotonic() + 900
        while not self.stopped.wait(self.ui_state()['interval']):
            self.check()
            if self.monotonic() >= next_account:
                self.refresh_account()
                next_account = self.monotonic() + 900

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
        self.engine = self.engine_factory(config)
        self.fix_engine = self.fix_factory(config, self.engine)
        self._perform_check()
        self._log('Relay ' + APP_VERSION + ' 启动；基础诊断独立于账号服务')

    def _publish_snapshot(self):
        snapshot = self.engine.snapshot()
        with self.state_lock:
            self._cache.update(ready=True, snapshot=snapshot,
                overall=self.engine.get_overall_status(), summary=self.engine.get_status_summary(),
                preset=self.config.get('general.preset', 'observe'),
                interval=self.config.get('general.check_interval', 30), config_path=self.config.config_path)
        self._state_changed()
        # No entitlement, database, Keychain or account lock on this worker.
        self.histories.submit(lambda: self._record_history(snapshot))
        return snapshot

    def _perform_check(self, notify=False):
        if self.engine is None:
            return
        self.engine.run_all()
        snapshot = self._publish_snapshot()
        if notify:
            unknown = self.engine.get_overall_status() == 'unknown'
            title = '检测未完全确认' if unknown else '检测完成'
            self._alert(title, f"发现 {len(snapshot['issues'])} 条问题或提示。\n" + ('部分检查未完成或证据不足，请查看基础报告。' if unknown else '结果已更新。'))

    def check(self, notify=False):
        if not self.ui_state()['ready']:
            return False
        return self._submit_diagnostic('网络检测', lambda: self._perform_check(notify))

    def prepare_fix(self):
        if not self.ui_state()['ready']:
            return False
        token = self._reserve_diagnostic('准备安全修复')
        if token is None:
            return False
        def prepare():
            waiting = False
            try:
                self.engine.run_all()
                snapshot = self._publish_snapshot()
                plans = self.fix_engine.describe_fixes(snapshot['issues'])
                if plans == ['没有可安全自动修复的问题']:
                    self._alert('暂无安全自动修复动作', '请查看诊断报告。检查未知或证据不足时，不会修改网络。')
                    return
                with self.state_lock:
                    if self.stopped.is_set() or self._diag_token != token:
                        return
                    self._pending_repair = {'token': token, 'issues': snapshot['issues']}
                waiting = True
                self._emit('repair_confirmation', {'token': token, 'plans': plans})
            except Exception as exc:
                self._alert('修复准备失败', safe_message(exc))
            finally:
                if not waiting:
                    self._finish_diagnostic(token)
        if not self.diagnostics.submit(prepare):
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
            self._finish_diagnostic(token)
            return True
        self._state_changed()
        def apply():
            try:
                results = self.fix_engine.fix_all(pending['issues'])
                self._publish_snapshot()
                self._alert('修复结果', '\n'.join(results))
            except Exception as exc:
                self._alert('修复未完成', safe_message(exc))
            finally:
                self._finish_diagnostic(token)
        if not self.diagnostics.submit(apply):
            self._finish_diagnostic(token)
            return False
        return True

    def set_preset(self, name):
        if not self.ui_state()['ready']:
            return False
        def change():
            if not self.config.apply_preset(name):
                raise ValueError('未知预设')
            self.config.save()
            self.engine = self.engine_factory(self.config)
            self.fix_engine = self.fix_factory(self.config, self.engine)
            self._perform_check()
            self._alert('预设已更新', '已保存并重新检测。切换预设本身不会修改系统网络。')
        return self._submit_diagnostic('更新预设', change)

    def redetect(self):
        if not self.ui_state()['ready']:
            return False
        def detect():
            self.config.auto_detect()
            self.config.save()
            self.engine = self.engine_factory(self.config)
            self.fix_engine = self.fix_factory(self.config, self.engine)
            self._perform_check()
            self._alert('环境检测完成', '检测设置已更新；系统 DNS、代理和 VPN 未作修改。')
        return self._submit_diagnostic('重新探测环境', detect)

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
                self._alert('此功能需要 Relay Pro', '请在账户页查看试用或订阅。已有本地历史会保留；基础诊断和单次报告继续免费。')
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
            if not rows:
                message = '暂无记录。Pro 有效期间检测到的不同结果会在本机保留 30 天。'
            else:
                lines = []
                for row in rows:
                    stamp = datetime.fromtimestamp(row['createdAt']).astimezone().strftime('%m-%d %H:%M:%S')
                    lines.append(f"{stamp} · {len(row.get('issues', []))} 条问题/提示 · 记录 #{row['id']}")
                message = '\n'.join(lines)
            self._alert('最近 20 条本地诊断记录', message)
        return self._pro_action('history', show)

    def compare_latest(self):
        def show():
            result = self._get_history().compare_latest()
            self._alert('最近两次诊断对比', json.dumps(result, ensure_ascii=False, indent=2)[:6000])
        return self._pro_action('compare', show)

    def prepare_pro_export(self):
        return self._pro_action('export_bundle', lambda: self._emit('choose_export_directory', {'kind': 'pro'}))

    def export_pro(self, directory):
        def export():
            paths = self._get_history().export(directory)
            self._emit('export_complete', {'paths': [str(path) for path in paths]})
        return self._pro_action('export_bundle', export)

    def export_basic(self, directory):
        snapshot = self.ui_state()['snapshot']
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
        for worker in (self.diagnostics, self.accounts, self.histories, self.files):
            worker.close()


def build_app_class():
    import rumps
    from PyObjCTools import AppHelper
    from AppKit import NSOpenPanel, NSModalResponseOK, NSWorkspace
    from Foundation import NSURL

    class NetworkDoctorApp(rumps.App):
        def __init__(self):
            icon = resource_directory() / 'assets/menubar/relay-menubar-template.png'
            super().__init__(APP_NAME, icon=str(icon), template=True, title=None, quit_button=None)
            self.controller = RelayController(self._on_event, AppHelper.callAfter)
            self._update_ui()

        def _on_event(self, event, payload):
            if self.controller.stopped.is_set():
                return
            if event == 'state':
                self._update_ui()
            elif event == 'alert':
                rumps.alert(title=payload['title'], message=payload['message'][:6500], ok='好的')
            elif event == 'repair_confirmation':
                response = rumps.alert(title='确认安全修复', message='\n'.join('• ' + p for p in payload['plans'])
                    + '\n\n将重新检查、保存涉及字段并验证连通性。失败时尝试回滚，并如实报告结果。', ok='执行修复', cancel='取消')
                self.controller.confirm_fix(payload['token'], bool(response))
            elif event == 'choose_export_directory':
                directory = self._choose_directory()
                if directory:
                    self.controller.export_pro(directory)
            elif event == 'export_complete':
                rumps.alert(title='报告已导出', message='\n'.join(payload['paths']) + '\n\n导出内容已脱敏。', ok='好的')

        def _update_ui(self):
            state = self.controller.ui_state()  # In-memory only; never account.summary/allows.
            idle = state['ready'] and not state['busy']
            self.menu.clear()
            if state['busy']:
                self.menu.add(rumps.MenuItem('⏳ ' + state['busy']))
            if state['overall'] == 'unknown':
                self.menu.add(rumps.MenuItem('🟡 状态尚未完全确认'))
            for line in state['summary']:
                self.menu.add(rumps.MenuItem(line) if line else None)
            self.menu.add(None)
            for title, callback, enabled in (
                ('🔍 一键检测', self.on_manual_check, idle),
                ('🔧 安全修复', self.on_fix, idle),
                ('📋 查看基础脱敏报告 · Free', self.on_show_report, state['ready']),
                ('📤 导出单次基础报告 · Free', self.on_export_basic, state['ready']),
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
            self.menu.add(rumps.MenuItem('ℹ️ 关于 Relay', callback=self.on_about))
            self.menu.add(rumps.MenuItem('退出 Relay', callback=self.on_quit))

        def on_manual_check(self, _):
            self.controller.check(notify=True)

        def on_fix(self, _):
            self.controller.prepare_fix()

        def on_show_report(self, _):
            state = self.controller.ui_state()
            if not state['snapshot'].get('last_check'):
                rumps.alert(title='暂无报告', message='请先完成一次检测。', ok='好的')
                return
            rumps.alert(title='单次基础脱敏报告 · Free', message=basic_report(state['snapshot'])[:6500], ok='关闭')

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
            rumps.alert(title=f'Relay {APP_VERSION}', message='基础检测、菜单栏监测、脱敏报告和安全修复永久免费，无需账号联网。\n\nPro 提供本地历史、对比与诊断包；诊断数据默认不上传。\n\n数据目录：' + str(self.controller.data_dir), ok='关闭')

        def on_quit(self, _):
            busy = self.controller.ui_state()['busy']
            if '修复' in busy:
                rumps.alert(title='请先完成修复', message='当前修复需要完成验证或回滚后才能退出。', ok='好的')
                return
            if rumps.alert(title='退出 Relay', message='退出后将停止菜单栏监测和正在进行的登录。', ok='退出', cancel='取消'):
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
    try:
        import importlib
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        if AccountClient is None or HistoryStore is None or redact is None:
            raise ImportError('commercial module unavailable')
        key = Ed25519PrivateKey.generate()
        key.public_key().verify(key.sign(b'Relay package self-check'), b'Relay package self-check')
        if sys.platform == 'darwin':
            for module in ('rumps', 'keyring.backends.macOS', 'PyObjCTools.AppHelper', 'AppKit'):
                importlib.import_module(module)
    except Exception as exc:
        errors['dependencies'] = type(exc).__name__ + ': package dependency unavailable'
    if not icon.is_file():
        errors['icon'] = 'missing menu icon'
    missing = sorted(expected - set(checks))
    if missing:
        errors['plugins'] = ', '.join(missing)
    result = {'product': APP_NAME, 'version': APP_VERSION, 'ok': not errors,
              'plugins': sorted(checks), 'menuIcon': str(icon),
              'optionalAccountModule': AccountClient is not None, 'errors': errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


def main():
    if '--self-check' in sys.argv:
        return self_check()
    try:
        import setproctitle
        setproctitle.setproctitle(APP_NAME)
    except ImportError:
        pass
    build_app_class()().run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
