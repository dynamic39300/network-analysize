"""Exact local action review; deliberately separate from redacted reports."""
import copy
from datetime import datetime
import json
import time

from AppKit import (NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSPopUpButton,
    NSScrollView, NSSize, NSSwitchButton, NSTextView, NSViewWidthSizable,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSObject

from .panel import FlippedView, button, label, place, separator, text_height
from .report_window import ReportNativeWindow


class ProposalActions(NSObject):
    def confirm_(self, sender):
        self.owner.decide(True)

    def cancel_(self, sender):
        self.owner.decide(False)

    def acknowledge_(self, sender):
        self.owner.update_enabled()

    def mode_(self, sender):
        self.owner.trust_ack.setState_(0)
        self.owner.render_text()
        self.owner.layout()

    def windowDidResize_(self, notification):
        self.owner.layout()


class ProposalWindow:
    def __init__(self, on_decision):
        self.on_decision = on_decision
        self.review = None
        self.valid = False
        self.actions = ProposalActions.alloc().init()
        self.actions.owner = self
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 800, 640), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · 确认本次处理')
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(760, 560))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 640))
        self.window.setContentView_(self.root)
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 756, 400))
        self.text.setEditable_(False)
        self.text.setSelectable_(True)
        self.text.setRichText_(False)
        self.text.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0))
        self.text.setTextColor_(NSColor.textColor())
        self.text.setBackgroundColor_(NSColor.textBackgroundColor())
        self.text.setTextContainerInset_(NSSize(14, 12))
        self.text.setVerticallyResizable_(True)
        self.text.setHorizontallyResizable_(False)
        self.text.setMinSize_(NSSize(0, 0))
        self.text.setMaxSize_(NSSize(1000000, 1000000))
        self.text.setAutoresizingMask_(NSViewWidthSizable)
        self.text.textContainer().setWidthTracksTextView_(True)
        self.scroll.setDocumentView_(self.text)
        self.acknowledge = NSButton.alloc().init()
        self.acknowledge.setButtonType_(NSSwitchButton)
        self.acknowledge.setTitle_('我已核对完整命令，并确认其当前用户权限风险')
        self.acknowledge.setTarget_(self.actions)
        self.acknowledge.setAction_('acknowledge:')
        self.mode = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 240, 28), False)
        self.mode.addItemsWithTitles_(['仅本次方案', '本次任务 · 15 分钟 / 3 次', '持续信任 · 8 小时 / 12 次'])
        self.mode.setTarget_(self.actions)
        self.mode.setAction_('mode:')
        self.mode.setAccessibilityLabel_('修改授权范围')
        self.trust_ack = NSButton.alloc().init()
        self.trust_ack.setButtonType_(NSSwitchButton)
        self.trust_ack.setTitle_('我确认上述授权范围、到期与额度；核心重启后需重新审阅')
        self.trust_ack.setTarget_(self.actions)
        self.trust_ack.setAction_('acknowledge:')
        self.confirm = button('确认并执行本次方案', self.actions, 'confirm:', 'checkmark.shield')
        self.cancel = button('取消本次方案', self.actions, 'cancel:', 'xmark')
        self.window.setDelegate_(self.actions)
        self.window.center()

    def show(self, review):
        self.review = copy.deepcopy(review)
        self.valid = True
        self.acknowledge.setState_(0)
        self.mode.selectItemAtIndex_(0)
        self.trust_ack.setState_(0)
        self.render_text()
        self.layout()
        self.text.sizeToFit()
        self.text.scrollRangeToVisible_((0, 0))
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def render_text(self):
        value = {'proposal': self.review['proposal']}
        intro = []
        if self.mode.indexOfSelectedItem() and self.review.get('trust_offer', {}).get('available'):
            value['trust_scope'] = self.review['trust_offer']['scope']
            value['scope_hash'] = self.review['trust_offer']['scope_hash']
            scope = value['trust_scope']
            intro = ['获准范围（新授权替换现有范围信任）']
            for action in scope['actions']:
                field = {'dns': 'DNS', 'ipv6': 'IPv6', 'proxy:http': 'HTTP 代理开关',
                         'proxy:https': 'HTTPS 代理开关', 'proxy:socks': 'SOCKS 代理开关'}[action['field']]
                intro.append(f"{action['service']} · {field} → {action['desired']}")
                env = action['environment']
                intro.append(f"接口 {env['wifi_interface']} · 网络 {env['wifi_network']} · 地址 {env['wifi_ip']}")
            intro += ['档案：' + str(scope['profile']), 'VPN：' + str(scope['vpn']['vpn_client'] or scope['vpn']['vpn']),
                      '目标值与环境必须继续匹配；后续原值可以不同。执行可能短暂中断连接。',
                      '每批修改前保存原值，验证不通过时尝试回退；无法确认结果时暂停信任。',
                      '不启用守护，不授予模型上传或系统管理员权限。核心重启后暂停。', '', '完整方案与范围：']
        self.text.setString_('\n'.join(intro) + ('\n' if intro else '') + json.dumps(value, ensure_ascii=True, indent=2))
        self.text.sizeToFit()

    def update_state(self, state):
        if not self.review:
            return
        valid = (state.get('ready') and not state.get('busy') and state.get('core_instance') == self.review['core_instance']
                 and state.get('run_id') == self.review['run_id'] and state.get('run_stage') == 'awaiting_authorization')
        if not valid:
            self.valid = False
        if self.mode.indexOfSelectedItem() and state.get('trust', {}).get('revision') != self.review.get('trust_revision'):
            self.valid = False
        self.update_enabled()

    def update_enabled(self):
        current = bool(self.valid and self.review and time.time() < self.review['expires_at'])
        trust = bool(self.mode.indexOfSelectedItem())
        eligible = bool(self.review and self.review.get('trust_offer', {}).get('available') and not self.review['unrestricted'])
        self.mode.setEnabled_(current and eligible)
        self.trust_ack.setEnabled_(current and eligible and trust)
        self.confirm.setEnabled_(current and (not self.review['unrestricted'] or bool(self.acknowledge.state()))
                                 and (not trust or eligible and bool(self.trust_ack.state())))
        self.confirm.setTitle_('授权范围并处理' if trust else '确认并执行本次方案')
        self.cancel.setEnabled_(current)

    def decide(self, accepted):
        self.update_enabled()
        if not (self.confirm.isEnabled() if accepted else self.cancel.isEnabled()):
            return
        review = copy.deepcopy(self.review)
        review['authorization_mode'] = ('single_run', 'task', 'continuous')[self.mode.indexOfSelectedItem()]
        self.valid = False
        self.update_enabled()
        self.on_decision(review, accepted)
        self.window.orderOut_(None)

    def layout(self):
        if not self.review:
            return
        width, height = self.root.bounds().size
        for view in list(self.root.subviews()):
            view.removeFromSuperview()
        scoped = bool(self.mode.indexOfSelectedItem())
        place(self.root, label('确认授权范围' if scoped else '确认本次处理', 18, bold=True), 22, 16, width - 44, 28)
        warning = ('动态命令不是沙箱，可能访问用户文件、联网或调用已有权限辅助工具。任意副作用需人工核对。'
                   if self.review['unrestricted'] else '确认后，范围内的后续成熟修复可免逐次询问。到期、额度耗尽、撤销或验证失败时停止自动执行。'
                   if scoped else '本次确认仅授权下方具体修改。执行前重新核对现场，执行后验证网络与恢复结果。')
        field = label(warning, 12)
        warning_height = text_height(field, width - 44)
        place(self.root, field, 22, 52, width - 44, warning_height)
        y = 60 + warning_height
        deadline = datetime.fromtimestamp(self.review['expires_at']).strftime('%H:%M:%S')
        place(self.root, label('本页审阅有效至 ' + deadline + ' · 含私有网络信息，未脱敏', 11, secondary=True), 22, y, width - 44, 22)
        digest = label('方案 SHA-256  ' + self.review['proposal_hash'], 11, secondary=True)
        place(self.root, digest, 22, y + 25, width - 44, text_height(digest, width - 44))
        top = y + 58
        eligible = self.review.get('trust_offer', {}).get('available') and not self.review['unrestricted']
        bottom = 110 if self.review['unrestricted'] else 174 if eligible else 64
        place(self.root, self.scroll, 22, top, width - 44, max(100, height - top - bottom))
        self.text.setFrameSize_(NSSize(self.scroll.contentSize().width,
                                      max(self.text.frame().size.height, self.scroll.contentSize().height)))
        if self.review['unrestricted']:
            place(self.root, self.acknowledge, 22, height - 98, width - 44, 28)
        if eligible:
            place(self.root, self.mode, 22, height - 163, 254, 28)
            place(self.root, label('同范围后续修复免确认；原值可变，目标值不变。动态命令或未知效果另行确认。', 11, secondary=True),
                  22, height - 128, width - 44, 24)
            place(self.root, self.trust_ack, 22, height - 98, width - 44, 28)
        separator(self.root, 22, height - 58, width - 44)
        place(self.root, self.cancel, width - 370, height - 45, 148, 32)
        place(self.root, self.confirm, width - 212, height - 45, 190, 32)
        self.update_enabled()
