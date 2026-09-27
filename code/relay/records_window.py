"""Searchable recent records with a separate complete unresolved-task list."""
import copy

from AppKit import (NSApplication, NSBackingStoreBuffered, NSColor, NSFont, NSPopUpButton,
    NSScrollView, NSSearchField, NSSegmentedControl, NSSize, NSTableColumn, NSTableView, NSTextView, NSViewWidthSizable,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSObject

from .agent_records import OUTCOMES, STAGES, records_summary
from .panel import FlippedView, button, label, place, separator
from .profiles_window import icon_button
from .report_window import ReportNativeWindow
from .task_details import detail_text


class RecordActions(NSObject):
    def numberOfRowsInTableView_(self, table):
        return len(self.owner.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        record = self.owner.rows[row]
        if str(column.identifier()) == 'state':
            return OUTCOMES.get(record.get('outcome'), STAGES.get(record['stage'], '状态未确认'))
        events = record.get('events', [])
        return events[0]['time'].replace('T', ' ') if events else record['id']

    def tableViewSelectionDidChange_(self, notification):
        self.owner.select()

    def controlTextDidChange_(self, notification):
        self.owner.filter()

    def filter_(self, sender):
        self.owner.filter()

    def inspect_(self, sender):
        selected = self.owner.selected()
        if selected and selected.get('has_receipt') and self.owner.inspect.isEnabled():
            self.owner.controller.review_receipt(selected['id'])

    def reload_(self, sender):
        self.owner.controller.show_agent_records()

    def report_(self, sender):
        if self.owner.current_detail and self.owner.on_report:
            self.owner.on_report(copy.deepcopy(self.owner.current_detail))

    def detailMode_(self, sender):
        self.owner.render_detail(reset=True)

    def history_(self, sender):
        self.owner.desktop.show_history()

    def compareHistory_(self, sender):
        self.owner.desktop.compare_latest()

    def bundle_(self, sender):
        self.owner.desktop.prepare_pro_export()

    def windowDidResize_(self, notification):
        self.owner.layout()


class RecordsWindow:
    def __init__(self, controller, on_report=None, desktop=None):
        self.controller = controller
        self.desktop = desktop
        self.offline_available = bool(desktop)
        self.on_report, self.current_detail = on_report, None
        self.payload, self.rows, self.state = {}, [], {}
        self.detail_id = None
        self.actions = RecordActions.alloc().init()
        self.actions.owner = self
        self.history = icon_button('本地诊断历史 · Pro', 'clock', self.actions, 'history:')
        self.compare = icon_button('对比最近两次诊断 · Pro', 'square.on.square', self.actions, 'compareHistory:')
        self.bundle = icon_button('导出诊断包 · Pro', 'square.and.arrow.up', self.actions, 'bundle:')
        self.detail_mode = NSSegmentedControl.alloc().init()
        self.detail_mode.setSegmentCount_(3)
        for index, title in enumerate(('时间线', '调查依据', '修改回读')):
            self.detail_mode.setLabel_forSegment_(title, index)
            self.detail_mode.setWidth_forSegment_(86, index)
        self.detail_mode.setSelectedSegment_(0)
        self.detail_mode.setTarget_(self.actions)
        self.detail_mode.setAction_('detailMode:')
        self.detail_mode.setAccessibilityLabel_('任务详情分类')
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 900, 640), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · 处理记录')
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(760, 560))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 900, 640))
        self.window.setContentView_(self.root)
        self.search = NSSearchField.alloc().init()
        self.search.setPlaceholderString_('搜索状态、时间、任务或事件')
        self.search.setDelegate_(self.actions)
        self.search.setAccessibilityLabel_('搜索处理记录')
        self.scope = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 200, 28), False)
        self.scope.addItemsWithTitles_(['最近 50 次', '全部待核对任务'])
        self.scope.setTarget_(self.actions)
        self.scope.setAction_('filter:')
        self.scope.setAccessibilityLabel_('处理记录范围')
        self.reload = icon_button('重新读取记录', 'arrow.clockwise', self.actions, 'reload:')
        self.table = NSTableView.alloc().init()
        self.table.setRowHeight_(44)
        self.table.setAllowsMultipleSelection_(False)
        self.table.setDataSource_(self.actions)
        self.table.setDelegate_(self.actions)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setAccessibilityLabel_('处理记录列表')
        for name, title, width in [('state', '结果', 130), ('time', '时间 / 任务', 180)]:
            column = NSTableColumn.alloc().initWithIdentifier_(name)
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            column.dataCell().setWraps_(True)
            column.dataCell().setUsesSingleLineMode_(False)
            self.table.addTableColumn_(column)
        self.list_scroll = NSScrollView.alloc().init()
        self.list_scroll.setHasVerticalScroller_(True)
        self.list_scroll.setHasHorizontalScroller_(True)
        self.list_scroll.setAutohidesScrollers_(True)
        self.list_scroll.setDocumentView_(self.table)
        self.detail_scroll = NSScrollView.alloc().init()
        self.detail_scroll.setHasVerticalScroller_(True)
        self.detail_scroll.setAutohidesScrollers_(True)
        self.detail = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 470, 420))
        self.detail.setEditable_(False)
        self.detail.setSelectable_(True)
        self.detail.setRichText_(False)
        self.detail.setFont_(NSFont.systemFontOfSize_(13))
        self.detail.setTextColor_(NSColor.textColor())
        self.detail.setBackgroundColor_(NSColor.textBackgroundColor())
        self.detail.setTextContainerInset_(NSSize(14, 12))
        self.detail.setVerticallyResizable_(True)
        self.detail.setHorizontallyResizable_(False)
        self.detail.setMinSize_(NSSize(0, 0))
        self.detail.setMaxSize_(NSSize(1000000, 1000000))
        self.detail.setAutoresizingMask_(NSViewWidthSizable)
        self.detail.textContainer().setWidthTracksTextView_(True)
        self.detail.setAccessibilityLabel_('脱敏任务时间线')
        self.detail_scroll.setDocumentView_(self.detail)
        self.inspect = button('查看私有收据', self.actions, 'inspect:', 'doc.text.magnifyingglass')
        self.report = icon_button('打开脱敏任务报告', 'doc.text', self.actions, 'report:')
        self.report.setEnabled_(False)
        self.count = label('', 11, secondary=True)
        self.window.setDelegate_(self.actions)
        self.window.center()
        self.layout()

    def show(self, payload):
        self.load_payload(payload)
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def load_payload(self, payload):
        self.payload = copy.deepcopy(payload)
        self.refresh_detail = True
        self.filter()

    def selected(self):
        index = self.table.selectedRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def filter(self):
        selected_id = (self.selected() or {}).get('id')
        rows = self.payload.get('pending' if self.scope.indexOfSelectedItem() == 1 else 'records', [])
        search = str(self.search.stringValue()).strip().casefold()
        self.rows = [row for row in rows if not search or search in
                     (row['id'] + ' ' + records_summary({'records': [row]})).casefold()]
        self.table.reloadData()
        if self.rows:
            from Foundation import NSIndexSet
            index = next((i for i, row in enumerate(self.rows) if row['id'] == selected_id), 0)
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
        else:
            self.table.deselectAll_(None)
        self.count.setStringValue_(f"{len(self.rows)} 条显示 · {len(self.payload.get('pending', []))} 项待核对")
        self.select()

    def select(self):
        selected = self.selected()
        same = bool(selected and selected.get('id') == self.detail_id)
        if same and not getattr(self, 'refresh_detail', False):
            self.update_state(self.state)
            return
        self.refresh_detail = False
        self.detail_id = selected.get('id') if selected else None
        if not same:
            self.current_detail = None
            self.report.setEnabled_(False)
            self.detail.setString_((selected['id'] + '\n\n' + records_summary({'records': [selected]})) if selected
                                  else '没有匹配的处理记录')
            self.detail.sizeToFit()
            self.detail.scrollRangeToVisible_((0, 0))
        self.update_state(self.state)
        if self.detail_id and self.state.get('ready'):
            self.controller.show_task_detail(self.detail_id)

    def show_detail(self, payload):
        if payload['record'].get('id') != self.detail_id:
            return
        self.current_detail = copy.deepcopy(payload)
        self.report.setEnabled_(self.on_report is not None)
        self.render_detail()

    def render_detail(self, reset=False):
        if not self.current_detail:
            return
        offset = self.detail_scroll.contentView().bounds().origin.y
        section = ('timeline', 'investigation', 'changes')[self.detail_mode.selectedSegment()]
        self.detail.setString_(detail_text(self.current_detail, section))
        self.detail.sizeToFit()
        maximum = max(0, self.detail.frame().size.height - self.detail_scroll.contentSize().height)
        self.detail_scroll.contentView().scrollToPoint_((0, 0 if reset else min(offset, maximum)))
        self.detail_scroll.reflectScrolledClipView_(self.detail_scroll.contentView())

    def focus_run(self, run_id):
        from Foundation import NSIndexSet
        self.search.setStringValue_('')
        for scope, key in enumerate(('records', 'pending')):
            if any(row['id'] == run_id for row in self.payload.get(key, [])):
                self.scope.selectItemAtIndex_(scope)
                self.filter()
                index = next(i for i, row in enumerate(self.rows) if row['id'] == run_id)
                self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
                self.table.scrollRowToVisible_(index)
                self.select()
                return

    def update_state(self, state):
        self.state = state
        self.inspect.setEnabled_(bool(state.get('ready') and not state.get('busy')
                                      and (self.selected() or {}).get('has_receipt')))
        self.reload.setEnabled_(bool(state.get('ready')))
        if not state.get('ready'):
            self.count.setStringValue_('核心未连接 · 仅显示已读取记录')

    def layout(self):
        width, height = self.root.bounds().size
        for child in list(self.root.subviews()):
            child.removeFromSuperview()
        place(self.root, label('处理记录', 18, bold=True), 22, 16, width - 172, 28)
        if self.desktop:
            for index, control in enumerate((self.history, self.compare, self.bundle)):
                place(self.root, control, width - 134 + index * 40, 16, 32, 30)
        place(self.root, self.scope, 22, 57, 188, 28)
        place(self.root, self.search, 224, 57, width - 286, 28)
        place(self.root, self.reload, width - 54, 56, 32, 30)
        if width < 730:
            list_height = min(140, (height - 218) * 0.42)
            place(self.root, self.list_scroll, 22, 101, width - 44, list_height)
            place(self.root, self.detail_mode, 22, 109 + list_height, 266, 28)
            place(self.root, self.detail_scroll, 22, 143 + list_height, width - 44, height - 214 - list_height)
        else:
            place(self.root, self.list_scroll, 22, 101, 320, height - 172)
            place(self.root, self.detail_mode, 354, 99, 266, 28)
            place(self.root, self.detail_scroll, 354, 135, width - 376, height - 206)
        self.detail.setFrameSize_(NSSize(self.detail_scroll.contentSize().width,
                                        max(self.detail.frame().size.height, self.detail_scroll.contentSize().height)))
        separator(self.root, 22, height - 60, width - 44)
        place(self.root, self.report, 22, height - 47, 32, 32)
        place(self.root, self.count, 64, height - 42, width - 272, 24)
        place(self.root, self.inspect, width - 197, height - 47, 175, 32)
