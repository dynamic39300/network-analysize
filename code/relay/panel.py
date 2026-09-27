"""Native diagnostic window and non-blocking repair sheet. Main thread only."""
import math
from pathlib import Path

from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSBezelStyleRounded,
    NSBox, NSBoxSeparator, NSButton, NSColor, NSFont, NSFontAttributeName,
    NSFontWeightSemibold, NSImage, NSImageView, NSLineBreakByCharWrapping,
    NSProgressIndicator, NSProgressIndicatorStyleSpinning, NSScrollView,
    NSRectFill, NSSize, NSSwitch, NSTextField, NSView, NSWindow, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled, NSWorkspace,
)
from Foundation import NSMakeRect, NSObject, NSString, NSURL

from .presentation import diagnostic_rows, handling_plan, optimization_issues, panel_summary, scan_caption


def open_network_settings():
    path = Path('/System/Library/PreferencePanes/Network.prefPane')
    return path.exists() and bool(NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(path))))


class FlippedView(NSView):
    def isFlipped(self):
        return True

    def isOpaque(self):
        return True

    def drawRect_(self, rect):
        NSColor.windowBackgroundColor().setFill()
        # Newer AppKit dirty rectangles can extend beyond the view's bounds.
        NSRectFill(self.bounds())


class PanelActions(NSObject):
    def guard_(self, sender):
        self.owner.controller.set_guard_enabled(bool(sender.state()))

    def check_(self, sender):
        self.owner.controller.check()

    def report_(self, sender):
        self.owner.on_report(sender)

    def records_(self, sender):
        self.owner.controller.show_agent_records()

    def profiles_(self, sender):
        self.owner.controller.show_profiles()

    def optimize_(self, sender):
        if self.owner.controller.ui_state().get('run_stage') == 'awaiting_authorization':
            self.owner.controller.prepare_fix()
            return
        issues = optimization_issues(self.owner.controller.ui_state())
        if issues:
            self.owner.controller.prepare_fix(issues)

    def details_(self, sender):
        key = self.owner.rows[sender.tag()]['id']
        if key in self.owner.expanded:
            self.owner.expanded.remove(key)
        else:
            self.owner.expanded.add(key)
        self.owner.render(self.owner.controller.ui_state())

    def rowAction_(self, sender):
        state = self.owner.controller.ui_state()
        if not state.get('ready') or state.get('busy'):
            return
        key = self.owner.rows[sender.tag()]['id']
        row = next(row for row in diagnostic_rows(state) if row['id'] == key)
        if not row['action']:
            self.owner.render(state)
            return
        if row['issue_types']:
            self.owner.controller.prepare_fix(row['issue_types'])
        else:
            self.owner.handling.add(key)
            self.owner.expanded.add(key)
            self.owner.render(state)
            self.owner.document.scrollRectToVisible_(self.owner.handling_frames[key])

    def settings_(self, sender):
        if self.owner.controller.ui_state().get('busy'):
            return
        self.owner.settings_error = '' if open_network_settings() else '未能打开网络设置。请从苹果菜单进入“系统设置 > 网络”。'
        self.owner.render(self.owner.controller.ui_state())

    def confirm_(self, sender):
        repair = self.owner.controller.ui_state().get('repair') or {}
        self.owner.controller.confirm_fix(repair.get('token'), True)

    def dismiss_(self, sender):
        repair = self.owner.controller.ui_state().get('repair') or {}
        if repair.get('phase') == 'awaiting_confirmation':
            self.owner.controller.confirm_fix(repair.get('token'), False)
        self.owner.dismiss_repair()

    def windowDidResize_(self, notification):
        self.owner.render(self.owner.controller.ui_state())

    def windowShouldClose_(self, window):
        return not bool(self.owner.controller.ui_state().get('busy') and self.owner.sheet)


def label(text, size=13, secondary=False, bold=False):
    field = NSTextField.wrappingLabelWithString_(text)
    field.setFont_(NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold) if bold
                   else NSFont.systemFontOfSize_(size))
    field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    field.setLineBreakMode_(NSLineBreakByCharWrapping)
    field.setMaximumNumberOfLines_(0)
    return field


def text_height(field, width):
    bounds = NSString.stringWithString_(field.stringValue()).boundingRectWithSize_options_attributes_(
        NSSize(max(1, width - 4), 10000), 3, {NSFontAttributeName: field.font()})
    return max(20, math.ceil(bounds.size.height) + 6)


def place(parent, view, x, y, width, height):
    view.setFrame_(NSMakeRect(x, y, width, height))
    parent.addSubview_(view)
    return view


def separator(parent, x, y, width):
    line = NSBox.alloc().initWithFrame_(NSMakeRect(x, y, width, 1))
    line.setBoxType_(NSBoxSeparator)
    parent.addSubview_(line)


def button(title, target, action, symbol=None):
    control = NSButton.buttonWithTitle_target_action_(title, target, action)
    control.setBezelStyle_(NSBezelStyleRounded)
    if symbol:
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, title)
        if icon is not None:
            control.setImage_(icon)
            control.setImagePosition_(2)
    return control


COLORS = {'ok': NSColor.systemGreenColor, 'warning': NSColor.systemOrangeColor,
          'error': NSColor.systemRedColor, 'working': NSColor.controlAccentColor,
          'neutral': NSColor.secondaryLabelColor}
STATUS_SYMBOLS = {'ok': 'checkmark.circle.fill', 'warning': 'exclamationmark.circle.fill',
                  'error': 'xmark.circle.fill', 'working': 'arrow.triangle.2.circlepath',
                  'neutral': 'minus.circle'}


class DiagnosticPanel:
    def __init__(self, controller, logo_path, on_report):
        self.controller, self.on_report = controller, on_report
        self.logo = NSImage.alloc().initWithContentsOfFile_(str(logo_path))
        self.actions = PanelActions.alloc().init()
        self.actions.owner = self
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 900, 580), style, NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · 网络诊断')
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(760, 560))
        self.window.setBackgroundColor_(NSColor.windowBackgroundColor())
        self.window.setDelegate_(self.actions)
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 900, 580))
        self.window.setContentView_(self.root)
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setDrawsBackground_(False)
        self.document = FlippedView.alloc().init()
        self.scroll.setDocumentView_(self.document)
        self.rows, self.row_buttons = [], []
        self.expanded = set()
        self.handling = set()
        self.handling_buttons = []
        self.handling_frames = {}
        self.settings_error = ''
        self.details_buttons = []
        self.rows_height = 0
        self.sheet = None
        self.dismissed_token = None
        self.render(controller.ui_state())
        self.window.center()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def render(self, state):
        width, height = self.root.bounds().size
        offset = self.scroll.contentView().bounds().origin.y
        for view in list(self.root.subviews()):
            view.removeFromSuperview()
        title, subtitle = panel_summary(state)
        if self.logo is not None:
            logo_view = NSImageView.alloc().init()
            logo_view.setImage_(self.logo)
            place(self.root, logo_view, 22, 17, 36, 36)
        place(self.root, label('NetCare', 20, bold=True), 68, 10, 180, 30)
        tagline = label(subtitle, 11, secondary=True)
        tagline.setToolTip_(subtitle)
        tagline.setMaximumNumberOfLines_(1)
        tagline.setLineBreakMode_(4)
        place(self.root, tagline, 69, 40, width - 465, 22)
        self.profiles_button = button('网络档案', self.actions, 'profiles:', 'list.bullet.rectangle')
        self.profiles_button.setEnabled_(bool(state.get('ready') and not state.get('busy')))
        place(self.root, self.profiles_button, width - 390, 23, 124, 32)
        check_title = '重新检测' if state.get('snapshot', {}).get('last_check') else '检测'
        check = button(check_title, self.actions, 'check:', 'arrow.clockwise')
        self.check_button = check
        check.setToolTip_(check_title)
        check.setAccessibilityLabel_(check_title)
        check.setEnabled_(bool(state.get('ready') and not state.get('busy')))
        place(self.root, check, width - 260, 23, 96, 32)
        optimize_title = '查看方案' if state.get('guard', {}).get('proposal_ready') else '一键优化'
        self.optimize_button = button(optimize_title, self.actions, 'optimize:', 'wand.and.stars')
        self.optimize_button.setEnabled_(bool(optimization_issues(state)) or
            bool(state.get('ready') and not state.get('busy') and state.get('run_stage') == 'awaiting_authorization'))
        self.optimize_button.setBezelColor_(NSColor.controlAccentColor())
        self.optimize_button.setContentTintColor_(NSColor.controlAccentColor())
        self.optimize_button.setToolTip_('自动整理可安全处理的问题，确认方案后执行' if optimization_issues(state)
                                        else '检测完成后，可自动处理的问题会在这里汇总')
        place(self.root, self.optimize_button, width - 158, 23, 136, 32)
        headline = label(title, 17, bold=True)
        headline.setToolTip_(subtitle)
        place(self.root, headline, 22, 72, width - 300, 27)
        place(self.root, label(scan_caption(state), 11, secondary=True), width - 275, 77, 253, 23)
        top = 104
        separator(self.root, 24, top, width - 48)
        place(self.root, self.scroll, 0, top + 1, width, max(80, height - top - 49))
        doc_width = self.scroll.contentSize().width
        for view in list(self.document.subviews()):
            view.removeFromSuperview()
        self.rows = diagnostic_rows(state)
        if not state.get('busy'):
            self.handling.intersection_update(row['id'] for row in self.rows if row['action'])
        self.row_buttons = []
        self.details_buttons = []
        self.handling_buttons = []
        self.handling_frames = {}
        y = 0
        for index, row in enumerate(self.rows):
            y = self._row(row, index, y, doc_width, state)
        self.rows_height = y
        self.document.setFrame_(NSMakeRect(0, 0, doc_width, max(y, self.scroll.contentSize().height)))
        self.scroll.contentView().scrollToPoint_((0, min(offset, max(0, y - self.scroll.contentSize().height))))
        self.scroll.reflectScrolledClipView_(self.scroll.contentView())
        separator(self.root, 22, height - 46, width - 44)
        last = state.get('snapshot', {}).get('last_check')
        stamp = last.strftime('%H:%M:%S') if hasattr(last, 'strftime') else '尚未完成'
        agent = state.get('agent', {})
        guard = state.get('guard', {})
        self.guard_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(68, height - 36, 46, 26))
        self.guard_switch.setState_(1 if guard.get('enabled') else 0)
        self.guard_switch.setTarget_(self.actions)
        self.guard_switch.setAction_('guard:')
        self.guard_switch.setAccessibilityLabel_('网络守护')
        self.guard_switch.setToolTip_('启用后自动检查网络变化；修改配置仍需逐次确认。暂停可停止后续守护探测。')
        self.guard_switch.setEnabled_(bool(state.get('ready') and not guard.get('changing')))
        place(self.root, label('守护', 12), 22, height - 32, 42, 22)
        self.root.addSubview_(self.guard_switch)
        guard_status = ('暂停中' if guard.get('phase') == 'pausing' else '设置中' if guard.get('changing') else '已暂停' if not guard.get('enabled') else
                        '正在检查' if guard.get('phase') == 'checking' else
                        '待确认' if guard.get('proposal_ready') else '需关注' if guard.get('attention') else '守护中')
        caption = agent.get('journal_error') or ('上次处理需要核对，请重新检测' if agent.get('recovery_pending')
                                                else guard_status + ' · ' + stamp)
        if guard.get('error'):
            caption = '守护设置或检查需要关注'
        elif guard.get('enabled') and guard.get('event_source') == 'periodic_only':
            caption = guard_status + ' · 仅定时检查'
        guard_label = label(caption, 11, secondary=True)
        guard_label.setToolTip_(guard.get('error') or ('每小时最多 12 次自动检查；每次最多 60 秒、80 条检测命令、8 个访问请求。'))
        place(self.root, guard_label, 124, height - 31, width - 420, 22)
        self.records_button = button('处理记录', self.actions, 'records:', 'clock.arrow.circlepath')
        self.records_button.setEnabled_(bool(state.get('ready') and not state.get('busy')))
        place(self.root, self.records_button, width - 280, height - 38, 120, 30)
        report = button('诊断报告', self.actions, 'report:', 'doc.text')
        report.setEnabled_(bool(state.get('snapshot', {}).get('last_check')))
        place(self.root, report, width - 152, height - 38, 128, 30)
        self._render_repair(state)

    def _row(self, row, index, y, width, state):
        number = label(f"{row['number']:02d}", 13, bold=True)
        number.setTextColor_(COLORS[row['tone']]() if row['tone'] == 'working' else NSColor.secondaryLabelColor())
        place(self.document, number, 22, y + 11, 27, 23)
        if index < len(self.rows) - 1:
            arrow = NSImageView.alloc().init()
            arrow.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_('chevron.down', '下一项'))
            arrow.setContentTintColor_(NSColor.tertiaryLabelColor())
            place(self.document, arrow, 28, y + 37, 10, 9)
        content_width = width - 380
        place(self.document, label(row['title'], 13, bold=True), 62, y + 6, content_width, 23)
        status = label(row['state'], 12, bold=True)
        status.setTextColor_(NSColor.labelColor())
        mark = NSImageView.alloc().init()
        mark.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(STATUS_SYMBOLS[row['tone']], row['state']))
        mark.setContentTintColor_(COLORS[row['tone']]())
        place(self.document, mark, width - 310, y + 20, 15, 15)
        place(self.document, status, width - 288, y + 17, 80, 22)
        summary = label(row['summary'], 12, secondary=row['tone'] not in ('warning', 'error'))
        summary_height = text_height(summary, content_width)
        place(self.document, summary, 62, y + 30, content_width, summary_height)
        bottom = max(y + 57, y + 30 + summary_height + 6)
        if row['action']:
            control = button(row['action'], self.actions, 'rowAction:',
                             'wrench.and.screwdriver')
            control.setTag_(index)
            control.setToolTip_('确认方案后自动修复此项' if row['issue_types'] else '开始处理此项，必要时打开系统设置并重新检测')
            control.setEnabled_(bool(state.get('ready') and not state.get('busy')))
            place(self.document, control, width - 200, y + 13, 151, 30)
            self.row_buttons.append(control)
        expanded = row['id'] in self.expanded
        disclosure = button('', self.actions, 'details:', 'chevron.up' if expanded else 'chevron.down')
        disclosure.setTag_(index)
        disclosure.setBordered_(False)
        disclosure.setToolTip_(('收起' if expanded else '展开') + row['title'] + '的原因与详细结果')
        disclosure.setAccessibilityLabel_(disclosure.toolTip())
        place(self.document, disclosure, width - 43, y + 14, 27, 28)
        self.details_buttons.append(disclosure)
        if expanded:
            guided = row['id'] in self.handling and row['action']
            if guided:
                start = bottom
                plan = handling_plan(row['id'], state)
                heading = label('处理步骤', 12, bold=True)
                place(self.document, heading, 62, bottom + 6, width - 90, 22)
                steps = label(plan['steps'] + ('\n' + self.settings_error if self.settings_error and plan['settings'] else ''), 12)
                step_height = text_height(steps, width - 90)
                place(self.document, steps, 62, bottom + 32, width - 90, step_height)
                bottom += step_height + 40
                x = 62
                controls = ([('打开网络设置', 'settings:', 'gearshape')] if plan['settings'] else [])
                controls.append(('重新检测', 'check:', 'arrow.clockwise'))
                for title, action, symbol in controls:
                    control = button(title, self.actions, action, symbol)
                    control.setEnabled_(bool(state.get('ready') and not state.get('busy')))
                    place(self.document, control, x, bottom, 150, 30)
                    self.handling_buttons.append(control)
                    x += 160
                bottom += 38
                self.handling_frames[row['id']] = NSMakeRect(62, start, width - 90, bottom - start)
            text = ('检测详情\n' + row['evidence'] if guided else
                    row['explanation'] + ('\n\n检测详情\n' + row['evidence'] if row['evidence'] else ''))
            details = label(text, 12, secondary=True)
            details_height = text_height(details, width - 90)
            place(self.document, details, 62, bottom + 5, width - 90, details_height)
            bottom += details_height + 15
        separator(self.document, 62, bottom, width - 86)
        return bottom + 1

    def _render_repair(self, state):
        repair = state.get('repair')
        if not repair or repair['token'] == self.dismissed_token:
            return
        if self.sheet is None:
            self.show()
            self.sheet = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 660, 440), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
            self.sheet.setReleasedWhenClosed_(False)
            self.sheet.setTitle_('NetCare · 网络优化')
            self.sheet_root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 660, 440))
            self.sheet.setContentView_(self.sheet_root)
            self.sheet_scroll = NSScrollView.alloc().init()
            self.sheet_scroll.setHasVerticalScroller_(True)
            self.sheet_scroll.setDrawsBackground_(False)
            self.sheet_document = FlippedView.alloc().init()
            self.sheet_scroll.setDocumentView_(self.sheet_document)
            self.window.beginSheet_completionHandler_(self.sheet, None)
        for view in list(self.sheet_root.subviews()):
            view.removeFromSuperview()
        phase, outcome = repair['phase'], repair.get('outcome')
        titles = {'preparing': '正在分析可优化的问题', 'awaiting_confirmation': '已为你整理优化方案',
                  'preflight': '再次确认当前网络', 'snapshot': '备份原来的设置', 'applying': '正在优化网络',
                  'verifying': '正在检查优化效果', 'rollback': '正在恢复原来的设置'}
        finished_titles = {'verified': '所选问题已处理，检查通过', 'rolled_back': '优化未成功，已恢复原来的设置',
                           'rollback_failed': '恢复未完成，需要手动处理', 'blocked': '当前条件不适合自动优化',
                           'cancelled': '已取消优化', 'failed': '优化未完成'}
        title = finished_titles.get(outcome, '修复结果待确认') if phase == 'finished' else titles.get(phase, '正在修复')
        place(self.sheet_root, label(title, 19, bold=True), 26, 24, 570, 30)
        running = phase not in ('finished', 'awaiting_confirmation')
        if running:
            spinner = NSProgressIndicator.alloc().init()
            spinner.setStyle_(NSProgressIndicatorStyleSpinning)
            spinner.setIndeterminate_(True)
            place(self.sheet_root, spinner, 609, 27, 20, 20)
            spinner.startAnimation_(None)
        hint = '将处理下面的问题。完成后重新检测；未成功时尝试恢复原设置。' if phase == 'awaiting_confirmation' else (
            '请稍候，完成后会自动检查网络是否恢复。' if running else '本次处理结果与过程记录如下。')
        place(self.sheet_root, label(hint, 12, secondary=True), 26, 62, 608, 32)
        separator(self.sheet_root, 26, 100, 608)
        place(self.sheet_root, self.sheet_scroll, 24, 112, 612, 250)
        for view in list(self.sheet_document.subviews()):
            view.removeFromSuperview()
        text = '\n\n'.join(repair.get('plans', [])) if phase == 'awaiting_confirmation' else '\n\n'.join(
            f"{event['time']}  {event['message']}" for event in repair.get('events', []))
        if phase == 'awaiting_confirmation':
            from datetime import datetime
            expires = repair.get('expires_at')
            deadline = datetime.fromtimestamp(expires).strftime('%H:%M:%S') if expires else '本次任务'
            text += ('\n\n授权范围：仅上列网络服务和目标配置。\n有效期：' + deadline
                     + '\n可能短暂中断连接。执行前保存原值；验证失败时尝试恢复，恢复也可能失败。')
        if phase == 'finished' and repair.get('results'):
            text += '\n\n' + '\n'.join(repair['results'])
        body = label(text, 13)
        body_width = self.sheet_scroll.contentSize().width - 12
        body_height = text_height(body, body_width)
        self.sheet_document.setFrame_(NSMakeRect(0, 0, body_width + 12, max(250, body_height)))
        place(self.sheet_document, body, 4, 0, body_width, body_height)
        self.sheet_scroll.contentView().scrollToPoint_((0, max(0, body_height - 250) if running else 0))
        self.sheet_scroll.reflectScrolledClipView_(self.sheet_scroll.contentView())
        if phase == 'awaiting_confirmation':
            cancel = button('取消', self.actions, 'dismiss:')
            place(self.sheet_root, cancel, 406, 383, 98, 32)
            confirm = button('开始优化', self.actions, 'confirm:', 'wand.and.stars')
            confirm.setKeyEquivalent_('\r')
            place(self.sheet_root, confirm, 510, 383, 126, 32)
        elif phase == 'finished':
            close = button('返回诊断', self.actions, 'dismiss:')
            close.setEnabled_(not bool(state.get('busy')))
            close.setKeyEquivalent_('\r')
            place(self.sheet_root, close, 510, 383, 126, 32)

    def dismiss_repair(self):
        repair = self.controller.ui_state().get('repair') or {}
        if repair.get('phase') not in ('finished', 'awaiting_confirmation'):
            return
        self.dismissed_token = repair.get('token')
        if self.sheet:
            self.window.endSheet_(self.sheet)
            self.sheet.orderOut_(None)
            self.sheet = None
