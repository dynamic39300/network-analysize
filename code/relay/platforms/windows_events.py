"""IP Helper notifications only schedule work; callbacks never inspect or mutate the OS."""
import ctypes
from ctypes import wintypes
import threading

_LIVE_SUBSCRIPTIONS = set()


class IPHelperSubscriptions:
    def __init__(self):
        self.api = ctypes.WinDLL('iphlpapi', use_last_error=True, winmode=0x800)
        self.callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
        self.callbacks = {}

    def register(self, name, changed):
        callback = self.callback_type(lambda _context, _row, _kind: changed())
        method = getattr(self.api, name)
        method.argtypes = [ctypes.c_ushort, self.callback_type, ctypes.c_void_p, ctypes.c_ubyte,
                           ctypes.POINTER(wintypes.HANDLE)]
        method.restype = wintypes.ULONG
        handle = wintypes.HANDLE()
        code = method(0, callback, None, False, ctypes.byref(handle))
        if code:
            raise OSError(code, 'IP Helper subscription failed')
        self.callbacks[handle.value] = callback
        _LIVE_SUBSCRIPTIONS.add(self)
        return handle.value

    def cancel(self, handle):
        method = self.api.CancelMibChangeNotify2
        method.argtypes, method.restype = [wintypes.HANDLE], wintypes.ULONG
        code = method(handle)
        if code:
            raise OSError(code, 'IP Helper cancellation failed')
        self.callbacks.pop(handle, None)
        if not self.callbacks:
            _LIVE_SUBSCRIPTIONS.discard(self)


class WindowsNetworkEvents:
    def __init__(self, changed, backend_factory=IPHelperSubscriptions):
        self.changed, self.backend_factory = changed, backend_factory
        self.backend = None
        self.handles = []
        self.stopped = threading.Event()
        self.available = False
        self.error = ''

    def _changed(self):
        if not self.stopped.is_set():
            try:
                self.changed()
            except Exception:
                pass

    def start(self):
        if self.available:
            return True
        if self.stopped.is_set():
            return False
        try:
            self.backend = self.backend_factory()
            for name in ('NotifyIpInterfaceChange', 'NotifyRouteChange2', 'NotifyUnicastIpAddressChange'):
                self.handles.append(self.backend.register(name, self._changed))
            self.available = True
        except Exception:
            self.error = 'Windows 网络事件订阅不可用，仅保留定时检查'
            self.close()
        return self.available

    def close(self):
        self.stopped.set()
        self.available = False
        # Called by the runtime thread, never by an IP Helper callback or while
        # holding a lock needed by it. CancelMibChangeNotify2 waits for callbacks.
        remaining = []
        for handle in self.handles:
            try:
                self.backend.cancel(handle)
            except Exception:
                remaining.append(handle)
                self.error = 'Windows 事件注销未完成，已屏蔽新回调'
        self.handles = remaining
