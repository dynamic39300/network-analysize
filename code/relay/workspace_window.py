"""One native five-view workspace; navigation never starts a network task."""
from AppKit import (NSApplication, NSBackingStoreBuffered, NSColor, NSImage, NSImageView,
    NSSize, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSObject

from .panel import FlippedView, button, label, place, separator, text_height
from .profiles_window import icon_button
from .report_window import ReportNativeWindow

PAGES = [('overview', '总览', 'network'), ('records', '处理记录', 'list.bullet.rectangle'),
         ('profiles', '网络档案', 'folder'), ('privacy', '权限与隐私', 'hand.raised'), ('settings', '设置', 'gearshape')]


class WorkspaceActions(NSObject):
    def navigate_(self, sender):
        self.owner.show(PAGES[sender.tag()][0])

    def dismissNotice_(self, sender):
        self.owner.message = ''
        self.owner.layout()

    def windowDidResize_(self, notification):
        self.owner.layout()

    def windowShouldClose_(self, window):
        self.owner.pages['settings'].dismiss()
        self.owner.pages['privacy'].dismiss()
        return True


class WorkspaceWindow:
    def __init__(self, controller, pages, logo_path):
        self.controller, self.pages = controller, pages
        self.current, self.message, self.focus_run = 'overview', '', None
        self.loaded, self.loading, self.focus = {'overview'}, set(), {}
        self.state = controller.ui_state()
        self.actions = WorkspaceActions.alloc().init()
        self.actions.owner = self
        self.window = ReportNativeWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 1060, 700), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
            NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable, NSBackingStoreBuffered, False)
        self.window.setTitle_('NetCare · 网络保障')
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSSize(760, 560))
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 1060, 700))
        self.window.setContentView_(self.root)
        self.content = FlippedView.alloc().init()
        self.placeholder = label('', 13, secondary=True)
        self.logo = NSImage.alloc().initWithContentsOfFile_(str(logo_path))
        self.nav = []
        for index, (_, title, symbol) in enumerate(PAGES):
            control = button(title, self.actions, 'navigate:', symbol)
            control.setTag_(index)
            control.setAlignment_(0)
            control.setAccessibilityLabel_(title)
            self.nav.append(control)
        self.dismiss_notice = icon_button('关闭提示', 'xmark', self.actions, 'dismissNotice:')
        for page in pages.values():
            old_window = getattr(page, 'window', None)
            if old_window:
                old_window.setDelegate_(None)
                page.root.removeFromSuperview()
                old_window.orderOut_(None)
            page.window = self.window
        self.window.setDelegate_(self.actions)
        self.window.center()
        self.layout()

    def show(self, name='overview', run_id=None):
        if name not in self.pages:
            raise ValueError('Unknown workspace page')
        if name != self.current:
            responder = self.window.firstResponder()
            # Preserve the field, not AppKit's shared field-editor text view.
            if hasattr(responder, 'isFieldEditor') and responder.isFieldEditor():
                responder = responder.delegate()
            if hasattr(responder, 'isDescendantOf_') and responder.isDescendantOf_(self.pages[self.current].root):
                self.focus[self.current] = responder
            self.window.makeFirstResponder_(None)
        self.current = name
        if run_id:
            self.focus_run = run_id
            self.loaded.discard('records')
        self.layout()
        self.window.makeKeyAndOrderFront_(None)
        responder = self.focus.get(name)
        if responder and responder.isDescendantOf_(self.pages[name].root):
            self.window.makeFirstResponder_(responder)
        self.request_current()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def request_current(self):
        name = self.current
        if (name in self.loaded or name in self.loading or not self.state.get('ready')
                or name == 'profiles' and (self.state.get('busy') or self.state.get('guard', {}).get('phase') == 'checking')):
            return
        if name == 'records':
            accepted = self.controller.show_agent_records()
        elif name == 'profiles':
            accepted = self.controller.show_profiles()
        else:
            accepted = self.controller.show_settings(name)
        if accepted:
            self.loading.add(name)

    def receive(self, name, payload):
        page = self.pages[name]
        if name in ('settings', 'privacy'):
            page.load(payload)
        else:
            page.load_payload(payload)
        self.loaded.add(name)
        self.loading.discard(name)
        if name == 'records' and self.focus_run:
            page.focus_run(self.focus_run)
            self.focus_run = None
        page.update_state(self.state)
        if self.current == name:
            self.layout()

    def notice(self, title, message):
        self.loading.clear()
        self.message = title + '：' + message
        self.layout()

    def update_state(self, state):
        was_ready = self.state.get('ready')
        old_instance = self.state.get('core_instance')
        self.state = state
        if old_instance != state.get('core_instance') or was_ready and not state.get('ready'):
            self.loaded = {'overview'}
            self.loading.clear()
        self.pages['overview'].render(state)
        for name, page in self.pages.items():
            if name != 'overview':
                page.update_state(state)
        if self.window.isVisible():
            self.request_current()
        if self.current not in self.loaded:
            self.layout()

    def layout(self):
        width, height = self.root.bounds().size
        for child in list(self.root.subviews()):
            if child is not self.content:
                child.removeFromSuperview()
        sidebar = 144
        if self.logo:
            image = NSImageView.alloc().init()
            image.setImage_(self.logo)
            place(self.root, image, 17, 19, 32, 32)
        place(self.root, label('NetCare', 18, bold=True), 56, 19, 82, 32)
        for index, control in enumerate(self.nav):
            y = 86 + index * 44 if index < 3 else height - 115 + (index - 3) * 44
            control.setBordered_(self.current == PAGES[index][0])
            control.setContentTintColor_(NSColor.controlAccentColor() if self.current == PAGES[index][0] else NSColor.labelColor())
            control.setAccessibilityValue_('已选中' if self.current == PAGES[index][0] else '')
            place(self.root, control, 10, y, 126, 34)
        place(self.root, label('网络保障', 11, secondary=True), 20, height - 31, 118, 22)
        footer = 0
        if self.message:
            field = label(self.message, 11)
            field.setToolTip_(self.message)
            footer = min(92, text_height(field, width - sidebar - 72) + 16)
            field.setMaximumNumberOfLines_(4)
            place(self.root, field, sidebar + 20, height - footer + 6, width - sidebar - 72, footer - 12)
            place(self.root, self.dismiss_notice, width - 40, height - footer + 6, 28, 28)
            separator(self.root, sidebar + 16, height - footer, width - sidebar - 32)
        place(self.root, self.content, sidebar, 0, width - sidebar, height - footer)
        for name, page in self.pages.items():
            if page.root.superview() is None:
                self.content.addSubview_(page.root)
            available = name in self.loaded or getattr(page, 'offline_available', False)
            page.root.setHidden_(name != self.current or not available)
            page.root.setFrame_(NSMakeRect(0, 0, width - sidebar, height - footer))
            if name == self.current:
                page.layout()
        self.placeholder.setHidden_(self.current in self.loaded or getattr(self.pages[self.current], 'offline_available', False))
        self.placeholder.setStringValue_('核心未连接；未读取此页面' if not self.state.get('ready') else
                                        '当前任务正在处理，稍后读取此页面' if self.state.get('busy') else '正在读取本地记录')
        place(self.content, self.placeholder, 22, 70, width - sidebar - 44, 56)
