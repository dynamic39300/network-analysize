"""Native connected desktop entry. Closing this client does not stop the core."""
from pathlib import Path
import os
import sys
import threading

from .desktop_services import DesktopServices, history_text, redacted_snapshot


def build_connected_app_class(data_dir, lifecycle=None, desktop_factory=DesktopServices):
    if sys.platform != 'darwin':
        raise NotImplementedError('The connected native desktop is currently macOS only')
    import rumps
    from PyObjCTools import AppHelper
    from .overview_page import OverviewPage
    from .workspace_window import WorkspaceWindow
    from .profiles_window import ProfilesWindow
    from .proposal_window import ProposalWindow
    from .receipt_window import ReceiptWindow
    from .records_window import RecordsWindow
    from .management_window import PrivacyWindow, SettingsWindow
    from .remote_controller import RemoteController
    from .report_window import ReportWindow
    from .task_details import detail_text

    assets = Path(__file__).resolve().parents[1]

    class ConnectedApp(rumps.App):
        def __init__(self):
            super().__init__('NetCare', icon=str(assets / 'assets/menubar/relay-menubar-template.png'),
                             template=True, title=None, quit_button=None)
            self.controller = RemoteController(data_dir, self.event, AppHelper.callAfter)
            self.desktop = desktop_factory(self.event, AppHelper.callAfter, data_dir,
                                           lambda: self.controller.ui_state()['snapshot'])
            self.controller.desktop = self.desktop
            self.closing_desktop = False
            self.lifecycle_thread = None
            self.lifecycle_state = {}
            self.panel = OverviewPage(self.controller, lambda name, run_id=None: self.workspace.show(name, run_id))
            self.review = ProposalWindow(self.controller.confirm_review)
            self.task_report = ReportWindow(assets / 'app_icon.png', title='任务报告',
                                           summary_builder=detail_text, caption='本次任务 · 脱敏报告')
            self.records = RecordsWindow(self.controller, self.task_report.show, desktop=self.desktop)
            self.receipt = ReceiptWindow(self.controller.confirm_receipt, self.controller.confirm_recovery)
            self.settings = SettingsWindow(self.controller, self.lifecycle_action if lifecycle else None, desktop=self.desktop)
            self.privacy = PrivacyWindow(self.controller)
            self.report = ReportWindow(assets / 'app_icon.png')
            self.history_report = None
            self.profiles = ProfilesWindow(self.controller)
            self.workspace = WorkspaceWindow(self.controller, {'overview': self.panel, 'records': self.records,
                'profiles': self.profiles, 'settings': self.settings, 'privacy': self.privacy}, assets / 'app_icon.png')
            self.controller.start()
            self.desktop.start()
            self.update()
            AppHelper.callAfter(self.workspace.show)
            if lifecycle:
                AppHelper.callAfter(self.lifecycle_action, 'open')

        def lifecycle_action(self, action):
            if self.lifecycle_thread and self.lifecycle_thread.is_alive():
                return
            warnings = {
                'stop': ('停止网络保障核心？', '新任务将停止接收。正在执行的修改会先完成验证或回退；守护将暂停。任务记录和恢复文件保留。'),
                'enable': ('启用登录启动？', '当前任务先收尾，再向 macOS 注册普通用户后台核心。仅恢复你明确启用的守护，不恢复模型上传同意、密钥或修复信任。'),
                'disable': ('关闭登录启动？', '核心会先收尾并停止，再注销后台服务。网络档案和处理记录保留。'),
                'enable_helper': ('允许系统配置修复？', '先停止核心，再向 macOS 申请管理员批准。受信任的辅助服务仅可修改 DNS、IPv6 模式和代理开关；每次修复仍需任务授权。不启用模型上传或守护。完成后可手动启动核心。'),
                'disable_helper': ('关闭系统配置修复？', '先停止核心，并确认辅助服务没有未收尾任务，再注销系统服务。未确认的修改和恢复资料保留；不会强制终止其他任务。'),
                'uninstall': ('准备移除 NetCare？', '先收尾、停止核心并注销登录与系统修复服务。不删除应用、任务记录、网络档案或恢复文件。')}
            warnings['activate_helper'] = ('核对并启用系统修复服务？', '核心会先停止。系统已批准且辅助服务无未收尾任务时启用修复；不会修改网络配置、自动启动核心或开启守护。')
            if action in warnings:
                title, message = warnings[action]
                if rumps.alert(title, message, ok='继续', cancel='取消') != 1:
                    self.settings.update_lifecycle(self.lifecycle_state)
                    return
            self.settings.update_lifecycle(self.lifecycle_state, busy=True)
            def work():
                error = None
                result = None
                try:
                    operations = {'open': lifecycle.start, 'status': lifecycle.status,
                        'start': lambda: lifecycle.start(explicit=True), 'stop': lifecycle.stop,
                        'enable': lambda: lifecycle.background(True), 'disable': lambda: lifecycle.background(False),
                        'enable_helper': lambda: lifecycle.privileged(True), 'disable_helper': lambda: lifecycle.privileged(False),
                        'activate_helper': lambda: lifecycle.privileged(True),
                        'uninstall': lifecycle.prepare_uninstall, 'system_settings': lifecycle.service.open_settings}
                    result = operations[action]()
                except Exception:
                    error = '操作未确认完成；未强制结束进程、重发修改或删除资料。请刷新状态后核对。'
                try:
                    state = lifecycle.status()
                except Exception:
                    state = {'core': 'unavailable', 'background': {'available': False, 'state': 'unknown'}}
                AppHelper.callAfter(self.lifecycle_done, state, error, result)
            self.lifecycle_thread = threading.Thread(target=work, name='NetCare lifecycle', daemon=True)
            self.lifecycle_thread.start()

        def lifecycle_done(self, state, error, result):
            if self.controller.stopped.is_set():
                return
            self.lifecycle_state = state
            self.settings.update_lifecycle(state)
            if error:
                self.workspace.notice('生命周期操作未完成', error)
            elif isinstance(result, dict) and result.get('ready_to_remove_app'):
                self.workspace.notice('已准备好移除应用', '核心已停止，登录与系统修复服务已关闭。应用和本地资料未删除。')
            self.update()

        def event(self, kind, payload):
            if self.controller.stopped.is_set():
                return
            if kind == 'state':
                self.update()
            elif kind == 'proposal_review':
                self.review.show(payload)
            elif kind == 'receipt_review':
                self.receipt.show(payload)
            elif kind == 'settings':
                self.workspace.receive(payload['view'], payload)
            elif kind == 'settings_refresh':
                for view in (self.settings, self.privacy):
                    if view.payload:
                        view.load(payload, preserve=not payload['completed'])
            elif kind == 'consent_review':
                self.privacy.show_consent(payload)
            elif kind == 'notification':
                try:
                    rumps.notification('NetCare', payload['title'], payload['message'])
                except Exception:
                    self.settings.error.setStringValue_('系统通知未送达；任务状态仍保留在处理记录中。')
            elif kind == 'agent_records':
                self.workspace.receive('records', payload)
            elif kind == 'task_detail':
                self.records.show_detail(payload)
            elif kind == 'profiles':
                self.workspace.receive('profiles', payload)
            elif kind == 'profile_draft':
                self.workspace.show('profiles')
                self.profiles.import_draft(payload)
            elif kind == 'alert':
                self.workspace.notice(payload['title'], payload['message'])
            elif kind == 'choose_export_directory':
                directory = self.choose_directory()
                if directory:
                    self.desktop.export_pro(directory)
            elif kind == 'export_complete':
                self.workspace.notice('报告已导出', '\n'.join(payload['paths']))
            elif kind == 'history':
                if self.history_report is None:
                    self.history_report = ReportWindow(assets / 'app_icon.png', title='本地诊断历史',
                        summary_builder=history_text, caption='30 天内 · 脱敏诊断记录')
                    self.history_report.tabs.setLabel_forSegment_('历史概览', 0)
                self.history_report.show(payload)

        def update(self):
            state = self.controller.ui_state()
            self.workspace.update_state(state)
            self.review.update_state(state)
            self.receipt.update_state(state)
            account_state = self.desktop.ui_state()
            self.settings.update_account(account_state)
            idle = state['ready'] and not state['busy']
            pending = state.get('run_stage') == 'awaiting_authorization'
            self.menu.clear()
            self.menu.add(rumps.MenuItem('打开网络保障', callback=lambda _: self.workspace.show()))
            self.menu.add(rumps.MenuItem('本地核心已连接' if state['ready'] else '本地核心未连接'))
            for title, action, enabled in (
                ('调查网络问题', self.controller.investigate, idle and not pending),
                ('检测网络', self.controller.check, idle and not pending),
                ('查看待确认方案', self.controller.prepare_fix, idle and pending),
                ('停止当前任务并暂停守护', self.controller.cancel, state['ready']),
                ('停止模型证据上传', self.controller.revoke_model, state['ready']),
                ('处理记录', lambda: self.workspace.show('records'), True),
                ('网络档案', lambda: self.workspace.show('profiles'), True),
                ('权限与隐私', lambda: self.workspace.show('privacy'), True),
                ('设置', lambda: self.workspace.show('settings'), True),
                ('查看基础报告 · Free', lambda: self.show_report(None), bool(state['snapshot'].get('last_check'))),
                ('导出基础报告 · Free', self.export_basic, bool(state['snapshot'].get('last_check'))),
            ):
                self.menu.add(rumps.MenuItem(title, callback=(lambda _, fn=action: fn()) if enabled else None))
            account = account_state['account']
            self.menu.add(rumps.MenuItem('账号 · ' + account['tier'] + ' · ' + (account.get('email') or '未登录'),
                                        callback=lambda _: self.workspace.show('settings')))
            history = rumps.MenuItem('本地诊断历史 · Pro')
            for title, action in (('最近 20 次不同结果', self.desktop.show_history), ('比较最近两次', self.desktop.compare_latest),
                                  ('导出 HTML / JSON', self.desktop.prepare_pro_export)):
                history.add(rumps.MenuItem(title, callback=lambda _, fn=action: fn()))
            self.menu.add(history)
            self.menu.add(None)
            self.menu.add(rumps.MenuItem('退出桌面（核心继续运行）', callback=self.quit))

        def show_report(self, _):
            snapshot = self.controller.ui_state()['snapshot']
            if snapshot.get('last_check'):
                self.report.show(redacted_snapshot(snapshot))

        def choose_directory(self):
            from AppKit import NSOpenPanel, NSModalResponseOK
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setAllowsMultipleSelection_(False)
            panel.setCanCreateDirectories_(True)
            panel.setPrompt_('导出到此文件夹')
            return str(panel.URLs()[0].path()) if panel.runModal() == NSModalResponseOK else None

        def export_basic(self):
            directory = self.choose_directory()
            if directory:
                self.desktop.export_basic(directory)

        def quit(self, _):
            if self.closing_desktop:
                return
            if self.lifecycle_thread and self.lifecycle_thread.is_alive():
                self.workspace.notice('正在处理核心生命周期', '等待本次操作确认结果后再退出桌面。')
                return
            self.closing_desktop = True
            self.workspace.notice('正在退出桌面', '等待账号和文件操作收尾；网络保障核心继续运行。')
            self.controller.close()
            quit_application = rumps.quit_application
            def finish():
                for worker in (self.desktop.accounts, self.desktop.histories, self.desktop.files):
                    worker.thread.join()
                AppHelper.callAfter(quit_application)
            threading.Thread(target=finish, name='NetCare desktop shutdown', daemon=True).start()

    return ConnectedApp


def run_desktop(data_dir, lifecycle=None):
    from .private_files import create_private_directory, lock_runtime, open_private_file
    create_private_directory(Path(data_dir))
    fd = open_private_file(Path(data_dir) / 'desktop.lock')
    try:
        lock_runtime(fd)
        build_connected_app_class(data_dir, lifecycle=lifecycle)().run()
    finally:
        os.close(fd)
    return 0
