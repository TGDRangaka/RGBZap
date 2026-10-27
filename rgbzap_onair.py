"""Detect whether a webcam or microphone is in use, for RGBZap's On-Air light.

Windows keeps a privacy log of which apps use the camera and microphone, under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore.
Each app has LastUsedTimeStart / LastUsedTimeStop (Windows FILETIME); Stop == 0 means
"still using it right now". We only read these two numbers: no camera frames, no audio,
no app names leave this module, and no admin rights are needed.
"""
import sys
import threading

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    import winreg

BASE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
KINDS = {"cam": "webcam", "mic": "microphone"}
FILETIME_EPOCH_DIFF = 116444736000000000          # 100 ns ticks between 1601 and 1970


def in_use(entries, boot_filetime=0):
    """entries: iterable of (start, stop) FILETIME ints. True if any app started using
    the device and hasn't stopped. A 'start' from before this boot is a stale record
    (an app that crashed, or a reboot while the camera was on) and is ignored."""
    return any(start > 0 and stop == 0 and start >= boot_filetime for start, stop in entries)


def boot_filetime():
    """FILETIME of the last boot (now minus uptime), or 0 if unknown."""
    if not IS_WINDOWS:
        return 0
    import time
    kernel32 = ctypes.windll.kernel32
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    uptime_ms = kernel32.GetTickCount64()
    now_ft = int(time.time() * 10_000_000) + FILETIME_EPOCH_DIFF
    return now_ft - uptime_ms * 10_000


def is_own(app_key, exe_path=None):
    """True if a ConsentStore app key is this very program. Desktop apps are keyed by
    their exe path with '\\' written as '#'. RGBZap's own audio capture appears there
    as microphone use, which must not light up its own On-Air light."""
    import os
    exe = os.path.basename(exe_path or sys.executable).lower()
    return app_key.lower().endswith("#" + exe) or app_key.lower() == exe


def _entries(kind):
    """(start, stop) for every app recorded under the device kind, packaged and desktop,
    except RGBZap itself."""
    out = []
    root = f"{BASE}\\{kind}"
    for sub in (root, root + "\\NonPackaged"):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged" or is_own(name):
                    continue
                try:
                    with winreg.OpenKey(key, name) as app:
                        start = int(winreg.QueryValueEx(app, "LastUsedTimeStart")[0])
                        stop = int(winreg.QueryValueEx(app, "LastUsedTimeStop")[0])
                except (OSError, ValueError, TypeError):
                    continue                         # app key without usage values
                out.append((start, stop))
    return out


def device_in_use(which):
    """which = 'cam' | 'mic'. False when it can't be read (non-Windows, no record)."""
    if not IS_WINDOWS:
        return False
    try:
        return in_use(_entries(KINDS[which]), boot_filetime())
    except Exception:
        return False


class CapabilityWatcher:
    """Polls every `period` seconds on its own thread and calls on_state({'cam': bool,
    'mic': bool}) after each poll (so listeners can double as a heartbeat)."""

    def __init__(self, on_state, period=2.0, reader=device_in_use):
        self.on_state, self.period, self.reader = on_state, period, reader
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            self._stop.clear()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.on_state({k: bool(self.reader(k)) for k in KINDS})
            except Exception:
                pass
            self._stop.wait(self.period)


def _selftest():
    """python rgbzap_onair.py --selftest"""
    boot = 1000
    assert in_use([(2000, 0)], boot) is True                  # started, not stopped
    assert in_use([(2000, 3000)], boot) is False              # finished
    assert in_use([(0, 0)], boot) is False                    # never used
    assert in_use([(500, 0)], boot) is False                  # stale: from before boot
    assert in_use([(500, 0), (1500, 0)], boot) is True        # one live app is enough
    assert in_use([], boot) is False
    # our own capture must be ignored (frozen exe and "python elk_gui.py" runs alike)
    assert is_own("C:#Users#USER#AppData#Local#Programs#RGBZap#RGBZap.exe", r"C:\x\RGBZap.exe")
    assert is_own("C:#Users#USER#AppData#Local#Programs#Python#pythonw.exe", r"C:\p\pythonw.exe")
    assert not is_own("C:#Program Files#Discord#Discord.exe", r"C:\x\RGBZap.exe")
    assert not is_own("C:#tools#notRGBZap.exe", r"C:\x\RGBZap.exe")
    assert not is_own("Microsoft.WindowsCamera_8wekyb3d8bbwe", r"C:\x\RGBZap.exe")
    # watcher reports both devices each poll and survives a reader that raises
    seen, calls = [], []

    def reader(k):
        calls.append(k)
        if len(calls) == 3:
            raise RuntimeError("registry hiccup")
        return k == "cam"
    w = CapabilityWatcher(seen.append, period=0.01, reader=reader)
    w.start()
    threading.Event().wait(0.3)
    w.stop()
    assert seen and seen[0] == {"cam": True, "mic": False}, seen[:1]
    assert len(seen) >= 3, "watcher stopped after a reader error"
    print("OK  on-air: stale/live/finished records, watcher heartbeat survives errors")
    if IS_WINDOWS:
        # the real registry: just prove it reads without error and the boot time is sane
        import time
        bt = boot_filetime()
        assert 0 < bt < int(time.time() * 10_000_000) + FILETIME_EPOCH_DIFF, bt
        assert isinstance(device_in_use("cam"), bool) and isinstance(device_in_use("mic"), bool)
        print("OK  on-air: real registry readable (cam=%s mic=%s right now)"
              % (device_in_use("cam"), device_in_use("mic")))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
