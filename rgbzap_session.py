"""Know when Windows is about to shut down, restart or sign the user out, so RGBZap can
switch the lights off first.

Windows sends WM_QUERYENDSESSION to every top-level window *before* it starts closing
programs, so Bluetooth is still up at that moment (the ideal time to send "off"). It
then sends WM_ENDSESSION to say whether the shutdown really went ahead or was cancelled.
Only a real top-level window gets these (message-only windows don't), so this keeps one
invisible, never-shown window on its own thread.
"""
import sys
import threading

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

WM_CLOSE, WM_DESTROY = 0x0010, 0x0002
WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016
CLASS_NAME = "RGBZapSessionWatcher"


class SessionWatcher:
    """on_query(): the session is about to end (shutdown, restart or sign-out). Runs on the
    watcher thread and may take a moment (a couple of seconds at most) to switch the
    lights off; Windows waits for it. on_end(ended): the final word: True if it went
    ahead, False if it was cancelled (so lights can come back)."""

    def __init__(self, on_query, on_end):
        self.on_query, self.on_end = on_query, on_end
        self._thread = None
        self._hwnd = None
        self._proc = None                       # keep the callback alive (ctypes)

    def start(self):
        if not IS_WINDOWS or self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        ready.wait(3.0)

    def stop(self):
        if self._hwnd and IS_WINDOWS:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            self._thread.join(2.0)
        self._thread = self._hwnd = None

    def _run(self, ready):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM]
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_QUERYENDSESSION:
                try:
                    self.on_query()
                except Exception:               # never block the shutdown over our problem
                    pass
                return 1                        # TRUE: RGBZap doesn't object
            if msg == WM_ENDSESSION:
                try:
                    self.on_end(bool(wparam))
                except Exception:
                    pass
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, CLASS_NAME
        user32.RegisterClassW(ctypes.byref(wc))
        # a plain overlapped window that is never shown: invisible, but a real top-level
        self._hwnd = user32.CreateWindowExW(0, CLASS_NAME, "RGBZap session watcher", 0,
                                            0, 0, 0, 0, None, None, hinst, None)
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnregisterClassW(CLASS_NAME, hinst)


def _selftest():
    """python rgbzap_session.py --selftest (sends the real messages to our own hidden
    window; it never touches Windows' actual shutdown)"""
    if not IS_WINDOWS:
        print("SKIP session: Windows only")
        return
    events, slow = [], {"on": False}

    def on_query():
        events.append("query")
        if slow["on"]:
            import time
            time.sleep(0.3)

    w = SessionWatcher(on_query, lambda ended: events.append(("end", ended)))
    w.start()
    assert w._hwnd, "hidden window not created"
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    assert not user32.IsWindowVisible(w._hwnd), "the watcher window must never be visible"
    # shutdown asked: we answer TRUE ("fine by us") only after on_query has finished
    slow["on"] = True
    assert user32.SendMessageW(w._hwnd, WM_QUERYENDSESSION, 0, 0x80000000) == 1
    assert events == ["query"], events
    # then the verdict: it went ahead / it was cancelled
    user32.SendMessageW(w._hwnd, WM_ENDSESSION, 1, 0x80000000)
    user32.SendMessageW(w._hwnd, WM_ENDSESSION, 0, 0)
    assert events == ["query", ("end", True), ("end", False)], events
    # a handler that blows up must not stop the shutdown (still answers TRUE) or the loop
    w.on_query = lambda: 1 / 0
    assert user32.SendMessageW(w._hwnd, WM_QUERYENDSESSION, 0, 0) == 1
    w.stop()
    assert w._hwnd is None and w._thread is None
    w2 = SessionWatcher(lambda: None, lambda e: None)    # can start again after stopping
    w2.start()
    assert w2._hwnd
    w2.stop()
    print("OK  session: hidden window answers shutdown queries, reports go-ahead/cancel, "
          "survives a failing handler, starts/stops cleanly")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
