"""Global hotkeys for RGBZap via the Win32 RegisterHotKey API (no extra packages).

RegisterHotKey asks Windows to tell us when a key combination is pressed anywhere,
without hooking the keyboard, so games and other apps are not slowed down and nothing
is recorded: Windows only wakes us for the exact combos we registered.

    parse_combo("ctrl+alt+pageup")  -> (mods, vk) or None
    normalize("Alt+Ctrl+PageUp")    -> "ctrl+alt+pageup"
    HotkeyManager(on_fire).start({"toggle_power": "ctrl+alt+home"}) -> {action: error}
"""
import sys
import threading

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN}
_MOD_ORDER = (("ctrl", MOD_CONTROL), ("alt", MOD_ALT), ("shift", MOD_SHIFT), ("win", MOD_WIN))
_NAMED = {"up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "space": 0x20,
          "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "insert": 0x2D,
          "delete": 0x2E, "tab": 0x09, "enter": 0x0D, "backspace": 0x08}
_ALIASES = {"pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown", "ins": "insert",
            "del": "delete", "return": "enter", "prior": "pageup", "next": "pagedown",
            "esc": None}                       # Esc stays free (it cancels dialogs)


def key_code(name):
    """Virtual-key code for a key name ('a', '7', 'f10', 'numpad3', 'pageup'), or None."""
    name = _ALIASES.get(name, name)
    if not name:
        return None
    if len(name) == 1 and name.isalnum() and name.isascii():
        return ord(name.upper())
    if name in _NAMED:
        return _NAMED[name]
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    if name.startswith("numpad") and name[6:].isdigit() and len(name) == 7:
        return 0x60 + int(name[6:])
    return None


def parse_combo(text):
    """'ctrl+alt+pageup' -> (mods, vk). None if it isn't a usable global hotkey: one
    plain key plus modifiers, with at least Ctrl, Alt or Win (Shift alone would steal
    ordinary typing)."""
    if not isinstance(text, str) or not text.strip():
        return None
    mods, key = 0, None
    for part in (p.strip().lower() for p in text.split("+")):
        if part in _MODS:
            mods |= _MODS[part]
        elif key is None and part:
            key = part
        else:
            return None                        # two main keys, or an empty slot
    vk = key_code(key) if key else None
    if vk is None or not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        return None
    return mods, vk


def normalize(text):
    """Canonical text for a combo ('Alt+Ctrl+PageUp' -> 'ctrl+alt+pageup'), or None."""
    parsed = parse_combo(text)
    if parsed is None:
        return None
    mods, vk = parsed
    key = next(k for k in [*(_NAMED), *(f"f{i}" for i in range(1, 25)),
                           *(f"numpad{i}" for i in range(10)),
                           *"abcdefghijklmnopqrstuvwxyz0123456789"] if key_code(k) == vk)
    return "+".join([n for n, m in _MOD_ORDER if mods & m] + [key])


def pretty(text):
    """'ctrl+alt+pageup' -> 'Ctrl+Alt+PgUp' for display; '' for no hotkey."""
    names = {"pageup": "PgUp", "pagedown": "PgDn", "insert": "Ins", "delete": "Del"}
    n = normalize(text)
    return "+".join(names.get(p, p.capitalize() if len(p) > 1 else p.upper())
                    for p in n.split("+")) if n else ""


def find_conflicts(bindings):
    """{action: combo} -> sorted list of (action, other_action) pairs sharing a combo."""
    seen, out = {}, []
    for action, combo in sorted(bindings.items()):
        n = normalize(combo)
        if not n:
            continue
        if n in seen:
            out.append((seen[n], action))
        else:
            seen[n] = action
    return out


class HotkeyManager:
    """Owns one thread with a Win32 message loop. start() registers every combo and
    returns {action: reason} for the ones Windows refused (usually 'already used by
    another program'); fire(action) is called on the thread for each press, so keep
    it quick or hand off to the UI thread."""

    def __init__(self, on_fire):
        self.on_fire = on_fire
        self._thread = None
        self._tid = None

    def start(self, bindings):
        self.stop()
        if not IS_WINDOWS:
            return {a: "global hotkeys need Windows" for a in bindings}
        failed, ready = {}, threading.Event()
        todo = [(a, parse_combo(c)) for a, c in bindings.items() if c]
        self._thread = threading.Thread(target=self._loop, args=(todo, failed, ready),
                                        daemon=True)
        self._thread.start()
        ready.wait(3.0)
        return failed

    def stop(self):
        if self._thread is not None and self._tid is not None and IS_WINDOWS:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2.0)
        self._thread = self._tid = None

    def _loop(self, todo, failed, ready):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT,
                                          wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                              wintypes.WPARAM, wintypes.LPARAM]
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)    # create this thread's queue
        self._tid = kernel32.GetCurrentThreadId()
        ids = {}
        for i, (action, parsed) in enumerate(todo, start=1):
            if parsed is None:
                failed[action] = "not a valid shortcut"
                continue
            mods, vk = parsed
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                ids[i] = action
            else:
                failed[action] = ("already used by another program"
                                  if ctypes.get_last_error() == ERROR_HOTKEY_ALREADY_REGISTERED
                                  or kernel32.GetLastError() == ERROR_HOTKEY_ALREADY_REGISTERED
                                  else "Windows refused this shortcut")
        ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in ids:
                    try:
                        self.on_fire(ids[msg.wParam])
                    except Exception:            # a bad handler must not kill the loop
                        pass
        finally:
            for i in ids:
                user32.UnregisterHotKey(None, i)


def _selftest():
    """python rgbzap_hotkeys.py --selftest"""
    assert parse_combo("ctrl+alt+pageup") == (MOD_CONTROL | MOD_ALT, 0x21)
    assert parse_combo("Alt + Ctrl + F10") == (MOD_CONTROL | MOD_ALT, 0x79)
    assert parse_combo("win+shift+7") == (MOD_WIN | MOD_SHIFT, ord("7"))
    assert parse_combo("ctrl+numpad3") == (MOD_CONTROL, 0x63)
    for bad in ("", None, "a", "shift+a", "ctrl", "ctrl+alt", "ctrl+a+b", "ctrl+esc",
                "ctrl+f25", "ctrl+??", "ctrl++", 5):
        assert parse_combo(bad) is None, f"accepted {bad!r}"
    assert normalize("Alt+Ctrl+PgUp") == "ctrl+alt+pageup"
    assert normalize("win+shift+alt+ctrl+f1") == "ctrl+alt+shift+win+f1"
    assert pretty("ctrl+alt+pageup") == "Ctrl+Alt+PgUp" and pretty("ctrl+f10") == "Ctrl+F10"
    assert pretty("nope") == ""
    assert find_conflicts({"a": "ctrl+alt+x", "b": "Alt+Ctrl+X", "c": "ctrl+alt+y",
                           "d": ""}) == [("a", "b")]
    print("OK  hotkeys: parse, normalize, reject unsafe combos, spot duplicates")
    if not IS_WINDOWS:
        return

    # real Win32 round trip: register, simulate the press, see it dispatched; a second
    # registration of the same combo is refused; stopping releases it
    got, done = [], threading.Event()
    mgr = HotkeyManager(lambda a: (got.append(a), done.set()))
    other = HotkeyManager(lambda a: None)
    combo = "ctrl+alt+shift+win+f24"                                # nobody uses this
    assert mgr.start({"probe": combo, "dud": "banana"}) == {"dud": "not a valid shortcut"}
    clash = other.start({"probe2": combo})
    assert list(clash) == ["probe2"] and "already" in clash["probe2"], clash
    other.stop()
    ctypes.windll.user32.PostThreadMessageW(mgr._tid, WM_HOTKEY, 1, 0)   # simulated press
    assert done.wait(2.0) and got == ["probe"], got
    mgr.stop()
    again = HotkeyManager(lambda a: None)
    assert again.start({"x": combo}) == {}, "combo not released after stop()"
    again.stop()
    print("OK  hotkeys: real RegisterHotKey round trip, clash reported, released on stop")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
