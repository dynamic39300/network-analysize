"""Modeless health-profile editor. All persistence stays on controller workers."""
import copy
from datetime import datetime

from AppKit import (
    NSAlert, NSAlertFirstButtonReturn, NSApplication, NSBackingStoreBuffered, NSColor,
    NSFont, NSImageOnly, NSModalResponseOK, NSOpenPanel, NSPopUpButton,
    NSScreen, NSScrollView, NSSize, NSTextField, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject

from .panel import FlippedView, button, label, place, separator
from .profiles import PATHS, REQUIREMENTS, SCHEMA, SCOPES, new_id, normalize_document
from .report_window import ReportNativeWindow


def icon_button(title, symbol, actions, selector):
    control = button(title, actions, selector, symbol)
    if control.image() is not None:
        control.setImagePosition_(NSImageOnly)
    control.setToolTip_(title)
    control.setAccessibilityLabel_(title)
    return control


def entry(value, actions, tooltip):
    control = NSTextField.alloc().init()
    control.setStringValue_(str(value))
    control.setFont_(NSFont.systemFontOfSize_(12))
    control.setDelegate_(actions)
    control.setToolTip_(tooltip)
    control.setAccessibilityLabel_(tooltip)
    return control


def popup(options, selected, actions):
    control = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 26), False)
    control.addItemsWithTitles_(list(options.values()))
    control.selectItemAtIndex_(list(options).index(selected))
    control.setTarget_(actions)
    control.setAction_('edit:')
    return control


class ProfileActions(NSObject):
    def controlTextDidChange_(self, notification):
        self.owner.mark_dirty()

    def edit_(self, sender):
        self.owner.mark_dirty()

    def choose_(self, sender):
        index = sender.indexOfSelectedItem()
        if self.owner.confirm_discard():
            self.owner.load(self.owner.profiles[index])
        else:
            sender.selectItemAtIndex_(self.owner.selected_index())

    def new_(self, sender):
        if self.owner.confirm_discard():
            self.owner.load({'name': '新网络档案', 'targets': [self.owner.empty_target()]})

    def duplicate_(self, sender):
        try:
            draft = self.owner.read_document()
        except ValueError as exc:
            self.owner.show_error(str(exc))
            return
        draft['name'] = draft['name'][:74] + ' 副本'
        for target in draft['targets']:
            target['id'] = new_id('target')
        self.owner.load(draft)

    def remove_(self, sender):
        owner = self.owner
        if owner.profile_id and owner.confirm('移除网络档案？', '此档案将从列表中移除；历史版本和处理记录会保留。', '移除'):
            owner.controller.change_profile('remove', profile_id=owner.profile_id)

    def add_(self, sender):
        self.owner.add_row(self.owner.empty_target())
        self.owner.mark_dirty()
        self.owner.layout()
        self.owner.document.scrollRectToVisible_(self.owner.rows[-1]['name'].frame())

    def deleteTarget_(self, sender):
        self.owner.rows.pop(sender.tag())
        self.owner.mark_dirty()
        self.owner.layout()

    def save_(self, sender):
        owner = self.owner
        owner.window.makeFirstResponder_(None)
        try:
            document = normalize_document(owner.read_document(), retain_ids=True)
            owner.controller.change_profile('save', document=document, profile_id=owner.profile_id,
                                            revision=owner.revision, imported=owner.imported)
        except (TypeError, ValueError) as exc:
            owner.show_error(str(exc))

    def activate_(self, sender):
        if self.owner.profile_id and self.owner.confirm_discard():
            self.owner.controller.change_profile('activate', profile_id=self.owner.profile_id)

    def import_(self, sender):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(['json'])
        panel.setPrompt_('导入为草稿')
        if panel.runModal() == NSModalResponseOK:
            self.owner.controller.import_profile(str(panel.URLs()[0].path()))

    def export_(self, sender):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setCanCreateDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_('导出到此文件夹')
        if panel.runModal() == NSModalResponseOK:
            self.owner.controller.export_profile(self.owner.profile_id, str(panel.URLs()[0].path()))

    def windowDidResize_(self, notification):
        self.owner.layout()

    def windowShouldClose_(self, window):
        confirmed = self.owner.confirm_discard()
        if confirmed:
            self.owner.dirty = False
        return confirmed


class ProfilesWindow:
    def __init__(self, controller):
        self.controller = controller
        self.profiles, self.rows = [], []
        self.profile_id = self.revision = self.active_id = None
        self.dirty = self.imported = False
        self.assessment = {}
        self.actions = ProfileActions.alloc().init()
        self.actions.owner = self
        screen = NSScreen.mainScreen().visibleFrame().size
        width, height = min(940, screen.width - 40), min(680, screen.height - 40)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · 网络档案')
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(min(760, width), min(560, height)))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        self.window.setContentView_(self.root)
        self.picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 28), False)
        self.picker.setTarget_(self.actions)
        self.picker.setAction_('choose:')
        self.picker.setAccessibilityLabel_('网络档案')
        self.name_field = entry('', self.actions, '档案名称')
        self.status_label = label('', 11, secondary=True)
        self.status_label.setMaximumNumberOfLines_(1)
        self.error_label = label('', 11, secondary=True)
        self.error_label.setMaximumNumberOfLines_(2)
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.document = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width - 44, 300))
        self.scroll.setDocumentView_(self.document)
        self.new_button = icon_button('新建档案', 'plus', self.actions, 'new:')
        self.duplicate_button = icon_button('复制为新档案', 'doc.on.doc', self.actions, 'duplicate:')
        self.remove_button = icon_button('移除档案', 'trash', self.actions, 'remove:')
        self.import_button = icon_button('导入档案', 'square.and.arrow.down', self.actions, 'import:')
        self.export_button = icon_button('导出已保存档案（含原始地址）', 'square.and.arrow.up', self.actions, 'export:')
        self.add_button = button('添加目标', self.actions, 'add:', 'plus')
        self.activate_button = button('使用此档案', self.actions, 'activate:', 'checkmark')
        self.save_button = button('保存并使用', self.actions, 'save:', 'checkmark.circle')
        self.load({'name': '新网络档案', 'targets': [self.empty_target()]})
        self.dirty = False
        self.window.setDelegate_(self.actions)
        self.window.center()

    @staticmethod
    def empty_target():
        return {'id': new_id('target'), 'name': '', 'url': '', 'expected_path': 'system',
                'when': 'always', 'requirement': 'transport', 'timeout': 8}

    @staticmethod
    def confirm(title, message, accept):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_(accept)
        alert.addButtonWithTitle_('取消')
        return alert.runModal() == NSAlertFirstButtonReturn

    def confirm_discard(self):
        return not self.dirty or self.confirm('放弃未保存的修改？', '系统网络配置尚未更改。', '放弃修改')

    def selected_index(self):
        return next((i for i, p in enumerate(self.profiles) if p['id'] == self.profile_id), -1)

    def show(self, payload):
        self.load_payload(payload)
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def load_payload(self, payload):
        self.profiles = copy.deepcopy(payload['profiles'])
        self.active_id = payload['active_id']
        self.assessment = payload['assessment']
        self.picker.removeAllItems()
        self.picker.addItemsWithTitles_([('当前 · ' if p['id'] == self.active_id else '') + p['name'] + f' · {i + 1}'
                                       for i, p in enumerate(self.profiles)])
        if not self.dirty or payload.get('replace'):
            current = next((p for p in self.profiles if p['id'] == self.active_id), None)
            if current:
                self.load(current)
        self.picker.selectItemAtIndex_(self.selected_index())
        self.update_state(self.controller.ui_state())

    def import_draft(self, document):
        if self.confirm_discard():
            self.load(document, imported=True)
            self.window.makeKeyAndOrderFront_(None)

    def load(self, profile, imported=False):
        self.profile_id, self.revision = profile.get('id'), profile.get('revision')
        self.source = profile.get('source')
        self.imported = imported
        self.dirty = self.profile_id is None
        self.name_field.setStringValue_(profile['name'])
        self.rows = []
        for target in profile['targets']:
            self.add_row(target)
        self.picker.selectItemAtIndex_(self.selected_index())
        self.picker.setToolTip_(profile['name'])
        self.error_label.setStringValue_('')
        self.window.setDocumentEdited_(self.dirty)
        self.layout()
        self.update_state(self.controller.ui_state())

    def add_row(self, target):
        row = {'id': target['id'], 'name': entry(target['name'], self.actions, '目标名称'),
               'url': entry(target['url'], self.actions, 'HTTP(S) 目标地址'),
               'expected_path': popup(PATHS, target['expected_path'], self.actions),
               'when': popup(SCOPES, target['when'], self.actions),
               'requirement': popup(REQUIREMENTS, target['requirement'], self.actions),
               'timeout': entry(target['timeout'], self.actions, '访问超时（1–30 秒）'),
               'result': label('', 11, secondary=True),
               'delete': icon_button('移除保护目标', 'minus.circle', self.actions, 'deleteTarget:')}
        row['result'].setMaximumNumberOfLines_(1)
        row['expected_path'].setToolTip_('不经代理仍可能经过 VPN；系统路径的 PAC 需要单独验证')
        row['when'].setToolTip_('仅在环境已确认满足条件时访问；未知环境不会视为正常')
        row['requirement'].setToolTip_('网络可达接受认证响应；服务正常响应需要 HTTP 2xx，不验证响应正文')
        self.rows.append(row)

    def read_document(self):
        targets = []
        for row in self.rows:
            target = {key: str(row[key].stringValue()) for key in ('name', 'url')}
            try:
                timeout = int(row['timeout'].stringValue())
            except ValueError:
                raise ValueError('访问超时须为 1–30 秒') from None
            target.update(id=row['id'], timeout=timeout)
            for field, options in (('expected_path', PATHS), ('when', SCOPES), ('requirement', REQUIREMENTS)):
                target[field] = list(options)[row[field].indexOfSelectedItem()]
            targets.append(target)
        return {'schema': SCHEMA, 'name': str(self.name_field.stringValue()), 'targets': targets}

    def mark_dirty(self):
        self.dirty = True
        self.window.setDocumentEdited_(True)
        self.error_label.setStringValue_('')
        self.update_state(self.controller.ui_state())

    def show_error(self, message):
        self.error_label.setStringValue_(message)
        self.error_label.setToolTip_(message)

    def update_state(self, state):
        profile = state.get('agent', {}).get('profile')
        if profile:
            self.assessment = profile
        idle = bool(state.get('ready') and not state.get('busy'))
        for control in (self.picker, self.new_button, self.duplicate_button, self.import_button):
            control.setEnabled_(idle)
        self.remove_button.setEnabled_(bool(idle and self.profile_id and self.profile_id != self.active_id))
        self.export_button.setEnabled_(bool(idle and self.profile_id and not self.dirty))
        self.activate_button.setEnabled_(bool(idle and self.profile_id and self.profile_id != self.active_id))
        self.save_button.setEnabled_(idle and self.dirty)
        self.add_button.setEnabled_(idle and len(self.rows) < 32)
        self.name_field.setEnabled_(idle)
        results = {t['id']: t for t in self.assessment.get('targets', [])}
        same = (not self.dirty and self.assessment.get('id') == self.profile_id
                and self.assessment.get('revision') == self.revision)
        for row in self.rows:
            for field in ('name', 'url', 'expected_path', 'when', 'requirement', 'timeout', 'delete'):
                row[field].setEnabled_(idle and (field != 'delete' or len(self.rows) > 1))
            result = results.get(row['id'], {}) if same else {}
            text = ((result.get('label', '尚未验证') + ' · ' + result.get('summary', '')) if result
                    else '未保存的预期' if self.dirty else '此档案尚未检测')
            row['result'].setStringValue_(text)
            row['result'].setToolTip_(text)
            row['result'].setTextColor_(NSColor.systemGreenColor() if result.get('state') == 'healthy'
                                       else NSColor.secondaryLabelColor())
        active = self.profile_id == self.active_id and self.profile_id
        caption = ('导入草稿' if self.imported else '未保存') if self.dirty else ('当前使用' if active else '未启用')
        if self.revision:
            caption += f' · 版本 {self.revision}'
        if not self.dirty:
            caption += ' · ' + {'configured_targets': '来自现有配置', 'user': '用户确认',
                                'user_import': '导入后确认'}.get(self.source, '来源未标注')
        if same:
            caption += f" · {self.assessment.get('covered', 0)}/{self.assessment.get('total', 0)} 个目标符合预期"
            verified = self.assessment.get('last_verified')
            if verified:
                caption += ' · 最近验证 ' + datetime.fromtimestamp(verified).strftime('%m-%d %H:%M')
        self.status_label.setStringValue_(caption)
        self.status_label.setToolTip_(caption)
        error = state.get('agent', {}).get('profile_error')
        if error:
            self.show_error(error)

    def layout(self):
        width, height = self.root.bounds().size
        for view in list(self.root.subviews()):
            view.removeFromSuperview()
        place(self.root, label('网络档案', 19, bold=True), 22, 14, 160, 30)
        place(self.root, self.status_label, 22, 48, width - 44, 22)
        place(self.root, self.picker, 22, 80, width - 282, 28)
        for index, control in enumerate((self.new_button, self.duplicate_button, self.remove_button,
                                          self.import_button, self.export_button)):
            place(self.root, control, width - 248 + index * 46, 79, 40, 30)
        place(self.root, label('名称', 12, secondary=True), 22, 124, 44, 24)
        place(self.root, self.name_field, 74, 122, width - 96, 26)
        place(self.root, label('保护目标', 13, bold=True), 22, 166, 160, 26)
        place(self.root, self.add_button, width - 142, 162, 120, 30)
        separator(self.root, 22, 200, width - 44)
        place(self.root, self.scroll, 22, 209, width - 44, height - 294)
        row_width = self.scroll.contentSize().width
        compact = row_width < 620
        row_height = 176 if compact else 128
        for view in list(self.document.subviews()):
            view.removeFromSuperview()
        for index, row in enumerate(self.rows):
            y = index * row_height
            row['delete'].setTag_(index)
            place(self.document, label(f'{index + 1:02d}', 11, secondary=True), 0, y + 4, 26, 23)
            place(self.document, row['name'], 32, y, 152, 26)
            place(self.document, row['url'], 196, y, row_width - 242, 26)
            place(self.document, row['delete'], row_width - 36, y - 1, 34, 28)
            if compact:
                span = (row_width - 52) / 2
                for n, (key, title) in enumerate((('expected_path', '预期路径'), ('when', '适用环境'),
                                                 ('requirement', '成功条件'), ('timeout', '超时（秒）'))):
                    x, top = 32 + n % 2 * (span + 12), y + 32 + n // 2 * 48
                    place(self.document, label(title, 10, secondary=True), x, top, span, 18)
                    place(self.document, row[key], x - 4, top + 18, span, 26)
            else:
                for title, x in (('预期路径', 32), ('适用环境', 194), ('成功条件', 374), ('超时（秒）', 558)):
                    place(self.document, label(title, 10, secondary=True), x, y + 32, 120, 18)
                place(self.document, row['expected_path'], 28, y + 50, 160, 26)
                place(self.document, row['when'], 190, y + 50, 178, 26)
                place(self.document, row['requirement'], 370, y + 50, 178, 26)
                place(self.document, row['timeout'], 558, y + 50, 52, 26)
            place(self.document, row['result'], 32, y + row_height - 44, row_width - 38, 22)
            separator(self.document, 32, y + row_height - 9, row_width - 38)
        self.document.setFrameSize_(NSSize(row_width, max(len(self.rows) * row_height, self.scroll.contentSize().height)))
        separator(self.root, 22, height - 74, width - 44)
        place(self.root, self.error_label, 22, height - 64, width - 370, 52)
        place(self.root, self.activate_button, width - 328, height - 51, 140, 32)
        place(self.root, self.save_button, width - 176, height - 51, 154, 32)
