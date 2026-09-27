"""Private audit, explicit native recovery, and separate human command acknowledgment."""
import copy
import json
import time

from AppKit import (NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont,
    NSScrollView, NSSize, NSSwitchButton, NSTextView, NSViewWidthSizable,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSObject

from .panel import FlippedView, button, label, place, separator, text_height
from .profiles_window import entry
from .report_window import ReportNativeWindow


class ReceiptActions(NSObject):
    def confirm_(self, sender):
        self.owner.decide()

    def acknowledge_(self, sender):
        self.owner.update_enabled()

    def restore_(self, sender):
        self.owner.recover('restore')

    def retain_(self, sender):
        self.owner.recover('retain')

    def windowShouldClose_(self, window):
        self.owner.valid = False
        self.owner.update_enabled()
        return True

    def controlTextDidChange_(self, notification):
        self.owner.update_enabled()

    def windowDidResize_(self, notification):
        self.owner.layout()


class ReceiptWindow:
    def __init__(self, on_decision, on_recovery=None):
        self.on_decision, self.review, self.valid = on_decision, None, False
        self.on_recovery = on_recovery
        self.actions = ReceiptActions.alloc().init()
        self.actions.owner = self
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 800, 640), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · 私有执行收据')
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(760, 560))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 640))
        self.window.setContentView_(self.root)
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 756, 300))
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
        self.text.setAccessibilityLabel_('完整私有执行收据')
        self.scroll.setDocumentView_(self.text)
        self.note = entry('', self.actions, '人工核对说明，1 至 2000 字')
        self.acknowledge = NSButton.alloc().init()
        self.acknowledge.setButtonType_(NSSwitchButton)
        self.acknowledge.setTitle_('已人工核对命令副作用，同意解除此任务的执行阻止')
        self.acknowledge.setTarget_(self.actions)
        self.acknowledge.setAction_('acknowledge:')
        self.confirm = button('记录人工核对', self.actions, 'confirm:', 'checkmark')
        self.restore = button('恢复原值并复验', self.actions, 'restore:', 'arrow.counterclockwise')
        self.retain = button('保留现值并复验', self.actions, 'retain:', 'checkmark.shield')
        self.window.setDelegate_(self.actions)
        self.window.center()

    def show(self, review):
        self.review, self.valid = copy.deepcopy(review), True
        self.acknowledge.setState_(0)
        self.acknowledge.setTitle_('已核对所列原值与现值，仅授权本次恢复或保留'
            if review.get('recovery') else '已人工核对命令副作用，同意解除此任务的执行阻止')
        self.note.setStringValue_('')
        recovery = review.get('recovery') or {}
        lines = []
        states = {'original': '已是原值', 'desired': '仍是本次修改值', 'drift': '有外部变化',
                  'unknown': '未能读取', 'not_started': '尚未写入'}
        for field in recovery.get('fields', []):
            target = field['target']
            name = {'dns': 'DNS', 'ipv6': 'IPv6', 'proxy:http': 'HTTP 代理',
                    'proxy:https': 'HTTPS 代理', 'proxy:socks': 'SOCKS 代理'}.get(field['field'], field['field'])
            lines.extend([f"{target['name']} · {name} · {states.get(field['state'], '待核对')}",
                f"服务标识：{target['service_id']}（{target['interface']}）",
                '原值：' + json.dumps(field['expected'], ensure_ascii=False),
                '现值：' + (json.dumps(field['actual'], ensure_ascii=False) if field['actual_known'] else '未知'), ''])
        lines.append(json.dumps({key: review.get(key) for key in
            ('run_id', 'stage', 'outcome', 'reconciliation', 'receipt', 'execution_history', 'manual_review')}, ensure_ascii=True, indent=2))
        self.text.setString_('\n'.join(lines))
        self.layout()
        self.text.sizeToFit()
        self.text.scrollRangeToVisible_((0, 0))
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def update_state(self, state):
        if self.review and (not state.get('ready') or state.get('busy')
                            or state.get('core_instance') != self.review['core_instance']):
            self.valid = False
        self.update_enabled()

    def update_enabled(self):
        current = bool(self.review and self.valid and time.time() < self.review['expires_at'])
        valid = bool(current and self.review['can_acknowledge'])
        recovery = (self.review or {}).get('recovery') or {}
        recover = bool(current and self.on_recovery and recovery.get('state') == 'pending')
        accepted = bool(self.acknowledge.state())
        note = str(self.note.stringValue())
        self.confirm.setEnabled_(valid and accepted and bool(note.strip()) and len(note) <= 2000)
        self.restore.setEnabled_(recover and accepted and recovery.get('can_restore') is True)
        self.retain.setEnabled_(recover and accepted and recovery.get('can_retain') is True)
        self.acknowledge.setEnabled_(valid or recover and (recovery.get('can_restore') or recovery.get('can_retain')))
        self.note.setEnabled_(valid)

    def decide(self):
        self.update_enabled()
        if not self.confirm.isEnabled():
            return
        self.valid = False
        self.update_enabled()
        if self.on_decision(copy.deepcopy(self.review), str(self.note.stringValue())):
            self.window.orderOut_(None)

    def recover(self, choice):
        self.update_enabled()
        control = {'restore': self.restore, 'retain': self.retain}.get(choice)
        if control is None or not control.isEnabled():
            return
        self.valid = False
        self.update_enabled()
        if self.on_recovery(copy.deepcopy(self.review), choice):
            self.window.orderOut_(None)

    def layout(self):
        if not self.review:
            return
        width, height = self.root.bounds().size
        for child in list(self.root.subviews()):
            child.removeFromSuperview()
        recovery = self.review.get('recovery')
        place(self.root, label('系统配置恢复' if recovery else '私有执行收据', 18, bold=True), 22, 16, width - 44, 28)
        message = '含原始命令和输出，仅在本地显示。人工核对会解除本任务的执行阻止，但不代表 Agent 已验证全部副作用。'
        if recovery:
            message = '恢复原值会修改所列配置，但不代表网络已恢复。保留现值需通过网络复验；两者都不会恢复旧授权。'
            if recovery['state'] == 'unavailable':
                message = '无法读取可信系统批次，恢复操作已停用。未覆盖当前配置，也未重放原方案。'
            elif recovery['state'] == 'finished':
                message = '系统批次已收尾。任务核对仍以当前配置和网络检测为准；旧授权不会恢复。'
            elif not recovery.get('eligible'):
                message = '原执行者仍持有有效批次，暂不能接管恢复。当前配置保持不变，请稍后重新审阅。'
            elif not recovery.get('can_restore') and not recovery.get('can_retain'):
                message = '配置有外部变化或无法读取，恢复操作已停用。不会覆盖其他程序的新配置。'
        warning = label(message, 12)
        h = text_height(warning, width - 44)
        place(self.root, warning, 22, 52, width - 44, h)
        digest = label('收据 SHA-256  ' + self.review['receipt_hash'], 11, secondary=True)
        place(self.root, digest, 22, 60 + h, width - 44, text_height(digest, width - 44))
        top = 90 + h
        bottom = 150 if self.review['can_acknowledge'] else 112 if recovery else 22
        place(self.root, self.scroll, 22, top, width - 44, height - top - bottom)
        self.text.setFrameSize_(NSSize(self.scroll.contentSize().width,
                                      max(self.text.frame().size.height, self.scroll.contentSize().height)))
        if self.review['can_acknowledge']:
            place(self.root, label('核对说明', 12), 22, height - 139, 85, 26)
            place(self.root, self.note, 110, height - 139, width - 132, 26)
            place(self.root, self.acknowledge, 22, height - 102, width - 44, 28)
            separator(self.root, 22, height - 61, width - 44)
            place(self.root, self.confirm, width - 186, height - 48, 164, 32)
        elif recovery:
            place(self.root, self.acknowledge, 22, height - 100, width - 44, 28)
            separator(self.root, 22, height - 61, width - 44)
            place(self.root, self.restore, width - 360, height - 48, 164, 32)
            place(self.root, self.retain, width - 186, height - 48, 164, 32)
        self.update_enabled()
