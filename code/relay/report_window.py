"""A bounded, modeless report reader. Only receives redacted snapshots."""
import copy
import json

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSColor, NSEventModifierFlagCommand,
    NSFont, NSFontAttributeName, NSFontWeightSemibold, NSForegroundColorAttributeName,
    NSImage, NSImageView, NSParagraphStyleAttributeName, NSPasteboard,
    NSPasteboardTypeString, NSScreen, NSScrollView, NSSegmentedControl, NSSize,
    NSTextView, NSViewWidthSizable, NSWindow, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
)
from Foundation import NSMutableAttributedString, NSMutableParagraphStyle, NSMakeRect, NSObject

from .panel import FlippedView, button, label, place, separator
from .presentation import diagnostic_rows


class ReportNativeWindow(NSWindow):
    def cancelOperation_(self, sender):
        self.performClose_(sender)

    def performKeyEquivalent_(self, event):
        characters = event.charactersIgnoringModifiers() or ''
        if characters == '\x1b' or (event.modifierFlags() & NSEventModifierFlagCommand and characters.lower() == 'w'):
            self.performClose_(None)
            return True
        return objc.super(ReportNativeWindow, self).performKeyEquivalent_(event)


class ReportActions(NSObject):
    def handle_(self, sender):
        if self.owner.on_handle:
            self.owner.window.performClose_(sender)
            self.owner.on_handle()

    def close_(self, sender):
        self.owner.window.performClose_(sender)

    def format_(self, sender):
        self.owner.update_body()

    def copy_(self, sender):
        clipboard = NSPasteboard.generalPasteboard()
        clipboard.clearContents()
        clipboard.setString_forType_(self.owner.current_text, NSPasteboardTypeString)
        self.owner.copy_button.setTitle_('已复制')

    def windowDidResize_(self, notification):
        self.owner.layout()


class ReportWindow:
    def __init__(self, logo_path, on_handle=None, title='检测报告', summary_builder=None, caption=None):
        self.on_handle = on_handle
        self.title, self.summary_builder = title, summary_builder
        self.caption = caption
        self.snapshot = {}
        self.current_text = ''
        self.logo = NSImage.alloc().initWithContentsOfFile_(str(logo_path))
        self.actions = ReportActions.alloc().init()
        self.actions.owner = self
        screen = NSScreen.mainScreen().visibleFrame().size
        width, height = min(800, screen.width - 40), min(620, screen.height - 40)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · ' + title)
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(min(640, width), min(440, height)))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        self.window.setContentView_(self.root)
        self.tabs = NSSegmentedControl.alloc().init()
        self.tabs.setSegmentCount_(2)
        self.tabs.setLabel_forSegment_('处理时间线' if summary_builder else '检测概览', 0)
        self.tabs.setLabel_forSegment_('详细数据', 1)
        self.tabs.setWidth_forSegment_(108, 0)
        self.tabs.setWidth_forSegment_(108, 1)
        self.tabs.setSelectedSegment_(0)
        self.tabs.setTarget_(self.actions)
        self.tabs.setAction_('format:')
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setDrawsBackground_(True)
        self.scroll.setBackgroundColor_(NSColor.textBackgroundColor())
        self.text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width - 40, 400))
        self.text_view.setEditable_(False)
        self.text_view.setSelectable_(True)
        self.text_view.setRichText_(False)
        self.text_view.setBackgroundColor_(NSColor.textBackgroundColor())
        self.text_view.setTextContainerInset_(NSSize(16, 14))
        self.text_view.setVerticallyResizable_(True)
        self.text_view.setHorizontallyResizable_(False)
        self.text_view.setMinSize_(NSSize(0, 0))
        self.text_view.setMaxSize_(NSSize(1000000, 1000000))
        self.text_view.setAutoresizingMask_(NSViewWidthSizable)
        self.text_view.textContainer().setWidthTracksTextView_(True)
        self.scroll.setDocumentView_(self.text_view)
        self.close_button = button('关闭报告', self.actions, 'close:')
        self.close_button.setKeyEquivalent_('\x1b')
        self.copy_button = button('复制报告', self.actions, 'copy:', 'doc.on.doc')
        self.handle_button = button('处理当前问题', self.actions, 'handle:', 'wrench.and.screwdriver')
        self.handle_button.setToolTip_('返回实时诊断面板，逐项处理当前问题')
        self.layout()
        self.window.setDelegate_(self.actions)
        self.window.center()

    def show(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)
        self.tabs.setSelectedSegment_(0)
        self.layout()
        self.update_body()
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def layout(self):
        width, height = self.root.bounds().size
        for view in list(self.root.subviews()):
            view.removeFromSuperview()
        if self.logo is not None:
            image = NSImageView.alloc().init()
            image.setImage_(self.logo)
            place(self.root, image, 22, 18, 34, 34)
        place(self.root, label(self.title, 18, bold=True), 68, 12, width - 90, 30)
        timestamp = self.snapshot.get('last_check')
        if hasattr(timestamp, 'strftime'):
            timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
        elif timestamp:
            timestamp = str(timestamp).replace('T', ' ')[:19]
        caption = self.caption or ('最近 50 次本地处理' if self.summary_builder else (timestamp or '尚无检测时间'))
        place(self.root, label('NetCare · ' + caption, 11, secondary=True), 69, 42, width - 95, 22)
        place(self.root, self.tabs, 22, 77, 224, 26)
        place(self.root, label('网络标识已脱敏', 11, secondary=True), width - 158, 81, 136, 22)
        place(self.root, self.scroll, 22, 116, width - 44, height - 180)
        self.text_view.setFrameSize_(NSSize(self.scroll.contentSize().width, max(self.text_view.frame().size.height, self.scroll.contentSize().height)))
        separator(self.root, 22, height - 52, width - 44)
        place(self.root, self.copy_button, 22, height - 41, 116, 30)
        if self.on_handle:
            place(self.root, self.handle_button, width - 316, height - 41, 166, 30)
        place(self.root, self.close_button, width - 138, height - 41, 116, 30)

    def update_body(self):
        detailed = self.tabs.selectedSegment() == 1
        headings = []
        if detailed:
            text = json.dumps(self.snapshot, ensure_ascii=False, indent=2, default=str)
        elif self.summary_builder:
            text = self.summary_builder(self.snapshot)
        else:
            rows = diagnostic_rows({'ready': True, 'snapshot': self.snapshot, 'repair_options': {}})
            parts = []
            for row in rows:
                heading = f"{row['number']:02d}  {row['title']} · {row['state']}"
                headings.append(heading)
                lines = [heading, row['summary']]
                if row['tone'] in ('warning', 'error'):
                    lines.append(row['explanation'])
                if row['evidence']:
                    lines.append(row['evidence'])
                parts.append('\n'.join(lines))
            other_errors = self.snapshot.get('check_errors', {})
            if other_errors:
                parts.append('未完成的检查\n' + '\n'.join(str(error) for error in other_errors.values()))
            if self.snapshot.get('message'):
                parts.append(str(self.snapshot['message']))
            text = '\n\n'.join(parts)
        self.current_text = text
        style = NSMutableParagraphStyle.alloc().init()
        style.setLineSpacing_(4)
        font = NSFont.monospacedSystemFontOfSize_weight_(12, 0) if detailed else NSFont.systemFontOfSize_(13)
        attributes = {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.textColor(),
                      NSParagraphStyleAttributeName: style}
        body = NSMutableAttributedString.alloc().initWithString_attributes_(text, attributes)
        for heading in headings:
            index = text.index(heading)
            # AppKit ranges count UTF-16 code units, not Python Unicode scalars.
            start = len(text[:index].encode('utf-16-le')) // 2
            length = len(heading.encode('utf-16-le')) // 2
            body.addAttribute_value_range_(NSFontAttributeName, NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold), (start, length))
        self.text_view.textStorage().setAttributedString_(body)
        self.text_view.sizeToFit()
        self.text_view.scrollRangeToVisible_((0, 0))
        self.copy_button.setTitle_('复制报告')
