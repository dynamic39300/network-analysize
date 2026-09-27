"""Native protected-target overview with real stages and expandable technical checks."""
from AppKit import NSImage, NSImageView, NSScrollView, NSSize, NSSwitch
from Foundation import NSMakeRect, NSObject

from .overview import overview
from .panel import COLORS, STATUS_SYMBOLS, FlippedView, button, label, place, separator, text_height
from .profiles_window import icon_button

TONES = {'healthy': 'ok', 'degraded': 'error', 'auth_required': 'warning',
         'unknown': 'warning', 'stale': 'warning', 'not_applicable': 'neutral'}


class OverviewActions(NSObject):
    def check_(self, sender):
        self.owner.controller.check()

    def investigate_(self, sender):
        self.owner.controller.investigate()

    def guard_(self, sender):
        self.owner.controller.set_guard_enabled(bool(sender.state()))

    def stop_(self, sender):
        self.owner.controller.cancel()

    def review_(self, sender):
        self.owner.controller.prepare_fix()

    def task_(self, sender):
        self.owner.navigate('records', self.owner.state.get('run_id'))

    def profiles_(self, sender):
        self.owner.navigate('profiles')

    def expand_(self, sender):
        key = self.owner.expansion_keys[sender.tag()]
        if key in self.owner.expanded:
            self.owner.expanded.remove(key)
        else:
            self.owner.expanded.add(key)
        self.owner.layout()


class OverviewPage:
    def __init__(self, controller, navigate):
        self.controller, self.navigate = controller, navigate
        self.state, self.expanded, self.expansion_keys = {}, set(), []
        self.root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 600))
        self.document = FlippedView.alloc().init()
        self.scroll = NSScrollView.alloc().init()
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setDocumentView_(self.document)
        self.actions = OverviewActions.alloc().init()
        self.actions.owner = self
        self.check = icon_button('检测保护目标', 'arrow.clockwise', self.actions, 'check:')
        self.investigate = button('调查问题', self.actions, 'investigate:', 'magnifyingglass')
        self.stop = icon_button('停止任务并暂停守护', 'stop', self.actions, 'stop:')
        self.review = button('查看待确认方案', self.actions, 'review:', 'checkmark.shield')
        self.task = button('查看处理记录', self.actions, 'task:', 'list.bullet.rectangle')
        self.profile_button = icon_button('网络档案', 'slider.horizontal.3', self.actions, 'profiles:')
        self.guard = NSSwitch.alloc().init()
        self.guard.setTarget_(self.actions)
        self.guard.setAction_('guard:')
        self.guard.setAccessibilityLabel_('网络守护')
        self.guard.setToolTip_('暂停不强杀正在收尾的修改；启用不授予新的写权限')

    def render(self, state):
        if self.state == state:
            return
        self.state = state
        self.layout()

    def icon(self, tone, x, y):
        view = NSImageView.alloc().init()
        view.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(STATUS_SYMBOLS[tone], tone))
        view.setContentTintColor_(COLORS[tone]())
        place(self.document, view, x, y, 18, 18)

    def line(self, text, x, y, width, *, size=12, bold=False, secondary=False):
        field = label(text, size, bold=bold, secondary=secondary)
        h = text_height(field, width)
        place(self.document, field, x, y, width, h)
        return y + h

    def disclosure(self, key, title, x, y, width):
        self.expansion_keys.append(key)
        control = button(title, self.actions, 'expand:', 'chevron.down' if key in self.expanded else 'chevron.right')
        control.setTag_(len(self.expansion_keys) - 1)
        control.setBordered_(False)
        control.setAlignment_(0)
        control.setToolTip_(title)
        place(self.document, control, x, y, width, 28)
        return control

    def layout(self):
        width, height = self.root.bounds().size
        data = overview(self.state)
        offset = self.scroll.contentView().bounds().origin.y
        for parent in (self.root, self.document):
            for child in list(parent.subviews()):
                if parent is self.document or child not in (self.guard, self.check, self.investigate, self.stop, self.scroll):
                    child.removeFromSuperview()
        self.expansion_keys = []
        place(self.root, label('总览', 20, bold=True), 22, 14, 100, 30)
        place(self.root, label('守护', 12), width - 352, 21, 40, 24)
        place(self.root, self.guard, width - 312, 18, 44, 28)
        place(self.root, self.check, width - 252, 17, 34, 30)
        place(self.root, self.investigate, width - 206, 16, 132, 32)
        place(self.root, self.stop, width - 56, 17, 34, 30)
        separator(self.root, 22, 58, width - 44)
        place(self.root, self.scroll, 0, 65, width, max(100, height - 65))
        w = self.scroll.contentSize().width
        y = self.line(data['title'], 22, 8, w - 44, size=17, bold=True) + 3
        y = self.line(data['environment'], 22, y, w - 44, secondary=True) + 6
        phase = self.state.get('busy') or ('守护检查中' if self.state.get('guard', {}).get('phase') == 'checking'
                                          else '守护已启用' if self.state.get('guard', {}).get('enabled') else '守护未启用 / 已暂停')
        y = self.line(phase + ' · ' + data['task'], 22, y, w - 44)
        if data['reason']:
            y = self.line(data['reason'], 22, y, w - 44)
        if data['permission_reason']:
            y = self.line(data['permission_reason'], 22, y, w - 44)
        if self.state.get('run_id'):
            place(self.document, self.task, 22, y + 5, 160, 32)
            if self.state.get('run_stage') == 'awaiting_authorization':
                place(self.document, self.review, 194, y + 5, 190, 32)
            y += 47
        separator(self.document, 22, y + 8, w - 44)
        y += 23
        y = self.line('保护目标 · ' + data['profile'], 22, y, w - 100, size=15, bold=True)
        place(self.document, self.profile_button, w - 56, y - 26, 34, 30)
        y = self.line(data['coverage'] + ' · ' + data['captured'], 22, y + 2, w - 44, secondary=True) + 8
        if not data['targets']:
            y = self.line('尚无保护目标', 22, y, w - 44)
        for row in data['targets']:
            self.icon(TONES.get(row['state'], 'neutral'), 22, y + 8)
            name = label(row['name'], 13, bold=True)
            name_h = text_height(name, w - 235)
            place(self.document, name, 50, y + 4, w - 235, name_h)
            place(self.document, label(row['label'], 12), w - 168, y + 4, 146, 25)
            y += max(30, name_h + 4)
            y = self.line(row['path_label'] + ' · ' + row['summary'], 50, y, w - 72, secondary=True) + 6
            separator(self.document, 50, y, w - 72)
            y += 8
        y = self.line('检测详情', 22, y + 16, w - 44, size=15, bold=True) + 6
        for row in data['checks']:
            self.icon(row['tone'], 22, y + 6)
            self.disclosure(row['id'], row['title'] + ' · ' + row['state'], 47, y, w - 70)
            y = self.line(row['summary'], 50, y + 30, w - 72, secondary=True) + 6
            if row['id'] in self.expanded:
                y = self.line(row['explanation'], 50, y, w - 72) + 4
                if row['evidence']:
                    y = self.line(row['evidence'], 50, y, w - 72, secondary=True) + 4
            separator(self.document, 50, y, w - 72)
            y += 8
        self.document.setFrameSize_(NSSize(w, max(y + 20, self.scroll.contentSize().height)))
        self.scroll.contentView().scrollToPoint_((0, min(offset, max(0, y + 20 - self.scroll.contentSize().height))))
        self.scroll.reflectScrolledClipView_(self.scroll.contentView())
        ready = bool(self.state.get('ready'))
        idle = ready and not self.state.get('busy') and self.state.get('guard', {}).get('phase') != 'checking'
        pending = self.state.get('run_stage') in ('awaiting_authorization', 'authorized')
        self.check.setEnabled_(idle and not pending)
        self.investigate.setEnabled_(idle and not pending)
        self.review.setEnabled_(idle and pending)
        self.stop.setEnabled_(ready)
        self.task.setEnabled_(bool(self.state.get('run_id')))
        self.guard.setState_(int(bool(self.state.get('guard', {}).get('enabled'))))
        self.guard.setEnabled_(ready)
