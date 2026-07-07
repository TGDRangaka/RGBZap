#!/usr/bin/env python3
"""
ELK-BLEDOM LED strip controller — desktop GUI (Windows/Mac/Linux).

Features:
  * Color picker + preset swatches (instant apply)
  * On / Off buttons
  * Brightness slider (RGB-scales the current color — works on all ELK-BLEDOM)
  * Built-in effects (jump / crossfade / blink) with adjustable speed
  * Persistent "keep-alive" BLE connection so changes are instant (no reconnect lag)
  * Single-instance guard — refuses to open twice (avoids fighting over the strip)
  * Live connection indicator (green = connected, red = not)
  * Auto-retry scan — retries once if the first scan finds nothing
  * Screen Sync (Ambilight): match the strip to a chosen monitor's color in
    real time — great for movies. Near-black scenes fade to a dim glow instead
    of blacking out.

Protocol bytes come from the dave-code-ruiz/elkbledom HA integration.
Requires: bleak, mss, numpy  (pip install bleak mss numpy). Tkinter ships with Python.

Run:  python elk_gui.py
"""
import asyncio
import colorsys
import os
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

import numpy as np
from bleak import BleakScanner, BleakClient

try:
    import winreg  # Windows only — used for the "run at startup" option
except ImportError:
    winreg = None

WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
NAME_PREFIXES = ("elk-bledom", "elk-bledob", "elk-bleddm", "melk", "ledble", "elk-ble")
ADDR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elk_address.txt")
LOCK_PORT = 49731  # arbitrary localhost port used as a single-instance lock
# Pace BLE writes to the strip's measured sustainable rate (~13/s ceiling).
# 80ms => ~12.5/s, safely under the ceiling so the send buffer never backs up.
SEND_INTERVAL = 0.08

# ---- protocol frames (classic ELK-BLEDOM 9-byte 0x7e..0xef) ----
def cmd_on():          return bytes([0x7e, 0x00, 0x04, 0xf0, 0x00, 0x01, 0xff, 0x00, 0xef])
def cmd_off():         return bytes([0x7e, 0x00, 0x04, 0x00, 0x00, 0x00, 0xff, 0x00, 0xef])
def cmd_color(r, g, b):return bytes([0x7e, 0x00, 0x05, 0x03, r & 0xff, g & 0xff, b & 0xff, 0x00, 0xef])
def cmd_effect(v):     return bytes([0x7e, 0x00, 0x03, v & 0xff, 0x03, 0x00, 0x00, 0x00, 0xef])
def cmd_effect_speed(v):return bytes([0x7e, 0x00, 0x02, v & 0xff, 0x00, 0x00, 0x00, 0x00, 0xef])

# effect name -> value (from definitions.json, EFFECTS class)
EFFECTS = {
    "Jump RGB": 135,
    "Jump 7-color": 136,
    "Crossfade Red": 139,
    "Crossfade Green": 140,
    "Crossfade Blue": 141,
    "Crossfade Yellow": 142,
    "Crossfade Cyan": 143,
    "Crossfade Magenta": 144,
    "Crossfade White": 145,
    "Crossfade Red-Green": 146,
    "Crossfade Red-Blue": 147,
    "Crossfade Green-Blue": 148,
    "Crossfade RGB": 137,
    "Crossfade 7-color": 138,
    "Blink Red": 150,
    "Blink Green": 151,
    "Blink Blue": 152,
    "Blink Yellow": 153,
    "Blink Cyan": 154,
    "Blink Magenta": 155,
    "Blink White": 156,
    "Blink 7-color": 149,
}

PRESETS = [
    ("Red", (255, 0, 0)), ("Green", (0, 255, 0)), ("Blue", (0, 0, 255)),
    ("Warm", (255, 138, 18)), ("Cool", (180, 220, 255)), ("White", (255, 255, 255)),
    ("Purple", (150, 0, 255)), ("Orange", (255, 90, 0)), ("Pink", (255, 40, 120)),
]

# Screen-sync color styles: (saturation_factor, gamma, center_weighted)
#   sat > 1 = more vivid; gamma < 1 = brighter mids; center = sample screen middle
SYNC_STYLES = {
    "Balanced": (1.35, 1.00, False),  # accurate but lively — good default for movies
    "Vivid":    (1.80, 0.85, True),   # punchy, center-weighted, dramatic
    "Accurate": (1.10, 1.00, False),  # faithful to the screen, subtle
}
SYNC_BLACK_LUMA = 16      # below this luminance a scene counts as "dark"
SYNC_GLOW_SCALE = 0.10    # dark scenes fade to 10% of the last bright color (dim glow)


# --- "run at Windows startup" via the HKCU Run registry key ---
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "ElkBledomController"


def _startup_command():
    """Command that launches this GUI with NO console window (pythonw.exe)."""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def startup_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _APP_NAME)
            return bool(val)
    except OSError:
        return False


def set_startup(enable):
    """Add or remove this app from Windows startup. Returns True on success."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, _APP_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(k, _APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def acquire_single_instance_lock():
    """Bind a localhost socket as a lock. Returns the socket on success, None if
    another instance already holds it. The OS frees the socket when we exit."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR is intentionally NOT set so a second bind fails.
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


class BleWorker:
    """Runs an asyncio loop in a background thread and holds one persistent client.

    status_cb(text)        -> called with human-readable status strings
    conn_cb(is_connected)  -> called with True/False on connection state changes
    """
    def __init__(self, status_cb, conn_cb):
        self.status_cb = status_cb
        self.conn_cb = conn_cb
        self.loop = asyncio.new_event_loop()
        self.client: BleakClient | None = None
        self.address: str | None = None
        # coalescing writer state (all touched only inside the loop thread)
        self._pending: dict[str, bytes] = {}
        self._evt: asyncio.Event | None = None
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._start_writer)
        self.loop.run_forever()

    def _start_writer(self):
        self._evt = asyncio.Event()
        self.loop.create_task(self._writer())

    async def _writer(self):
        """Single writer: coalesces rapid updates so only the latest value per
        channel is sent, and paces sends to the strip's real throughput.

        Measured on ELK-BLEDOM: write-without-response returns instantly (it does
        NOT wait for transmission), so sending faster than the link drains just
        piles packets into the OS/controller buffer — latency then grows without
        bound (the classic "screen sync lags more over time"). The strip's real
        sustainable rate is ~13 writes/sec; SEND_INTERVAL keeps us safely under
        it so the buffer stays empty and latency is constant."""
        while True:
            await self._evt.wait()
            self._evt.clear()
            items = list(self._pending.items())
            self._pending.clear()
            for _channel, payload in items:
                await self._write(payload)
            await asyncio.sleep(SEND_INTERVAL)  # pace to real drain rate

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def queue(self, channel: str, payload: bytes):
        """Thread-safe: schedule the latest payload for a channel to be sent by
        the writer. Newer values overwrite older un-sent ones (coalescing)."""
        self.loop.call_soon_threadsafe(self._set_pending, channel, payload)

    def _set_pending(self, channel, payload):
        self._pending[channel] = payload
        if self._evt is not None:
            self._evt.set()

    def _set_connected(self, state: bool):
        self.conn_cb(state)

    # ---- async operations ----
    async def _scan_once(self, timeout=8.0):
        devices = await BleakScanner.discover(timeout=timeout)
        for d in devices:
            name = (d.name or "").lower()
            if any(name.startswith(p) for p in NAME_PREFIXES):
                return d.address
        return None

    async def _scan(self):
        self.status_cb("Scanning...")
        addr = await self._scan_once()
        if not addr:
            # auto-retry once — strips sometimes miss the first discovery window
            self.status_cb("Nothing yet, retrying scan...")
            await asyncio.sleep(1.0)
            addr = await self._scan_once()
        if addr:
            self.address = addr
            with open(ADDR_FILE, "w") as f:
                f.write(addr)
            self.status_cb(f"Found {addr}")
            return addr
        self.status_cb("No strip found — is it on and the phone app closed?")
        return None

    async def _connect(self):
        if self.client and self.client.is_connected:
            self.status_cb("Already connected")
            self._set_connected(True)
            return
        if not self.address:
            if os.path.exists(ADDR_FILE):
                self.address = open(ADDR_FILE).read().strip()
            if not self.address:
                await self._scan()
            if not self.address:
                return
        # On Windows, connecting by raw address only works if the device was
        # freshly discovered — resolve a BLEDevice via a scan first.
        self.status_cb(f"Locating {self.address}...")
        device = await BleakScanner.find_device_by_address(self.address, timeout=12.0)
        if device is None:
            self.status_cb("Strip not advertising — is it on and the phone app closed?")
            self._set_connected(False)
            return
        self.status_cb(f"Connecting to {self.address}...")
        try:
            self.client = BleakClient(device, timeout=20.0,
                                      disconnected_callback=self._on_disconnect)
            await self.client.connect()
            self.status_cb(f"Connected  ({self.address})")
            self._set_connected(True)
        except Exception as e:
            self.client = None
            self.status_cb(f"Connect failed: {e}")
            self._set_connected(False)

    def _on_disconnect(self, _client):
        # called by bleak from its own thread/loop
        self._set_connected(False)
        self.status_cb("Device disconnected")

    async def _disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.client = None
        self._set_connected(False)
        self.status_cb("Disconnected")

    async def _write(self, payload):
        if not self.client or not self.client.is_connected:
            await self._connect()
        if self.client and self.client.is_connected:
            try:
                await self.client.write_gatt_char(WRITE_UUID, payload, response=False)
            except Exception as e:
                self.status_cb(f"Write failed: {e}")
                self.client = None
                self._set_connected(False)


class ScreenSync:
    """Samples a chosen monitor's average color and pushes it to the strip via
    the BLE worker's coalescing 'color' channel. Runs in its own thread."""
    def __init__(self, ble, status_cb, color_cb, get_brightness):
        self.ble = ble
        self.status_cb = status_cb
        self.color_cb = color_cb            # called with (r,g,b) for the swatch
        self.get_brightness = get_brightness
        self.monitor_index = 1              # which mss monitor (1..n); set from GUI
        self.style = "Balanced"
        self.alpha = 0.30                   # smoothing: higher = snappier
        # Capture a bit above the BLE send rate (~12.5/s) so each send uses a
        # fresh frame; the writer coalesces and drops any extras. No point
        # capturing faster than the strip can display.
        self.fps = 15
        self._running = False
        self._thread: threading.Thread | None = None
        self._prev = None                   # smoothed color (float rgb)
        self._last_bright = np.array([255.0, 150.0, 80.0])  # fallback warm glow

    @staticmethod
    def list_monitors():
        """Return list of (index, label) for individual monitors."""
        import mss
        out = []
        with mss.MSS() as sct:
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue  # skip the combined virtual screen
                out.append((i, f"Display {i} - {m['width']}x{m['height']}"))
        return out

    def start(self):
        if self._running:
            return
        self._running = True
        self._prev = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    @property
    def running(self):
        return self._running

    def _extract(self, arr):
        """arr = HxWx4 BGRA uint8. Returns (r,g,b) ints after style + dark-glow."""
        sat, gamma, center = SYNC_STYLES.get(self.style, SYNC_STYLES["Balanced"])
        h, w = arr.shape[:2]
        if center:
            # sample the central 60% for a punchier dominant color
            y0, y1 = int(h * 0.2), int(h * 0.8)
            x0, x1 = int(w * 0.2), int(w * 0.8)
            region = arr[y0:y1, x0:x1]
        else:
            region = arr
        # subsample for speed, take BGR channels, average
        sub = region[::8, ::8, :3].astype(np.float32)
        b, g, r = sub[..., 0].mean(), sub[..., 1].mean(), sub[..., 2].mean()
        rgb = np.array([r, g, b])

        # gamma (brighten mids for Vivid)
        if gamma != 1.0:
            rgb = 255.0 * np.power(np.clip(rgb / 255.0, 0, 1), gamma)

        # saturation boost in HSV
        if sat != 1.0:
            hh, ss, vv = colorsys.rgb_to_hsv(*(rgb / 255.0))
            ss = min(1.0, ss * sat)
            rgb = np.array(colorsys.hsv_to_rgb(hh, ss, vv)) * 255.0

        # dark-scene / DRM-black handling -> dim glow of the last bright color
        luma = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        if luma < SYNC_BLACK_LUMA:
            rgb = self._last_bright * SYNC_GLOW_SCALE
        else:
            self._last_bright = rgb.copy()

        # temporal smoothing (anti-strobe)
        if self._prev is None:
            self._prev = rgb
        else:
            self._prev = self._prev * (1 - self.alpha) + rgb * self.alpha
        out = np.clip(self._prev, 0, 255).astype(int)
        return int(out[0]), int(out[1]), int(out[2])

    def _run(self):
        import mss
        interval = 1.0 / max(1, self.fps)
        self.status_cb(f"Screen Sync ON — {self.style}")
        try:
            with mss.MSS() as sct:
                while self._running:
                    t0 = time.perf_counter()
                    try:
                        mon = sct.monitors[self.monitor_index]
                    except (IndexError, KeyError):
                        self.status_cb("Sync: selected display not found")
                        break
                    shot = sct.grab(mon)
                    arr = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                        shot.height, shot.width, 4)
                    r, g, b = self._extract(arr)
                    scale = self.get_brightness() / 100.0
                    self.ble.queue("color", cmd_color(int(r * scale),
                                                      int(g * scale), int(b * scale)))
                    self.color_cb((r, g, b))
                    dt = time.perf_counter() - t0
                    if dt < interval:
                        time.sleep(interval - dt)
        except Exception as e:
            self.status_cb(f"Sync stopped: {e}")
        finally:
            self._running = False
            self.status_cb("Screen Sync OFF")


class App:
    def __init__(self, root):
        self.root = root
        self.ble = BleWorker(self.set_status, self.set_connected)
        self.base_color = (255, 255, 255)   # current color before brightness scaling
        self.brightness = 100               # percent

        root.title("ELK-BLEDOM Controller")
        root.configure(padx=16, pady=14)
        root.resizable(False, False)

        # --- connection row (with live indicator) ---
        top = tk.Frame(root)
        top.pack(fill="x", pady=(0, 10))
        tk.Button(top, text="Scan", width=7, command=self.on_scan).pack(side="left")
        tk.Button(top, text="Connect", width=8, command=self.on_connect).pack(side="left", padx=4)
        tk.Button(top, text="Disconnect", width=9, command=self.on_disconnect).pack(side="left")
        self.dot = tk.Canvas(top, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="right")
        self._dot_id = self.dot.create_oval(2, 2, 14, 14, fill="#c62828", outline="")
        tk.Label(top, text="link:").pack(side="right", padx=(0, 3))

        # --- power row ---
        pw = tk.Frame(root)
        pw.pack(fill="x", pady=4)
        tk.Button(pw, text="ON",  width=12, height=2, bg="#2e7d32", fg="white",
                  command=lambda: self.send(cmd_on())).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(pw, text="OFF", width=12, height=2, bg="#b71c1c", fg="white",
                  command=lambda: self.send(cmd_off())).pack(side="left", expand=True, fill="x")

        # --- color swatch + picker ---
        tk.Label(root, text="Color", anchor="w").pack(fill="x", pady=(12, 2))
        self.swatch = tk.Label(root, bg="#ffffff", height=2, relief="sunken", bd=1)
        self.swatch.pack(fill="x")
        cbtn = tk.Frame(root)
        cbtn.pack(fill="x", pady=6)
        tk.Button(cbtn, text="Pick color...", command=self.on_pick).pack(side="left")

        presets = tk.Frame(root)
        presets.pack(fill="x")
        for i, (label, rgb) in enumerate(PRESETS):
            hexc = "#%02x%02x%02x" % rgb
            b = tk.Button(presets, text=label, bg=hexc,
                          fg="black" if sum(rgb) > 380 else "white",
                          width=8, command=lambda c=rgb: self.set_color(c))
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")
        for c in range(3):
            presets.columnconfigure(c, weight=1)

        # --- brightness ---
        tk.Label(root, text="Brightness", anchor="w").pack(fill="x", pady=(12, 0))
        self.bright = tk.Scale(root, from_=1, to=100, orient="horizontal",
                               command=self.on_brightness)
        self.bright.set(100)
        self.bright.pack(fill="x")

        # --- effects ---
        tk.Label(root, text="Effect", anchor="w").pack(fill="x", pady=(12, 0))
        er = tk.Frame(root)
        er.pack(fill="x")
        self.effect_var = tk.StringVar(value=list(EFFECTS)[0])
        ttk.Combobox(er, textvariable=self.effect_var, values=list(EFFECTS),
                     state="readonly", width=22).pack(side="left", fill="x", expand=True)
        tk.Button(er, text="Apply", command=self.on_effect).pack(side="left", padx=4)

        tk.Label(root, text="Effect speed", anchor="w").pack(fill="x", pady=(10, 0))
        self.speed = tk.Scale(root, from_=1, to=100, orient="horizontal",
                              command=self.on_speed)
        self.speed.set(50)
        self.speed.pack(fill="x")

        # --- screen sync (ambilight) ---
        ttk.Separator(root, orient="horizontal").pack(fill="x", pady=(14, 8))
        tk.Label(root, text="Screen Sync (Ambilight)",
                 font=("", 9, "bold"), anchor="w").pack(fill="x")

        self.sync = ScreenSync(self.ble, self.set_status,
                               self.set_sync_swatch, lambda: self.brightness)
        try:
            self._monitors = ScreenSync.list_monitors()   # [(index, label), ...]
        except Exception as e:
            self._monitors = []
            self.set_status(f"Monitor detection failed: {e}")

        sr = tk.Frame(root)
        sr.pack(fill="x", pady=(4, 0))
        tk.Label(sr, text="Display:").pack(side="left")
        self.display_var = tk.StringVar()
        labels = [lbl for _, lbl in self._monitors] or ["(none found)"]
        self.display_cb = ttk.Combobox(sr, textvariable=self.display_var,
                                       values=labels, state="readonly", width=20)
        self.display_cb.current(0)
        self.display_cb.pack(side="left", padx=4, fill="x", expand=True)
        self.display_cb.bind("<<ComboboxSelected>>", self.on_display_change)

        sr2 = tk.Frame(root)
        sr2.pack(fill="x", pady=(6, 0))
        tk.Label(sr2, text="Style:").pack(side="left")
        self.style_var = tk.StringVar(value="Balanced")
        style_cb = ttk.Combobox(sr2, textvariable=self.style_var,
                                values=list(SYNC_STYLES), state="readonly", width=12)
        style_cb.pack(side="left", padx=4)
        style_cb.bind("<<ComboboxSelected>>", self.on_style_change)
        tk.Label(sr2, text="Smoothing:").pack(side="left", padx=(8, 0))
        self.smooth = tk.Scale(sr2, from_=5, to=100, orient="horizontal",
                               showvalue=False, command=self.on_smooth, length=90)
        self.smooth.set(30)
        self.smooth.pack(side="left", fill="x", expand=True)

        self.sync_btn = tk.Button(root, text="▶ Start Screen Sync", height=2,
                                  bg="#1565c0", fg="white", command=self.on_toggle_sync)
        self.sync_btn.pack(fill="x", pady=(6, 0))
        # apply the initially-selected display index
        self.on_display_change()

        # --- run at startup ---
        ttk.Separator(root, orient="horizontal").pack(fill="x", pady=(12, 6))
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        cb = tk.Checkbutton(root, text="Run automatically when Windows starts",
                            variable=self.startup_var, command=self.on_startup_toggle,
                            anchor="w")
        if winreg is None:
            cb.config(state="disabled")  # non-Windows: not supported
        cb.pack(fill="x")

        # --- status bar ---
        self.status = tk.Label(root, text="Ready", anchor="w", fg="#555",
                               relief="sunken", bd=1, padx=6)
        self.status.pack(fill="x", pady=(14, 0))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # auto-connect on launch
        self.ble.submit(self.ble._connect())

    # ---- thread-safe UI updates ----
    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def set_connected(self, state):
        color = "#2e7d32" if state else "#c62828"
        self.root.after(0, lambda: self.dot.itemconfig(self._dot_id, fill=color))

    def send(self, payload):
        self.ble.submit(self.ble._write(payload))

    def _apply_current_color(self):
        r, g, b = self.base_color
        s = self.brightness / 100.0
        # coalesced channel: rapid slider drags collapse to the latest color
        self.ble.queue("color", cmd_color(int(r * s), int(g * s), int(b * s)))

    def set_color(self, rgb):
        self.base_color = rgb
        self.swatch.config(bg="#%02x%02x%02x" % rgb)
        self._apply_current_color()

    # ---- button callbacks ----
    def on_scan(self):        self.ble.submit(self.ble._scan())
    def on_connect(self):     self.ble.submit(self.ble._connect())
    def on_disconnect(self):  self.ble.submit(self.ble._disconnect())

    def on_pick(self):
        rgb, _ = colorchooser.askcolor(color="#%02x%02x%02x" % self.base_color,
                                       title="Pick a color")
        if rgb:
            self.set_color(tuple(int(x) for x in rgb))

    def on_brightness(self, val):
        self.brightness = int(float(val))
        self._apply_current_color()

    def on_effect(self):
        v = EFFECTS[self.effect_var.get()]
        self.send(cmd_effect(v))

    def on_speed(self, val):
        self.ble.queue("speed", cmd_effect_speed(int(float(val))))

    # ---- screen sync callbacks ----
    def set_sync_swatch(self, rgb):
        # show the live synced color in the swatch (thread-safe)
        self.root.after(0, lambda: self.swatch.config(bg="#%02x%02x%02x" % rgb))

    def on_display_change(self, _evt=None):
        if not self._monitors:
            return
        idx = self.display_cb.current()
        self.sync.monitor_index = self._monitors[idx][0]

    def on_style_change(self, _evt=None):
        self.sync.style = self.style_var.get()
        if self.sync.running:
            self.set_status(f"Screen Sync ON — {self.sync.style}")

    def on_smooth(self, val):
        # slider 5..100 -> alpha 0.05..1.0 (higher = snappier, lower = smoother)
        self.sync.alpha = max(0.05, int(float(val)) / 100.0)

    def on_startup_toggle(self):
        want = self.startup_var.get()
        if set_startup(want):
            self.set_status("Will run at startup" if want else "Startup entry removed")
        else:
            self.startup_var.set(not want)  # revert the checkbox on failure
            self.set_status("Couldn't change startup setting")

    def on_toggle_sync(self):
        if self.sync.running:
            self.sync.stop()
            self.sync_btn.config(text="▶ Start Screen Sync", bg="#1565c0")
        else:
            if not self._monitors:
                self.set_status("No display detected for sync")
                return
            self.send(cmd_on())          # make sure the strip is on
            self.sync.start()
            self.sync_btn.config(text="■ Stop Screen Sync", bg="#c62828")

    def on_close(self):
        try:
            self.sync.stop()
            self.ble.submit(self.ble._disconnect())
        finally:
            self.root.after(200, self.root.destroy)


if __name__ == "__main__":
    _lock = acquire_single_instance_lock()
    if _lock is None:
        # hidden root just to show the dialog
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showwarning(
            "ELK-BLEDOM Controller",
            "The controller is already running.\n\n"
            "Only one copy can hold the Bluetooth connection at a time — "
            "switch to the window that's already open.")
        _r.destroy()
        raise SystemExit(0)

    root = tk.Tk()
    App(root)
    root.mainloop()
