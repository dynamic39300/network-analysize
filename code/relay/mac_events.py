"""Read-only macOS network notifications on a dedicated run loop."""
import threading


class MacNetworkEvents:
    PATTERNS = [r'State:/Network/Global/.*', r'State:/Network/Service/.*/.*',
                r'State:/Network/Interface/.*/.*', r'Setup:/Network/Service/.*/.*']

    def __init__(self, changed):
        self.changed = changed
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.available = False
        self.error = ''
        self.thread = None

    def start(self):
        if self.thread is not None:
            return self.available
        self.thread = threading.Thread(target=self._run, name='NetCare network events', daemon=True)
        self.thread.start()
        self.ready.wait(2)
        return self.available

    def _run(self):
        try:
            import objc
            from CoreFoundation import (CFRunLoopAddSource, CFRunLoopGetCurrent,
                                        CFRunLoopRemoveSource, CFRunLoopRunInMode, kCFRunLoopDefaultMode)
            from SystemConfiguration import (SCDynamicStoreCreate, SCDynamicStoreCreateRunLoopSource,
                                             SCDynamicStoreSetNotificationKeys)
            with objc.autorelease_pool():
                def callback(store, keys, context):
                    if not self.stopped.is_set():
                        self.changed()
                store = SCDynamicStoreCreate(None, 'NetCare Guard', callback, None)
                if store is None or not SCDynamicStoreSetNotificationKeys(store, None, self.PATTERNS):
                    raise RuntimeError('network change subscription unavailable')
                source = SCDynamicStoreCreateRunLoopSource(None, store, 0)
                if source is None:
                    raise RuntimeError('network event source unavailable')
                loop = CFRunLoopGetCurrent()
                CFRunLoopAddSource(loop, source, kCFRunLoopDefaultMode)
                self.available = True
                self.ready.set()
                try:
                    while not self.stopped.is_set():
                        with objc.autorelease_pool():
                            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.5, False)
                finally:
                    CFRunLoopRemoveSource(loop, source, kCFRunLoopDefaultMode)
        except Exception as exc:
            self.error = type(exc).__name__
        finally:
            self.available = False
            self.ready.set()

    def close(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(2)
