"""Native local settings and separate, revocable evidence-upload consent."""
import copy
from datetime import datetime
import time

from AppKit import (NSApplication, NSBackingStoreBuffered, NSButton, NSFont, NSScrollView,
    NSSecureTextField, NSSize, NSSwitchButton, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSObject

from .models import ResponsesModel
from .panel import FlippedView, button, label, place, separator, text_height
from .preferences import validate_preferences
from .profiles_window import entry, icon_button, popup
from .report_window import ReportNativeWindow
from .trust import REASONS as TRUST_REASONS

DETECTION_PRESETS = {'observe': '中性观察', 'company': '公司档案', 'personal_proxy': '个人代理', 'minimal': '极简监测'}


class ManagementActions(NSObject):
    def controlTextDidChange_(self, notification):
        self.owner.dirty = True
        self.owner.update_enabled()

    def edit_(self, sender):
        self.owner.dirty = True
        self.owner.update_enabled()

    def saveModel_(self, sender):
        self.owner.save_model()

    def savePreferences_(self, sender):
        self.owner.save_preferences()

    def forget_(self, sender):
        self.owner.controller.forget_credential()

    def review_(self, sender):
        self.owner.controller.review_consent()

    def allow_(self, sender):
        self.owner.allow()

    def revoke_(self, sender):
        self.owner.controller.revoke_model()

    def revokeTrust_(self, sender):
        self.owner.controller.revoke_trust()

    def cancel_(self, sender):
        self.owner.controller.cancel()

    def reload_(self, sender):
        self.owner.controller.show_settings(self.owner.kind)
        if getattr(self.owner, 'lifecycle_action', None):
            self.owner.lifecycle_action('status')

    def startCore_(self, sender):
        self.owner.lifecycle_action('start')

    def stopCore_(self, sender):
        self.owner.lifecycle_action('stop')

    def background_(self, sender):
        enabled = self.owner.lifecycle_state.get('background', {}).get('state')
        self.owner.lifecycle_action('disable' if enabled in ('enabled', 'requires_approval') else 'enable')

    def systemSettings_(self, sender):
        self.owner.lifecycle_action('system_settings')

    def privileged_(self, sender):
        enabled = self.owner.lifecycle_state.get('helper', {}).get('state')
        self.owner.lifecycle_action('disable_helper' if enabled in ('enabled', 'requires_approval') else 'enable_helper')

    def activateHelper_(self, sender):
        self.owner.lifecycle_action('activate_helper')

    def uninstall_(self, sender):
        self.owner.lifecycle_action('uninstall')

    def login_(self, sender):
        self.owner.desktop.login()

    def cancelLogin_(self, sender):
        self.owner.desktop.cancel_login()

    def logout_(self, sender):
        self.owner.desktop.logout()

    def syncAccount_(self, sender):
        self.owner.desktop.refresh_account()

    def accountPage_(self, sender):
        self.owner.desktop.open_account_page()

    def saveDetection_(self, sender):
        if self.owner.save_detection.isEnabled():
            self.owner.controller.set_preset(list(DETECTION_PRESETS)[self.owner.detection_preset.indexOfSelectedItem()],
                                            self.owner.payload['detection']['revision'])

    def redetect_(self, sender):
        if self.owner.redetect.isEnabled():
            self.owner.controller.redetect(self.owner.payload['detection']['revision'])

    def openConfig_(self, sender):
        if self.owner.payload:
            from pathlib import Path
            from AppKit import NSWorkspace
            from Foundation import NSURL
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([
                NSURL.fileURLWithPath_(str(Path(self.owner.payload['data_directory']) / 'config.json'))])

    def windowDidResize_(self, notification):
        self.owner.layout()

    def windowShouldClose_(self, window):
        self.owner.dismiss()
        return True


def checkbox(title, actions):
    control = NSButton.alloc().init()
    control.setButtonType_(NSSwitchButton)
    control.setTitle_(title)
    control.setTarget_(actions)
    control.setAction_('edit:')
    return control


class ManagementWindow:
    def __init__(self, controller, title, kind):
        self.controller, self.title, self.kind = controller, title, kind
        self.payload, self.state, self.dirty = None, {}, False
        self.actions = ManagementActions.alloc().init()
        self.actions.owner = self
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 800, 640), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · ' + title)
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(760, 560))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 640))
        self.window.setContentView_(self.root)
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.document = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 756, 550))
        self.scroll.setDocumentView_(self.document)
        self.reload = icon_button('重新读取', 'arrow.clockwise', self.actions, 'reload:')
        self.error = label('', 12, secondary=True)
        self.window.setDelegate_(self.actions)
        self.window.center()

    def show(self, payload):
        self.load(payload)
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def update_state(self, state):
        self.state = state
        self.update_enabled()

    def idle(self):
        return bool(self.payload and self.state.get('ready') and not self.state.get('busy')
                    and self.payload['core_instance'] == self.state.get('core_instance')
                    and self.state.get('run_stage') not in ('awaiting_authorization', 'authorized'))

    def begin_layout(self):
        width, height = self.root.bounds().size
        for child in list(self.root.subviews()):
            child.removeFromSuperview()
        for child in list(self.document.subviews()):
            child.removeFromSuperview()
        place(self.root, label(self.title, 18, bold=True), 22, 16, width - 95, 28)
        place(self.root, self.reload, width - 55, 16, 32, 30)
        error_height = max(28, min(80, text_height(self.error, width - 44)))
        place(self.root, self.scroll, 22, 58, width - 44, height - 76 - error_height)
        place(self.root, self.error, 22, height - 10 - error_height, width - 44, error_height)
        self.content_width = self.scroll.contentSize().width - 8
        return self.content_width

    def line(self, text, y, *, heading=False):
        field = label(text, 14 if heading else 12, bold=heading)
        h = max(24, text_height(field, self.content_width))
        place(self.document, field, 0, y, self.content_width, h)
        return y + h + 8

    def field(self, title, control, y):
        place(self.document, label(title, 12), 0, y, 106, 26)
        place(self.document, control, 112, y, self.content_width - 112, 28)
        return y + 40

    def finish_layout(self, y):
        self.document.setFrameSize_(NSSize(self.scroll.contentSize().width, max(y + 12, self.scroll.contentSize().height)))
        self.update_enabled()

    def dismiss(self):
        pass


class SettingsWindow(ManagementWindow):
    def __init__(self, controller, lifecycle_action=None, desktop=None):
        super().__init__(controller, '设置', 'settings')
        self.lifecycle_action = lifecycle_action
        self.desktop, self.account_state = desktop, {}
        self.offline_available = bool(lifecycle_action or desktop)
        self.login = button('浏览器登录', self.actions, 'login:', 'person.crop.circle')
        self.cancel_login = button('取消登录', self.actions, 'cancelLogin:', 'xmark')
        self.logout = button('退出账号', self.actions, 'logout:', 'rectangle.portrait.and.arrow.right')
        self.sync_account = icon_button('同步账号状态', 'arrow.clockwise', self.actions, 'syncAccount:')
        self.account_page = button('账户、试用与订阅', self.actions, 'accountPage:', 'arrow.up.right.square')
        self.lifecycle_state = {}
        self.lifecycle_busy = False
        self.start_core = button('启动核心', self.actions, 'startCore:', 'play')
        self.stop_core = button('停止核心', self.actions, 'stopCore:', 'stop')
        self.background = checkbox('登录时启动网络保障核心', self.actions)
        self.background.setAction_('background:')
        self.privileged = checkbox('允许系统配置修复', self.actions)
        self.privileged.setAction_('privileged:')
        self.activate_helper = icon_button('核对系统批准并启用修复服务', 'checkmark.shield', self.actions, 'activateHelper:')
        self.system_settings = button('系统登录项', self.actions, 'systemSettings:', 'gearshape')
        self.uninstall = button('准备移除应用', self.actions, 'uninstall:', 'trash')
        self.name = entry('', self.actions, '模型名称')
        self.endpoint = entry('', self.actions, 'Responses API 接收地址')
        self.key = NSSecureTextField.alloc().init()
        self.key.setFont_(NSFont.systemFontOfSize_(12))
        self.key.setDelegate_(self.actions)
        self.key.setAccessibilityLabel_('模型 API 密钥，仅本次核心内存')
        self.key_action = popup({'replace': '替换密钥', 'keep': '保留同地址密钥', 'forget': '不保留密钥'}, 'replace', self.actions)
        self.save_model_button = button('保存模型配置', self.actions, 'saveModel:', 'externaldrive')
        self.forget = button('清除内存密钥', self.actions, 'forget:', 'key.slash')
        self.notifications = checkbox('桌面运行时接收需要处理的通知', self.actions)
        self.days = entry('30', self.actions, '已结束任务保留天数，7 至 90')
        self.limit = entry('1000', self.actions, '已结束任务最多条数，100 至 1000')
        self.save_preferences_button = button('保存偏好', self.actions, 'savePreferences:', 'externaldrive')
        self.detection_preset = popup(DETECTION_PRESETS, 'observe', self.actions)
        self.save_detection = button('应用检测预设', self.actions, 'saveDetection:', 'checkmark')
        self.redetect = button('重新识别环境', self.actions, 'redetect:', 'arrow.triangle.2.circlepath')
        self.open_config = icon_button('显示本地配置', 'folder', self.actions, 'openConfig:')

    def update_lifecycle(self, state, busy=False):
        self.lifecycle_state, self.lifecycle_busy = copy.deepcopy(state), busy
        self.layout()

    def update_account(self, state):
        if self.account_state != state:
            self.account_state = copy.deepcopy(state)
            self.layout()

    def account_layout(self, y):
        if not self.desktop:
            return y
        account = self.account_state.get('account', {})
        y = self.line('账号', y, heading=True)
        y = self.line(account.get('tier', 'Free') + ' · ' + (account.get('email') or '未登录'), y)
        y = self.line('等待浏览器确认' if self.account_state.get('login_active') else account.get('message', '基础诊断无需登录'), y)
        place(self.document, self.cancel_login if self.account_state.get('login_active') else self.login, 0, y, 144, 32)
        place(self.document, self.logout, 156, y, 128, 32)
        place(self.document, self.sync_account, 296, y, 32, 32)
        y += 44
        place(self.document, self.account_page, 0, y, 216, 32)
        y += 47
        separator(self.document, 0, y, self.content_width)
        return y + 16

    def runtime_layout(self, y):
        y = self.line('运行环境', y, heading=True)
        if not self.lifecycle_action:
            return self.line('连接已有核心 · 系统服务状态未查询 · 未接入签名升级', y)
        state = self.lifecycle_state
        label = {'running': '运行中', 'stopped': '已停止', 'draining': '正在收尾',
                 'unavailable': '进程仍占用数据，连接不可用'}.get(state.get('core'), '尚未确认')
        y = self.line('本地核心：' + ('操作进行中' if self.lifecycle_busy else label), y)
        if state.get('signed_service_required') and state.get('background', {}).get('state') != 'enabled':
            y = self.line('签名核心：等待启用并批准登录服务', y)
        place(self.document, self.start_core, 0, y, 132, 32)
        place(self.document, self.stop_core, 148, y, 132, 32)
        y += 44
        registration = state.get('background', {})
        registered = registration.get('state') in ('enabled', 'requires_approval')
        self.background.setState_(int(registered))
        place(self.document, self.background, 0, y, self.content_width, 28)
        y += 36
        statuses = {'enabled': '已启用', 'not_registered': '未启用', 'requires_approval': '等待系统批准',
                    'not_found': '服务文件未找到', 'unknown': '状态未知', 'unavailable': '此入口不支持注册'}
        y = self.line('登录启动：' + statuses.get(registration.get('state'), '尚未确认'), y)
        helper = state.get('helper', {})
        self.privileged.setState_(int(helper.get('state') in ('enabled', 'requires_approval')))
        place(self.document, self.privileged, 0, y, self.content_width - 44, 28)
        place(self.document, self.activate_helper, self.content_width - 32, y, 32, 28)
        y += 36
        helper_status = ('需要受信任的 Developer ID 安装包' if helper.get('reason') == 'signed_bundle_required'
                         else statuses.get(helper.get('state'), '尚未确认'))
        y = self.line('系统修复注册：' + helper_status, y)
        y = self.line('修复授权与模型上传同意独立；系统服务变更后核心保持停止。', y)
        place(self.document, self.system_settings, 0, y, 132, 32)
        place(self.document, self.uninstall, 148, y, 160, 32)
        return y + 48

    def load(self, payload, preserve=False):
        self.payload = copy.deepcopy(payload)
        if not preserve:
            model = payload['model']
            self.name.setStringValue_(model['name'])
            self.endpoint.setStringValue_(model['endpoint'])
            self.key.setStringValue_('')
            self.key_action.selectItemAtIndex_(1 if model['credential_present'] else 0)
            preferences = payload['preferences']['values']
            self.days.setStringValue_(str(preferences['history_days']))
            self.limit.setStringValue_(str(preferences['history_limit']))
            self.notifications.setState_(int(preferences['notifications_enabled']))
            self.detection_preset.selectItemAtIndex_(list(DETECTION_PRESETS).index(
                payload.get('detection', {}).get('preset', 'observe'))
                if payload.get('detection', {}).get('preset', 'observe') in DETECTION_PRESETS else 0)
            self.dirty = False
        self.error.setStringValue_('')
        self.layout()

    def save_model(self):
        if not self.save_model_button.isEnabled():
            return
        self.window.makeFirstResponder_(None)
        action = ('replace', 'keep', 'forget')[self.key_action.indexOfSelectedItem()]
        values = {'name': str(self.name.stringValue()).strip(), 'endpoint': str(self.endpoint.stringValue()).strip(),
                  'api_key': str(self.key.stringValue()) if action == 'replace' else '',
                  'credential_action': action, 'revision': self.payload['model']['revision']}
        try:
            ResponsesModel(values['name'], values['endpoint'], values['api_key'])
            if action == 'keep' and values['endpoint'] != self.payload['model']['endpoint']:
                raise ValueError('Endpoint changed')
        except ValueError:
            self.error.setStringValue_('配置无效：模型名称、HTTPS / 本机回环 Responses 地址或密钥不符合要求；换地址需替换或清除密钥。')
            self.layout()
            return
        if self.controller.configure_model(**values):
            self.key.setStringValue_('')
            self.save_model_button.setEnabled_(False)

    def save_preferences(self):
        if not self.save_preferences_button.isEnabled():
            return
        try:
            values = validate_preferences({'history_days': int(str(self.days.stringValue())),
                'history_limit': int(str(self.limit.stringValue())), 'notifications_enabled': bool(self.notifications.state())})
        except ValueError:
            self.error.setStringValue_('保留时间须为 7–90 天，最多条数须为 100–1000 的整数。')
            self.layout()
            return
        self.controller.save_preferences(values, self.payload['preferences']['revision'])

    def update_enabled(self):
        if not hasattr(self, 'save_model_button'):
            return
        idle = self.idle()
        same_model = bool(self.payload and self.payload['model']['revision'] == self.state.get('model', {}).get('revision'))
        self.save_model_button.setEnabled_(idle and same_model)
        if idle and not same_model:
            self.error.setStringValue_('模型配置或上传权限已变化；请重新读取后再保存模型配置。')
        self.save_preferences_button.setEnabled_(idle)
        self.key.setEnabled_(idle and self.key_action.indexOfSelectedItem() == 0)
        self.forget.setEnabled_(bool(self.state.get('ready') and self.state.get('model', {}).get('credential_present')))
        for control in (self.name, self.endpoint, self.key_action, self.days, self.limit, self.notifications):
            control.setEnabled_(idle)
        available = bool(self.lifecycle_action and not self.lifecycle_busy)
        self.start_core.setEnabled_(available and self.lifecycle_state.get('core') == 'stopped'
            and (not self.lifecycle_state.get('signed_service_required')
                 or self.lifecycle_state.get('background', {}).get('state') == 'enabled'))
        self.stop_core.setEnabled_(available and self.lifecycle_state.get('core') == 'running')
        background = self.lifecycle_state.get('background', {}).get('available', False)
        helper = self.lifecycle_state.get('helper', {}).get('available', False)
        self.background.setEnabled_(available and background)
        self.privileged.setEnabled_(available and helper)
        self.activate_helper.setEnabled_(available and helper and
            self.lifecycle_state.get('helper', {}).get('state') in ('enabled', 'requires_approval'))
        self.system_settings.setEnabled_(available and (background or helper))
        self.uninstall.setEnabled_(available and background and self.lifecycle_state.get('helper', {}).get('available', True))
        account = self.account_state.get('account', {})
        changing = bool(self.account_state.get('login_active') or self.account_state.get('logout_pending'))
        configured = bool(self.desktop and account.get('configured'))
        self.login.setEnabled_(configured and not changing and not account.get('email'))
        self.cancel_login.setEnabled_(bool(self.account_state.get('login_active')))
        self.logout.setEnabled_(configured and not self.account_state.get('logout_pending')
                               and bool(account.get('email') or self.account_state.get('login_active')))
        self.sync_account.setEnabled_(configured and not changing and not self.account_state.get('refresh_pending'))
        self.account_page.setEnabled_(configured)
        detection = (self.payload or {}).get('detection', {})
        detection_ready = idle and detection.get('available', False) and detection.get('revision') == self.state.get('detection', {}).get('revision')
        for control in (self.detection_preset, self.save_detection, self.redetect):
            control.setEnabled_(bool(detection_ready))
        self.open_config.setEnabled_(bool(self.payload))

    def dismiss(self):
        self.key.setStringValue_('')

    def layout(self):
        if not self.payload and not self.offline_available:
            return
        width = self.begin_layout()
        y = self.runtime_layout(self.account_layout(0))
        if not self.payload or self.offline_available and not self.state.get('ready'):
            self.finish_layout(y)
            return
        separator(self.document, 0, y, width)
        identity = self.payload.get('ipc_identity')
        if identity:
            y = self.line('通信身份：' + ('同一签名构建、用户与登录会话' if identity == 'signed_build_and_session'
                                       else '同一用户 · 未验证应用签名'), y + 15)
        y = self.line('检测设置', y + 15, heading=True)
        y = self.field('检测预设', self.detection_preset, y)
        place(self.document, self.save_detection, 0, y, 152, 32)
        place(self.document, self.redetect, 164, y, 152, 32)
        place(self.document, self.open_config, 328, y, 32, 32)
        y += 48
        if self.payload.get('detection', {}).get('load_error'):
            y = self.line('配置文件读取失败 · 当前设置只读', y)
        separator(self.document, 0, y, width)
        y = self.line('模型连接', y + 15, heading=True)
        y = self.field('模型名称', self.name, y)
        y = self.field('接收地址', self.endpoint, y)
        y = self.field('密钥处理', self.key_action, y)
        y = self.field('API 密钥', self.key, y)
        model = self.payload['model']
        y = self.line('密钥仅存于本次核心内存，重启后清除。上传同意：' + ('已允许' if model['consented'] else '未允许'), y)
        place(self.document, self.save_model_button, 0, y, 160, 32)
        place(self.document, self.forget, 176, y, 160, 32)
        y += 49
        separator(self.document, 0, y, width)
        y = self.line('本地记录与通知', y + 15, heading=True)
        y = self.field('保留天数', self.days, y)
        y = self.field('最多条数', self.limit, y)
        place(self.document, self.notifications, 0, y, width, 28)
        y += 38
        y = self.line('保留策略在后续任务写入时清理已结束记录；未核对任务、恢复文件和安全审计不随此策略删除。', y)
        place(self.document, self.save_preferences_button, 0, y, 140, 32)
        y += 49
        separator(self.document, 0, y, width)
        y = self.line('数据目录：' + self.payload['data_directory'], y + 15)
        self.finish_layout(y)


class PrivacyWindow(ManagementWindow):
    def __init__(self, controller):
        super().__init__(controller, '权限与隐私', 'privacy')
        self.review = None
        self.inspect = button('审阅上传同意', self.actions, 'review:', 'eye')
        self.allow_button = button('允许本次核心上传', self.actions, 'allow:', 'checkmark.shield')
        self.acknowledge = checkbox('同意向上述接收地址上传约定的最小证据', self.actions)
        self.revoke = button('撤销上传同意', self.actions, 'revoke:', 'hand.raised')
        self.cancel = button('停止任务并暂停守护', self.actions, 'cancel:', 'stop')
        self.revoke_trust = button('撤销范围信任', self.actions, 'revokeTrust:', 'hand.raised.slash')

    def load(self, payload, preserve=False):
        self.payload = copy.deepcopy(payload)
        if not preserve:
            self.review = None
            self.acknowledge.setState_(0)
        self.layout()

    def show_consent(self, review):
        if not self.payload:
            return
        self.review = copy.deepcopy(review)
        self.payload['model'] = copy.deepcopy(review['model'])
        self.acknowledge.setState_(0)
        self.layout()
        self.window.makeKeyAndOrderFront_(None)
        self.document.scrollRectToVisible_(self.acknowledge.frame())

    def update_state(self, state):
        if self.review and (not state.get('ready') or state.get('core_instance') != self.review['core_instance']
                           or state.get('model', {}).get('revision') != self.review['revision']):
            self.review = None
            self.acknowledge.setState_(0)
        super().update_state(state)
        if self.payload and state.get('ready') and state.get('core_instance') == self.payload['core_instance']:
            model = state.get('model', {})
            recovery = state.get('agent', {}).get('recovery_pending', self.payload['recovery_pending'])
            trust = state.get('trust', self.payload.get('trust', {}))
            if model and (model != self.payload['model'] or recovery != self.payload['recovery_pending']
                          or trust != self.payload.get('trust', {})):
                self.payload['model'] = copy.deepcopy(model)
                self.payload['recovery_pending'] = recovery
                self.payload['trust'] = copy.deepcopy(trust)
                self.layout()

    def update_enabled(self):
        if not hasattr(self, 'inspect'):
            return
        model = self.state.get('model', {})
        self.inspect.setEnabled_(self.idle() and bool(model.get('configured')))
        valid = bool(self.idle() and self.review and time.time() < self.review['expires_at']
                     and self.review['revision'] == model.get('revision')
                     and (model.get('local') or model.get('credential_present')))
        self.acknowledge.setEnabled_(valid)
        self.allow_button.setEnabled_(valid and bool(self.acknowledge.state()))
        self.revoke.setEnabled_(bool(self.state.get('ready')))
        self.cancel.setEnabled_(bool(self.state.get('ready')))
        self.revoke_trust.setEnabled_(bool(self.state.get('ready')))

    def allow(self):
        self.update_enabled()
        if not self.allow_button.isEnabled():
            return
        self.controller.confirm_consent(copy.deepcopy(self.review))
        self.review = None
        self.acknowledge.setState_(0)
        self.update_enabled()

    def dismiss(self):
        self.review = None
        self.acknowledge.setState_(0)

    def layout(self):
        if not self.payload:
            return
        width = self.begin_layout()
        y = self.line('系统修改授权', 0, heading=True)
        grant = self.payload.get('trust', {}).get('grant')
        if self.payload.get('trust', {}).get('available') is False:
            y = self.line('授权记录不可用；新的范围信任已停用。', y)
        if not grant:
            y = self.line('默认逐次确认。暂无任务或持续信任。', y)
        else:
            mode = '本次任务' if grant['mode'] == 'task' else '限定范围持续信任'
            state = '有效' if grant['state'] == 'active' else TRUST_REASONS.get(grant['reason'], '已暂停')
            y = self.line(mode + ' · ' + state, y)
            y = self.line('到期：' + datetime.fromtimestamp(grant['expires_at']).strftime('%m-%d %H:%M:%S') +
                          f" · 已预留 {grant['used']}/{grant['limit']} 次执行", y)
            for action in grant['scope']['actions']:
                y = self.line(f"{action['service']} · {action['field']} → {action['desired']}", y)
                environment = action['environment']
                y = self.line(f"{environment['wifi_interface']} · {environment['wifi_network']} · {environment['wifi_ip']}", y)
            y = self.line('档案：' + str(grant['scope']['profile']) + ' · VPN：' + str(grant['scope']['vpn']['vpn_client'] or grant['scope']['vpn']['vpn']), y)
            y = self.line('范围 SHA-256：' + grant['scope_hash'], y)
        y = self.line('守护、上传同意和系统权限分别授权；核心重启后范围信任暂停。尚无独立特权服务。', y)
        y = self.line('当前用户动态命令不是沙箱；人工核对不代表自动验证。' +
                      ('存在待核对任务。' if self.payload['recovery_pending'] else '暂无待核对任务。'), y)
        place(self.document, self.cancel, 0, y, 220, 32)
        place(self.document, self.revoke_trust, 234, y, 158, 32)
        y += 48
        separator(self.document, 0, y, width)
        y = self.line('近期修改授权', y + 15, heading=True)
        for event in self.payload.get('authorization_events', []):
            name = {'granted': '已授予范围信任', 'reserved': '已预留一次执行'}.get(event['event'], TRUST_REASONS.get(event['event'], '状态变化'))
            y = self.line(datetime.fromtimestamp(event['time']).strftime('%m-%d %H:%M:%S') + '  ' + name, y)
        if not self.payload.get('authorization_events'):
            y = self.line('暂无范围授权记录', y)
        separator(self.document, 0, y + 8, width)
        y = self.line('模型证据上传', y + 15, heading=True)
        model = self.payload['model']
        y = self.line('模型：' + (model['name'] or '未配置') + ' · ' + ('已允许本次核心上传' if model['consented'] else '未允许上传'), y)
        y = self.line('接收地址：' + model['endpoint'], y)
        y = self.line('证据约定：' + model['data_schema'], y)
        y = self.line('仅含状态、目标别名、计数及有限执行统计；不含原始地址、配置、argv、stdin、stdout、stderr 或恢复材料。供应商保留策略独立于本地设置。', y)
        place(self.document, self.inspect, 0, y, 164, 32)
        place(self.document, self.revoke, 180, y, 164, 32)
        y += 44
        if self.review:
            y = self.line('本次审阅有效至 ' + datetime.fromtimestamp(self.review['expires_at']).strftime('%H:%M:%S') +
                          '；仅绑定当前模型、地址、证据约定和核心实例。', y)
            y = self.line('绑定 SHA-256：' + self.review['binding'], y)
        place(self.document, self.acknowledge, 0, y, width, 28)
        y += 36
        place(self.document, self.allow_button, 0, y, 210, 32)
        y += 48
        separator(self.document, 0, y, width)
        y = self.line('近期隐私操作', y + 15, heading=True)
        names = {'configured': '模型配置已保存', 'consent_granted': '上传同意已授予',
                 'consent_revoked': '上传同意已撤销', 'credential_forgotten': '内存密钥已清除'}
        for event in self.payload['privacy_events']:
            y = self.line(datetime.fromtimestamp(event['time']).strftime('%m-%d %H:%M:%S') + '  ' + names[event['action']], y)
        if not self.payload['privacy_events']:
            y = self.line('暂无操作记录', y)
        self.finish_layout(y)
