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
import json
import math
import os
import re
import socket
import sys
import threading
import time
import tkinter as tk
import urllib.request
from types import SimpleNamespace
import webbrowser
from tkinter import messagebox, simpledialog, ttk

import numpy as np
from bleak import BleakScanner, BleakClient

from elk_protocol import (NAME_PREFIXES, EFFECTS, is_strip_name, pick_write_char,
                          cmd_on, cmd_off, cmd_color, cmd_effect, cmd_effect_speed,
                          data_file, APP_DIR)
import rgbzap_api as api_mod
import rgbzap_hotkeys as hotkeys
import rgbzap_obs as obs_mod
import rgbzap_onair as onair_mod
import rgbzap_session as session_mod
import rgbzap_tools
from rgbzap_comfort import FADES, FlashLimiter, apply_balance, fade_steps
from rgbzap_sync import AREAS, crop_black_bars, select_area

try:
    import winreg  # Windows only — used for the "run at startup" option
except ImportError:
    winreg = None

try:  # Windows notification center (pip install winrt-Windows.UI.Notifications.Management)
    from winrt.windows.ui.notifications import NotificationKinds, KnownNotificationBindings
    from winrt.windows.ui.notifications.management import (
        UserNotificationListener, UserNotificationListenerAccessStatus)
except ImportError:
    UserNotificationListener = None    # no notification alerts; toggles show disabled

try:
    import pystray                     # system tray icon (pip install pystray)
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None                     # no tray: closing the window quits, as before

_HERE = os.path.dirname(os.path.abspath(__file__))     # where pre-1.0 versions kept data
ADDR_FILE = data_file("address.txt", legacy=os.path.join(_HERE, "elk_address.txt"))
# logo / icons; inside the built exe they're unpacked under sys._MEIPASS
ASSETS = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                      "assets")
APP_VERSION = "1.3.0"
APP_URL = "https://github.com/TGDRangaka/RGBZap"
AUTHOR_URL = "https://dilshan-rangaka.vercel.app/"
# arbitrary localhost port used as a single-instance lock (RGBZAP_LOCK_PORT lets tests run
# a second, isolated copy next to a running one)
LOCK_PORT = int(os.environ.get("RGBZAP_LOCK_PORT") or 49731)
# Pace BLE writes to the strip's measured sustainable rate (~13/s ceiling).
# 80ms => ~12.5/s, safely under the ceiling so the send buffer never backs up.
SEND_INTERVAL = 0.08


def ripple_envelope(t):
    """t in 0..1 -> brightness level 0..1 for the 'connected' ripple.
    Snaps up to full, then dips through several decaying bounces and settles at ~1.0.
    Using dips (subtractive) instead of an overshooting cosine keeps every bounce
    visible instead of clipping the peaks flat at 1.0."""
    rise = min(1.0, t / 0.10)                                  # quick jump to full
    dips = math.exp(-2.4 * t) * abs(math.sin(2 * math.pi * 2.0 * t))  # 4 decaying bounces
    return max(0.0, min(1.0, rise * (1.0 - 0.9 * dips)))


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

AUDIO_MODES = {
    # name -> one-line description shown under the picker
    "Pulse":       "Bass drives the brightness of your color",
    "Beat flash":  "Your color flashes on every beat, then fades",
    "Beat colors": "Jumps to a new color on every beat",
    "Spectrum":    "Bass = red, mids = green, highs = blue",
    "Pitch":       "Low sounds glow red, high sounds glow blue",
}
SYSTEM_AUDIO = "System audio (what's playing)"


# ---- software flame effects: brightness-only flicker on the current color ----
# name -> (floor, flicker, gusts_per_sec)
#   floor   = lowest brightness as a fraction of the slider (never goes dark)
#   flicker = share of the swing from fast jitter (rest is slow "breathing" drift)
#   gusts   = how often a draft makes the flame dip suddenly
FLAME_EFFECTS = {
    "Candle": (0.70, 0.25, 0.08),
    "Torch":  (0.45, 0.40, 0.25),
    "Fire":   (0.25, 0.55, 0.60),
}


class Flame:
    """Smooth random brightness 0..1 for a flame. Three layers: slow drift toward
    random targets, fast eased jitter, and occasional gusts that dip quickly and
    recover slowly. Pure (no I/O) so it's testable."""
    def __init__(self, floor, flicker, gusts, rng=None):
        import random
        self.floor, self.flicker, self.gusts = floor, flicker, gusts
        self.rng = rng or random.Random()
        self.slow = self.slow_target = 0.8
        self.fast = self.fast_target = 0.5
        self.gust = self.gust_target = 0.0

    def step(self, dt, speed=50):
        """Advance dt seconds; speed 1..100 (50 = natural). Returns level in [floor, 1]."""
        r, sf = self.rng, 0.4 + speed / 50.0          # speed factor ~0.42..2.4
        if r.random() < dt * 0.7 * sf:                # new breathing target now and then
            self.slow_target = r.uniform(0.55, 1.0)
        self.slow += (self.slow_target - self.slow) * min(1.0, dt * 1.5 * sf)
        if r.random() < min(1.0, dt * 6.0 * sf):      # new jitter target most ticks
            self.fast_target = r.random()
        self.fast += (self.fast_target - self.fast) * min(1.0, dt * 6.0 * sf)
        if r.random() < dt * self.gusts * sf:         # a draft hits the flame
            self.gust_target = r.uniform(0.4, 0.8)
        self.gust_target *= math.exp(-dt * 2.5 * sf)  # draft fades -> slow recovery
        self.gust += (self.gust_target - self.gust) * min(1.0, dt * 4.0 * sf)
        v = self.slow * (1 - self.flicker) + self.fast * self.flicker - self.gust * 0.6
        v = max(0.0, min(1.0, v))
        return self.floor + (1.0 - self.floor) * v


# ---- more software effects. step(dt, speed) returns EITHER a float level 0..1
# (brightness of the user's color) OR an (r, g, b) tuple at full brightness (the
# effect picks the color). The brightness slider scales both. speed 50 = natural.
def _sf(speed):
    """Speed slider 1..100 -> rate factor 0.31..1.7, exactly 1.0 at 50 (natural pace)."""
    return 0.3 + 0.7 * speed / 50.0


class Breathe:
    """Slow sine swell between a dim floor and full, on the current color."""
    def __init__(self):
        self.t = 0.0

    def step(self, dt, speed=50):
        self.t += dt * _sf(speed)
        return 0.10 + 0.90 * (0.5 - 0.5 * math.cos(2 * math.pi * self.t / 6.0))  # 6 s


class Heartbeat:
    """Lub-dub double pulse then a rest, ~60 bpm at speed 50, on the current color."""
    def __init__(self):
        self.t = 0.0

    def step(self, dt, speed=50):
        self.t = (self.t + dt * _sf(speed)) % 1.0     # one beat = 1.0 s at sf 1
        # each pulse is a 0.22 s sine bump: wider than ~2 send ticks (80 ms), so the
        # strip can't miss a beat between updates. lub at 0, dub at 0.32.
        bump = lambda x: math.sin(math.pi * x / 0.22) if 0 <= x < 0.22 else 0.0
        return 0.20 + 0.80 * max(bump(self.t), 0.75 * bump(self.t - 0.32))


class Lightning:
    """Dim night-blue sky with random white double/triple flashes (picks its colors)."""
    PICKS_COLOR = True                            # ignores the user's color
    # LEDs look far brighter than their value at the low end, so the sky must be
    # tiny next to the bolt (was 20,35,110: too bright, flashes barely stood out)
    SKY, BOLT = (2, 4, 16), (255, 255, 255)

    def __init__(self, rng=None):
        import random
        self.rng = rng or random.Random()
        self.seq = []                                 # flash levels still to play, 1/tick

    def step(self, dt, speed=50):
        r = self.rng
        if not self.seq and r.random() < dt * 0.35 * _sf(speed):   # a strike begins
            self.seq = []
            for _ in range(r.randint(1, 3)):
                self.seq += [r.uniform(0.7, 1.0), r.uniform(0.0, 0.3)]
            self.seq += [0.15, 0.05]                  # afterglow
        lvl = self.seq.pop(0) if self.seq else 0.0
        return tuple(int(s + (b - s) * lvl) for s, b in zip(self.SKY, self.BOLT))


class Rainbow:
    """Slow trip around the color wheel, ~30 s per lap at speed 50 (picks its colors)."""
    PICKS_COLOR = True                            # ignores the user's color
    def __init__(self):
        self.h = 0.0

    def step(self, dt, speed=50):
        self.h = (self.h + dt * _sf(speed) / 30.0) % 1.0
        return hs_rgb(self.h, 1.0)


class Ocean:
    """Calm drift through blues and teals with gentle swells (picks its colors)."""
    PICKS_COLOR = True                            # ignores the user's color
    def __init__(self, rng=None):
        import random
        self.rng = rng or random.Random()
        self.h = self.h_target = 0.55
        self.v = self.v_target = 0.8

    def step(self, dt, speed=50):
        r, sf = self.rng, _sf(speed)
        if r.random() < dt * 0.4 * sf:
            self.h_target = r.uniform(0.47, 0.64)     # teal .. deep blue
        if r.random() < dt * 0.6 * sf:
            self.v_target = r.uniform(0.55, 1.0)
        self.h += (self.h_target - self.h) * min(1.0, dt * 0.6 * sf)
        self.v += (self.v_target - self.v) * min(1.0, dt * 1.0 * sf)
        return tuple(int(c * self.v) for c in hs_rgb(self.h, 0.9))


class Police:
    """Red double-blink, then blue double-blink (picks its colors)."""
    PICKS_COLOR = True                            # ignores the user's color
    PATTERN = [(255, 0, 0), (0, 0, 0), (255, 0, 0), (0, 0, 0),
               (0, 0, 255), (0, 0, 0), (0, 0, 255), (0, 0, 0)]

    def __init__(self):
        self.t = 0.0

    def step(self, dt, speed=50):
        # one pattern step lasts >= 2 ticks at speed 50, so no blink falls between sends
        self.t += dt * _sf(speed)
        return self.PATTERN[int(self.t / 0.16) % len(self.PATTERN)]


class Drift:
    """Slow wander through a palette (picks its colors): eases from one palette color to
    the next over a few seconds, with a gentle brightness swell on top. It turns around
    at the ends of the palette (ping-pong) so only neighbouring colors ever blend."""
    PICKS_COLOR = True
    SWELL_PERIOD = 11.0                           # seconds per brightness swell

    def __init__(self, palette, swell=0.18, hold=(4.0, 9.0), rng=None):
        import random
        self.palette, self.swell, self.hold = palette, swell, hold
        self.rng = rng or random.Random()
        self.i, self.dir = 0, 1
        self.j = self._advance(0)                 # color we're blending toward
        self.t, self.dur = 0.0, self.rng.uniform(*hold)
        self.clock = 0.0
        self.phase = self.rng.random() * 2 * math.pi   # tiles shouldn't all swell in step

    def _advance(self, i):
        if not 0 <= i + self.dir < len(self.palette):
            self.dir = -self.dir
        return i + self.dir

    def step(self, dt, speed=50):
        sf = _sf(speed)
        self.t += dt * sf
        self.clock += dt * sf
        while self.t >= self.dur:                 # a very slow tick can't skip a color
            self.t -= self.dur
            self.i, self.j = self.j, self._advance(self.j)
            self.dur = self.rng.uniform(*self.hold)
        f = self.t / self.dur
        f = f * f * (3 - 2 * f)                   # ease in/out
        a, b = self.palette[self.i], self.palette[self.j]
        lvl = 1.0 - self.swell * (0.5 - 0.5 * math.cos(
            self.phase + 2 * math.pi * self.clock / self.SWELL_PERIOD))
        return tuple(max(0, min(255, int(round((x + (y - x) * f) * lvl))))
                     for x, y in zip(a, b))


# name -> (palette, swell depth) for the slow "Ambient" effects. Each palette lists
# neighbours that blend cleanly (no muddy grey from complementary colors).
DRIFT_PALETTES = {
    "Aurora":    ([(0, 255, 110), (0, 230, 190), (40, 140, 255), (140, 60, 255)], 0.20),
    "Lava lamp": ([(255, 30, 0), (255, 110, 0), (255, 0, 70), (190, 0, 140)], 0.25),
    "Vaporwave": ([(255, 50, 190), (150, 70, 255), (0, 200, 255)], 0.12),
    "Sunset":    ([(255, 200, 60), (255, 110, 0), (255, 40, 60), (170, 20, 140),
                   (60, 20, 130)], 0.15),
    "Forest":    ([(0, 110, 20), (60, 200, 40), (0, 170, 90), (130, 220, 50)], 0.30),
    "Galaxy":    ([(30, 0, 120), (110, 0, 200), (0, 50, 210), (190, 0, 160)], 0.35),
    "Ice":       ([(160, 225, 255), (40, 140, 255), (0, 200, 220), (225, 245, 255)], 0.10),
}


class Breath478:
    """Guided breathing on your color: swell in for 4 s, hold 7 s, ease out for 8 s (the
    4-7-8 pattern). One breath is 19 s at the natural speed; the speed slider changes
    the pace."""
    CYCLE, LOW = 19.0, 0.08

    def __init__(self):
        self.t = 0.0

    def step(self, dt, speed=50):
        self.t = (self.t + dt * _sf(speed)) % self.CYCLE
        t, lo = self.t, self.LOW
        if t < 4.0:                               # inhale
            return lo + (1 - lo) * (0.5 - 0.5 * math.cos(math.pi * t / 4.0))
        if t < 11.0:                              # hold
            return 1.0
        return lo + (1 - lo) * (0.5 + 0.5 * math.cos(math.pi * (t - 11.0) / 8.0))  # exhale


class NeonFlicker:
    """A neon sign on its last legs: a steady, faintly humming glow on your color with
    the odd stutter of one to three quick dropouts."""
    def __init__(self, rng=None):
        import random
        self.rng = rng or random.Random()
        self.seq = []                             # levels still to play, one per tick
        self.t = 0.0

    def step(self, dt, speed=50):
        r, sf = self.rng, _sf(speed)
        if not self.seq and r.random() < dt * 0.2 * sf:       # a stutter begins
            for _ in range(r.randint(1, 3)):
                self.seq += [r.uniform(0.12, 0.45)] + [r.uniform(0.85, 1.0)] * r.randint(1, 2)
        if self.seq:
            return self.seq.pop(0)
        self.t += dt * sf
        return 0.93 + 0.07 * math.sin(self.t * 7.0)           # the hum


class Fireworks:
    """Near-dark sky with a burst every second or two: a flash in a random color that
    sparkles and fades like a spent rocket (picks its colors)."""
    PICKS_COLOR = True
    SKY, LIFE = (1, 2, 8), 1.6

    def __init__(self, rng=None):
        import random
        self.rng = rng or random.Random()
        self.wait, self.age, self.rgb = self.rng.uniform(0.3, 1.2), None, (0, 0, 0)

    def step(self, dt, speed=50):
        r, sf = self.rng, _sf(speed)
        if self.age is None:                      # waiting for the next rocket
            self.wait -= dt * sf
            if self.wait > 0:
                return self.SKY
            self.age, self.rgb = 0.0, hs_rgb(r.random(), r.uniform(0.75, 1.0))
        self.age += dt * sf
        x = self.age / self.LIFE
        if x >= 1.0:
            self.age, self.wait = None, r.uniform(0.8, 2.6)
            return self.SKY
        lvl = (1.0 - x) ** 2 * (0.7 + 0.3 * r.random())       # fade + sparkle
        return tuple(max(s, int(c * lvl)) for s, c in zip(self.SKY, self.rgb))


class Party:
    """Disco: hops to a fresh bright color every ~0.6 s with a pulse on each hop (picks
    its colors). Hops are capped at ~2 per second at any speed, so it stays a party,
    not a strobe."""
    PICKS_COLOR = True

    def __init__(self, rng=None):
        import random
        self.rng = rng or random.Random()
        self.t, self.hue = 0.0, self.rng.random()

    def step(self, dt, speed=50):
        hop = max(0.45, 0.6 / _sf(speed))
        self.t += dt
        if self.t >= hop:
            self.t -= hop
            self.hue = (self.hue + self.rng.uniform(0.18, 0.5)) % 1.0
        lvl = 1.0 - 0.45 * (self.t / hop)         # bright on the hop, easing down
        return tuple(int(c * lvl) for c in hs_rgb(self.hue, 1.0))


# name -> factory for every app-driven effect (they stop when RGBZap quits; the
# controller's built-in EFFECTS keep running without the PC)
SOFT_EFFECTS = {
    **{name: (lambda p=p: Flame(*p)) for name, p in FLAME_EFFECTS.items()},
    "Breathe": Breathe, "Breath 4-7-8": Breath478, "Heartbeat": Heartbeat,
    "Neon flicker": NeonFlicker, "Lightning": Lightning, "Fireworks": Fireworks,
    "Rainbow": Rainbow, "Ocean": Ocean, "Police": Police, "Party": Party,
    **{name: (lambda p=p, s=s: Drift(p, s)) for name, (p, s) in DRIFT_PALETTES.items()},
}

# how the Effects tab and tray menu sort SOFT_EFFECTS (every effect in exactly one group)
SOFT_GROUPS = [
    ("Ambient", ["Rainbow", "Ocean", *DRIFT_PALETTES]),
    ("Flames & pulses", [*FLAME_EFFECTS, "Breathe", "Breath 4-7-8", "Heartbeat",
                         "Neon flicker", "Lightning", "Fireworks", "Police", "Party"]),
]

# ---- notification / call alerts ----
# ponytail: wording heuristic, English only; add per-app rules if a caller app is missed
_CALL_RE = re.compile(r"incoming (voice |video |audio )?call|is calling|calling you|"
                      r"call from|ringing", re.I)
CALL_MAX_S = 30.0                       # stop pulsing even if the call toast lingers


def is_call(text):
    """True if a notification's app name + text reads like an incoming call."""
    return bool(_CALL_RE.search(text or ""))


ONAIR_MAX_S = 24 * 3600                 # an On-Air light holds until the camera/mic/OBS stops
ONAIR_RED = (255, 0, 0)


def alert_frame(kind, t, color=None, calm=False):
    """(r, g, b) for an alert at t seconds, or None when it's over.
    notify/flash: two quick pulses (1 s) in `color` (white by default); with calm=True
    ("reduce flashing") a single slow swell instead. call: green swell until cancelled.
    onair: steady red, until cancelled."""
    if kind in ("notify", "flash"):
        c = color or (255, 255, 255)
        if calm:
            if t >= 1.4:
                return None
            lvl = math.sin(math.pi * t / 1.4) ** 2
        else:
            if t >= 1.0:
                return None
            lvl = math.sin(math.pi * (t % 0.5) / 0.5) ** 2
        return tuple(int(x * lvl) for x in c)
    if kind == "onair":
        return color or ONAIR_RED
    lvl = 0.15 + 0.85 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 1.2))
    return (0, int(255 * lvl), int(60 * lvl))


_HW_COLORS = {"Red": (255, 0, 0), "Green": (0, 255, 0), "Blue": (0, 0, 255),
              "Yellow": (255, 255, 0), "Cyan": (0, 255, 255), "Magenta": (255, 0, 255),
              "White": (255, 255, 255)}


def builtin_preview(name, t):
    """(r, g, b) a controller built-in effect shows at time t (seconds at speed 50).
    Drives the EFFECTS tab tiles: what the effect does, not the strip's exact timing.
    ponytail: hand-tuned periods; the controller's real ones vary by firmware."""
    kind, _, what = name.partition(" ")
    if what == "7-color":
        cols = list(_HW_COLORS.values())
    elif what == "RGB":
        cols = [_HW_COLORS[c] for c in ("Red", "Green", "Blue")]
    else:
        cols = [_HW_COLORS[c] for c in what.split("-")]
    n = len(cols)
    if kind == "Jump":
        return cols[int(t / 0.7) % n]
    if kind == "Blink":
        i = int(t / 0.35)
        return cols[(i // 2) % n] if i % 2 == 0 else (0, 0, 0)
    if n == 1:                                   # Crossfade one color: swell in and out
        lvl = 0.5 - 0.5 * math.cos(2 * math.pi * t / 2.4)
        return tuple(int(c * lvl) for c in cols[0])
    pos = (t / 1.6) % n                          # Crossfade several: blend into the next
    i, f = int(pos), pos - int(pos)
    f = f * f * (3 - 2 * f)
    a, b = cols[i], cols[(i + 1) % n]
    return tuple(int(x + (y - x) * f) for x, y in zip(a, b))


# ---- persisted settings (restored on next launch) ----
SETTINGS_FILE = data_file("settings.json", legacy=os.path.join(_HERE, "elk_settings.json"))
# global hotkeys: action -> (label, default shortcut). No bare letters: on many keyboards
# Ctrl+Alt is AltGr and Ctrl+Alt+letter types a character. "" = no default shortcut.
HOTKEY_ACTIONS = {
    "toggle_power":    ("Lights on / off",           "ctrl+alt+home"),
    "brightness_up":   ("Brighter",                  "ctrl+alt+pageup"),
    "brightness_down": ("Dimmer",                    "ctrl+alt+pagedown"),
    "toggle_sync":     ("Screen sync on / off",      "ctrl+alt+end"),
    "toggle_audio":    ("Audio reactive on / off",   "ctrl+alt+insert"),
    "next_scene":      ("Next scene",                "ctrl+alt+shift+right"),
    "prev_scene":      ("Previous scene",            "ctrl+alt+shift+left"),
    "effect_off":      ("Effect off (steady color)", ""),
}
DEFAULT_SETTINGS = {
    "color": [255, 255, 255], "brightness": 100, "mode": "color",
    "effect": list(EFFECTS)[0], "speed": 50,
    "display": "", "style": "Balanced", "extract": "Dominant", "dark": "Dim",
    "sat": int(SYNC_STYLES["Balanced"][0] * 100), "smooth": 30,
    "custom": [],                       # saved picker colors, newest first
    "strips": [],                       # user's light strips: [[name, address], ...]
    "notify_flash": False, "notify_call": False,   # alert on notifications / calls
    "update_check": True,               # ask GitHub for a newer release (notify only)
    "off_on_shutdown": True,            # switch the lights off when the PC shuts down
    "fade": "Smooth",                   # how color changes blend: Off / Quick / Smooth / Slow
    "reduce_flash": False,              # soften flashing effects, alerts and sync cuts
    "area": "Whole screen", "crop_bars": True,     # screen sync: where to sample, skip bars
    "bal_r": 100, "bal_g": 100, "bal_b": 100,      # strip color balance (% gain per channel)
    "hotkeys_on": True,
    "hotkeys": {a: d for a, (_label, d) in HOTKEY_ACTIONS.items()},
    "onair": False, "onair_cam": True, "onair_mic": False,   # red while camera / mic is on
    "obs_on": False, "obs_host": "localhost", "obs_port": 4455, "obs_password": "",
    "obs_live": True, "obs_rec": False,            # red while streaming / recording
    "obs_scenes": {},                   # OBS scene name -> RGBZap scene name
    "api_on": False, "api_port": 49732, "api_token": "",   # local control API (off by default)
    "audio_mode": "Pulse", "audio_source": SYSTEM_AUDIO,
    "audio_sens": 50, "audio_smooth": 40, "audio_floor": 8,
    # named snapshots of the LIGHT tab; starters until the user changes the list
    "scenes": {
        "Focus":       {"color": [180, 220, 255], "brightness": 100, "mode": "color",
                        "effect": "Candle", "speed": 50},
        "Movie night": {"color": [255, 138, 18], "brightness": 20, "mode": "color",
                        "effect": "Candle", "speed": 50},
        "Cozy fire":   {"color": [255, 90, 0], "brightness": 70, "mode": "effect",
                        "effect": "Candle", "speed": 40},
        "Relax":       {"color": [150, 0, 255], "brightness": 60, "mode": "effect",
                        "effect": "Breathe", "speed": 30},
        "Party":       {"color": [255, 255, 255], "brightness": 100, "mode": "effect",
                        "effect": "Rainbow", "speed": 90},
        "Storm":       {"color": [255, 255, 255], "brightness": 90, "mode": "effect",
                        "effect": "Lightning", "speed": 50},
        "Night light": {"color": [255, 60, 10], "brightness": 6, "mode": "color",
                        "effect": "Candle", "speed": 50},
    },
}
SCENE_FIELDS = ("color", "brightness", "mode", "effect", "speed")
CUSTOM_MAX = 9                          # one chip row, same width as the presets


def _is_rgb(c):
    return (type(c) is list and len(c) == 3
            and all(type(x) is int and 0 <= x <= 255 for x in c))


# ---- live color picker: hue across, saturation down, full value ----
# (brightness has its own slider, so the picker only needs hue + saturation)
def hs_rgb(h, s):
    """Hue 0..1, saturation 0..1 at full value -> (r, g, b) ints."""
    return tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(h % 1.0, s, 1.0))


def hs_image_data(w, h):
    """PhotoImage.put() data for a w x h hue/saturation field. Pixel (x, y) is
    hs_rgb(x/(w-1), 1 - y/(h-1)), so a click maps back to exactly what's shown."""
    hue = np.linspace(0.0, 1.0, w)[None, :].repeat(h, 0) % 1.0
    sat = np.linspace(1.0, 0.0, h)[:, None].repeat(w, 1)
    i = np.floor(hue * 6.0).astype(int) % 6
    f = hue * 6.0 - np.floor(hue * 6.0)
    one, p, q, t = np.ones_like(sat), 1 - sat, 1 - sat * f, 1 - sat * (1 - f)
    r = np.choose(i, [one, q, p, p, t, one])
    g = np.choose(i, [t, one, one, q, p, p])
    b = np.choose(i, [p, p, t, one, one, q])
    rgb = np.rint(np.stack([r, g, b], -1) * 255).astype(int)
    return " ".join("{" + " ".join("#%02x%02x%02x" % tuple(px) for px in row) + "}"
                    for row in rgb)


_SETTING_CHOICES = {
    "mode": ("color", "effect"), "effect": {**SOFT_EFFECTS, **EFFECTS},
    "style": SYNC_STYLES,
    "extract": ("Dominant", "Average"), "dark": ("Dim", "Glow", "Keep"),
    "audio_mode": AUDIO_MODES, "fade": FADES, "area": AREAS,
}
_SETTING_RANGES = {"brightness": (1, 100), "speed": (1, 100), "sat": (100, 250),
                   "smooth": (5, 100), "audio_sens": (1, 100), "audio_smooth": (0, 100),
                   "audio_floor": (0, 50), "bal_r": (50, 100), "bal_g": (50, 100),
                   "bal_b": (50, 100), "obs_port": (1, 65535), "api_port": (1024, 65535)}


def load_settings(path=SETTINGS_FILE):
    """Saved settings merged over defaults. Any missing, mistyped or out-of-range
    value falls back to its default, so a hand-edited/corrupt file can't crash launch."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    cfg = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return cfg
    for k in DEFAULT_SETTINGS:
        v = raw.get(k)
        if k == "custom" and type(v) is list:
            cfg[k] = [c for c in v if _is_rgb(c)][:CUSTOM_MAX]   # drop bad entries only
        elif k == "strips" and type(v) is list:
            cfg[k] = [s for s in v if type(s) is list and len(s) == 2
                      and all(type(x) is str and x for x in s)]
        elif k == "scenes" and type(v) is dict:
            cfg[k] = {name: {f: sc[f] for f in SCENE_FIELDS} for name, sc in v.items()
                      if type(name) is str and 0 < len(name) <= 40 and type(sc) is dict
                      and all(_valid_setting(f, sc.get(f)) for f in SCENE_FIELDS)}
        elif k == "hotkeys":
            saved = v if type(v) is dict else {}
            cfg[k] = {a: (hotkeys.normalize(saved[a]) or "") if type(saved.get(a)) is str
                      and (saved[a] == "" or hotkeys.normalize(saved[a])) else DEFAULT_SETTINGS[k][a]
                      for a in HOTKEY_ACTIONS}
        elif k == "obs_scenes" and type(v) is dict:
            cfg[k] = {n: s for n, s in list(v.items())[:60]
                      if type(n) is str and type(s) is str and 0 < len(n) <= 120
                      and 0 < len(s) <= 40}
        elif k not in ("custom", "strips", "scenes", "hotkeys", "obs_scenes") \
                and _valid_setting(k, v):
            cfg[k] = v
    return cfg


def _valid_setting(k, v):
    """True if v is an acceptable value for scalar setting k (type, choices, range)."""
    if type(v) is not type(DEFAULT_SETTINGS[k]):
        return False
    if k == "color" and not _is_rgb(v):
        return False
    if k == "api_token" and v and not re.fullmatch(r"[0-9a-f]{32}", v):
        return False                        # only tokens we generated (or none)
    if k in ("obs_host", "obs_password") and not (len(v) <= (253 if k == "obs_host" else 200)
                                                  and v.isprintable()):
        return False
    if k in _SETTING_CHOICES and v not in _SETTING_CHOICES[k]:
        return False
    lo_hi = _SETTING_RANGES.get(k)
    return lo_hi is None or lo_hi[0] <= v <= lo_hi[1]


def save_settings(cfg, path=SETTINGS_FILE):
    """Write atomically (temp file + replace) so a crash mid-write can't corrupt it."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)



# ---- UI theme: dark "light console" (accent is the strip's live color) ----
INK, PANEL, PANEL2 = "#0A0C12", "#12151E", "#1A1F2B"
LINE, TEXT, MUTE = "#262D3A", "#E8ECF4", "#8592A6"
GOOD, BAD = "#37D39A", "#FF5D5D"
SYNC_BLUE = "#3B82F6"
AUDIO_PINK = "#E0479E"
FX_COLS, FX_ASPECT = 4, 2.5           # Effects tab tiles: columns per row, width : height
FONT_H1   = ("Bahnschrift SemiBold", 18)
FONT_EYE  = ("Bahnschrift SemiBold", 9)
FONT_TAB  = ("Bahnschrift SemiBold", 10)
FONT_BODY = ("Segoe UI", 10)
FONT_BTN  = ("Segoe UI Semibold", 10)
FONT_MONO = ("Consolas", 10)
FONT_HEX  = ("Consolas", 15)


# --- "run at Windows startup" via the HKCU Run registry key ---
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_APP_NAME = "RGBZap"
_OLD_APP_NAME = "ElkBledomController"     # name used before the RGBZap rename


def _startup_command():
    """Command that launches this GUI with NO console window (pythonw.exe)."""
    if getattr(sys, "frozen", False):     # built RGBZap.exe: just the exe itself
        return f'"{sys.executable}"'
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
            if not val:
                return False
    except OSError:
        return False
    # Task Manager / Settings "Startup apps" can disable the entry without
    # removing it: odd first byte in StartupApproved = disabled.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVED_KEY) as k:
            flag, _ = winreg.QueryValueEx(k, _APP_NAME)
            return not (flag and flag[0] & 1)
    except OSError:
        return True


def set_startup(enable):
    """Add or remove this app from Windows startup. Returns True on success."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, _APP_NAME, 0, winreg.REG_SZ, _startup_command())
                # clear a Task Manager "Disabled" flag, else Windows skips the entry
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVED_KEY, 0,
                                        winreg.KEY_SET_VALUE) as a:
                        winreg.DeleteValue(a, _APP_NAME)
                except OSError:
                    pass
            else:
                try:
                    winreg.DeleteValue(k, _APP_NAME)
                except FileNotFoundError:
                    pass
            try:                             # drop the pre-rename entry either way
                winreg.DeleteValue(k, _OLD_APP_NAME)
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVED_KEY, 0,
                                    winreg.KEY_SET_VALUE) as a:
                    winreg.DeleteValue(a, _OLD_APP_NAME)
            except OSError:
                pass
        return True
    except OSError:
        return False


def migrate_startup():
    """Builds before the RGBZap rename registered under another name: if that
    entry exists, re-register under the new name (set_startup drops the old one)."""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _OLD_APP_NAME)
    except OSError:
        return
    set_startup(True)


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


# ---- update check: one request to GitHub's "latest release" endpoint ----
# Sends only a standard HTTPS request (app name + version in the User-Agent, which
# GitHub requires). Notify-only: it never downloads or runs anything.
RELEASES_API = "https://api.github.com/repos/TGDRangaka/RGBZap/releases/latest"
UPDATE_EVERY_MS = 24 * 3600 * 1000      # re-check daily while the app sits in the tray
UPDATE_RETRY_MS = 3600 * 1000           # after a failed check (offline), try again in 1 h


def parse_version(s):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); anything else (beta tags, junk) -> None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (s or "").strip()) if isinstance(s, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def parse_release(data, current=APP_VERSION):
    """(version, page_url) if `data` (GitHub's latest-release JSON) is a public release
    newer than `current`, else None. The page must be on this project's GitHub, so an
    odd response can never make the app open some other site."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    new, cur, url = parse_version(data.get("tag_name")), parse_version(current), data.get("html_url")
    if not new or not cur or new <= cur:      # tuples compare numerically: 1.10 > 1.9
        return None
    if not isinstance(url, str) or not url.startswith(APP_URL + "/"):
        return None
    return ".".join(map(str, new)), url


def fetch_latest_release(urlopen=urllib.request.urlopen, timeout=8):
    req = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": f"RGBZap/{APP_VERSION}", "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


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
        self.write_uuid: str | None = None   # resolved per device after connect
        self.fx_provider = None              # App-set: () -> (color, brightness) | None
        # coalescing writer state (all touched only inside the loop thread)
        self._pending: dict[str, bytes] = {}
        self._evt: asyncio.Event | None = None
        # One connect at a time. Without this, writes queued at launch (sliders fire
        # their callbacks when initialised) each started their own connect, leaving
        # several clients on the strip; when a stray one dropped, the link the app
        # wrote through went dead while the UI still showed "Connected".
        self._conn_lock = asyncio.Lock()
        self._want_connected = False         # True until the user disconnects/closes
        # alerts (notifications/calls): play over whatever runs, then restore it
        self._last: dict[bytes, bytes] = {}  # frame kind -> latest sent, in send order
        self._held: dict[str, bytes] = {}    # channel frames queued while an alert plays
        self._alert_on = False
        self._alert_kind = None
        self._alert_lock = asyncio.Lock()
        self.alert_cancel = False            # set from any thread: stop the current alert
        self.onair_on = False                # set from any thread: keep the On-Air light lit
        self.frozen = False                  # True while the PC shuts down: writes are dropped
        self.calm = False                    # "Reduce flashing": gentler alerts
        self.balance = (1.0, 1.0, 1.0)       # per-channel gain, fixes a tinted "white"
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._start_writer)
        self.loop.run_forever()

    def _start_writer(self):
        self._evt = asyncio.Event()
        self.loop.create_task(self._writer())
        self.loop.create_task(self._watchdog())

    def _connected(self):
        return bool(self.client and self.client.is_connected and self.write_uuid)

    async def _watchdog(self):
        """Keep the link up: if it should be connected but isn't (strip dropped,
        out of range, power blip), reconnect on its own, backing off 3s -> 30s."""
        delay = 3.0
        while True:
            await asyncio.sleep(delay)
            if not self._want_connected or self._connected():
                delay = 3.0
                continue
            self.status_cb("Link lost — reconnecting...")
            try:
                await self._connect()
            except Exception as e:
                self.status_cb(f"Reconnect failed: {e}")
            delay = 3.0 if self._connected() else min(30.0, delay * 2)

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
                # A transient BLE error (e.g. a scan/reconnect raising mid-write)
                # must NOT kill this loop — that would silently freeze the color
                # channel forever while direct send() writes keep working.
                try:
                    await self._write(payload)
                except Exception as e:
                    self.status_cb(f"Write dropped: {e}")
            await asyncio.sleep(SEND_INTERVAL)  # pace to real drain rate

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def queue(self, channel: str, payload: bytes):
        """Thread-safe: schedule the latest payload for a channel to be sent by
        the writer. Newer values overwrite older un-sent ones (coalescing)."""
        self.loop.call_soon_threadsafe(self._set_pending, channel, payload)

    def _set_pending(self, channel, payload):
        if self.frozen:                      # the PC is shutting down: nothing may relight it
            return
        if self._alert_on:                   # hold it: sent once the alert finishes
            self._held[channel] = payload
            return
        self._pending[channel] = payload
        if self._evt is not None:
            self._evt.set()

    def _set_connected(self, state: bool):
        self.conn_cb(state)

    # ---- async operations ----
    async def _scan_once(self, timeout=8.0):
        devices = await BleakScanner.discover(timeout=timeout)
        for d in devices:
            if is_strip_name(d.name):
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

    async def _scan_all(self, timeout=8.0, everything=False):
        """Every strip in range as [(name, address)]. Read-only: doesn't touch the
        current link (a connected strip stops advertising, so it won't be listed).
        everything=True also lists other named Bluetooth devices, for strips whose
        advertised name isn't recognised: known strips first, then strongest signal
        (usually the one on your desk), capped so the list stays usable."""
        if not everything:
            devices = await BleakScanner.discover(timeout=timeout)
            return [(d.name, d.address) for d in devices if is_strip_name(d.name)]
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        rows = []
        for dev, adv in found.values():
            name = adv.local_name or dev.name
            if name:                          # nameless devices can't be told apart
                rssi = adv.rssi if adv.rssi is not None else -127
                rows.append((not is_strip_name(name), -rssi, name, dev.address))
        rows.sort()
        return [(name, addr) for _, _, name, addr in rows[:20]]

    async def _select(self, addr):
        """Switch to a user-picked strip: remember it, release any other strip,
        then connect through the normal path (lock, fallbacks, watchdog)."""
        self._want_connected = True
        if addr == self.address and self._connected():
            self.status_cb("Already connected")
            return
        async with self._conn_lock:
            self.address = addr
            try:
                with open(ADDR_FILE, "w") as f:
                    f.write(addr)
            except OSError:
                pass
            await self._drop_client()
            self._set_connected(False)
        await self._connect(fx=True)

    async def _alert(self, kind, brightness, max_s=CALL_MAX_S, color=None):
        """Play an alert over whatever is running, then put the strip back as it was:
        replay the latest frame of each kind in send order (so off stays off, an effect
        resumes), then any frames that sync/audio/effects queued meanwhile.
        kinds: "notify" / "flash" (pulses; flash takes a color), "call" (green swell until
        cancelled), "onair" (steady red while onair_on stays True)."""
        if kind in ("notify", "flash") and self._alert_on:
            return                           # something is already showing
        if kind == "call" and self._alert_on and self._alert_kind == "onair":
            return                           # you're on air: a ringing call doesn't override it
        if kind == "onair" and self._alert_on and self._alert_kind == "onair":
            return
        if not self._connected():
            return                           # never start a connect just to flash
        async with self._alert_lock:         # a call waits for a notify to finish
            if kind == "onair" and not self.onair_on:
                return                       # it ended while we waited: don't blip the strip
            self._alert_on, self._alert_kind = True, kind
            if kind != "onair":
                self.alert_cancel = False
            s = max(brightness, 50) / 100.0  # visible even at a dim brightness
            step = 0.5 if kind == "onair" else SEND_INTERVAL   # steady red needs few writes
            try:
                await self._write(cmd_on(), track=False)
                t = 0.0
                while t < max_s and (self.onair_on if kind == "onair" else not self.alert_cancel):
                    rgb = alert_frame(kind, t, color, self.calm)
                    if rgb is None:
                        break
                    await self._write(cmd_color(*(int(c * s) for c in rgb)), track=False)
                    await asyncio.sleep(step)
                    t += step
            finally:
                for payload in list(self._last.values()):
                    await self._write(payload, track=False)
                self._alert_on, self._alert_kind = False, None
                held, self._held = self._held, {}
                for channel, payload in held.items():
                    self._set_pending(channel, payload)

    def last_color(self):
        """(r, g, b) of the last color frame sent (before color balance), or None."""
        p = self._last.get(b"\x05")
        return (p[4], p[5], p[6]) if p and len(p) == 9 and p[3] == 0x03 else None

    def _calibrated(self, payload):
        """Apply the strip's color balance to a color frame (others pass through)."""
        if len(payload) == 9 and payload[2] == 0x05 and payload[3] == 0x03 \
                and self.balance != (1.0, 1.0, 1.0):
            r, g, b = apply_balance(tuple(payload[4:7]), self.balance)
            return payload[:4] + bytes((r, g, b)) + payload[7:]
        return payload

    async def _open(self, target):
        """Connect to a BLEDevice or a raw address string. Returns True on success.
        Connecting by raw address recovers the common 'strip left in a ghost
        connection so it isn't advertising' case — Windows can still reach a device
        it has seen before without a fresh advertisement."""
        if not target:
            return False
        self.status_cb(f"Connecting to {self.address}...")
        await self._drop_client()            # never leave a stale client holding the strip
        client = None
        try:
            client = BleakClient(target, timeout=20.0,
                                 disconnected_callback=self._on_disconnect)
            await client.connect()
            wu = await pick_write_char(client)
            if wu is None:
                # Distinguish a half-open connection (GATT not enumerated — happens
                # when reconnecting to a still-settling strip) from a genuinely
                # unsupported model. Only the latter has real characteristics but
                # none writable; the former just needs a retry.
                n_chars = sum(1 for s in client.services for _ in s.characteristics)
                await client.disconnect()
                self._set_connected(False)
                if n_chars > 0:
                    self._unsupported = True   # real GATT, nothing writable
                return False
            self.client = client
            self.write_uuid = wu
            self.status_cb(f"Connected  ({self.address})")
            self._set_connected(True)
            return True
        except Exception as e:
            if client is not None:           # half-open attempt: release it
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self.status_cb(f"Connect failed: {e}")
            self._set_connected(False)
            return False

    async def _drop_client(self):
        """Disconnect and forget the current client (if any), ignoring errors."""
        old, self.client, self.write_uuid = self.client, None, None
        if old is not None:
            try:
                await old.disconnect()
            except Exception:
                pass

    async def _play_connect_fx(self, color, brightness):
        """Damped brightness ripple on `color`, settling to color*brightness."""
        r, g, b = color
        await self._write(cmd_on())
        n = 20   # more frames so the multiple bounces render smoothly
        for i in range(n):
            lvl = ripple_envelope(i / (n - 1))
            await self._write(cmd_color(int(r * lvl), int(g * lvl), int(b * lvl)))
            await asyncio.sleep(SEND_INTERVAL)
        s = brightness / 100.0
        await self._write(cmd_color(int(r * s), int(g * s), int(b * s)))

    async def _connect(self, fx=False):
        self._want_connected = True
        async with self._conn_lock:
            if self._connected():
                if fx:                       # only report it for a user-pressed Connect
                    self.status_cb("Already connected")
                self._set_connected(True)
                return
            fresh = await self._connect_locked()
        # ripple runs outside the lock: its writes may need to reconnect, and the
        # lock isn't re-entrant. Only on a user/launch connect (fx=True) and only
        # when nothing else owns the color channel (provider returns None).
        if fresh and fx and self.fx_provider:
            params = self.fx_provider()
            if params:
                await self._play_connect_fx(*params)

    async def _connect_locked(self):
        """Try every connect strategy. Returns True once connected."""
        from_cache = False
        self._unsupported = False   # set True only if we see real GATT with no writable char
        if not self.address:
            if os.path.exists(ADDR_FILE):
                self.address = open(ADDR_FILE).read().strip()
                from_cache = bool(self.address)
            if not self.address:
                await self._scan()
            if not self.address:
                return False

        # 1) fast path: connect straight to the known address, no scan. Windows
        #    can reach a device it has seen before without a fresh advertisement,
        #    and the strip stops advertising while connected anyway, so scanning
        #    first just wastes ~12s and can miss it.
        self.status_cb(f"Connecting to {self.address}...")
        if await self._open(self.address):
            return True

        # 2) raw connect failed — the link may be stale. Settle, then scan for a
        #    fresh advertisement and use that BLEDevice (falls back to raw again).
        await asyncio.sleep(1.5)
        device = await BleakScanner.find_device_by_address(self.address, timeout=8.0)
        if await self._open(device or self.address):
            return True

        # 3) a cached/shared address may just be wrong for this machine — forget it
        #    and scan fresh for whatever strip is actually here.
        if from_cache:
            self._forget_address()
            addr = await self._scan()
            if addr and await self._open(addr):
                return True

        if self._unsupported:
            self.status_cb("Connected, but this strip model isn't supported yet "
                           "(no writable characteristic).")
        else:
            self.status_cb("Couldn't connect — power-cycle the strip and close the phone app.")
        self._set_connected(False)
        return False

    def _forget_address(self):
        self.address = None
        try:
            os.remove(ADDR_FILE)
        except OSError:
            pass

    def _on_disconnect(self, client):
        # called by bleak; ignore stale/probe clients so they can't flip the UI
        if client is not self.client:
            return
        self._set_connected(False)
        self.status_cb("Device disconnected" if not self._want_connected
                       else "Link lost — reconnecting...")  # watchdog takes it from here

    async def _disconnect(self):
        self._want_connected = False         # user asked: stop the watchdog reconnecting
        async with self._conn_lock:
            await self._drop_client()
        self._set_connected(False)
        self.status_cb("Disconnected")

    async def _shutdown_off(self, quick=False):
        """The PC is shutting down (or signing out): stop everything that writes to the
        strip, then switch it off. A write-without-response can be dropped and has to wait
        for the radio's next slot, and we won't get another chance, so it goes out three
        times and then we give the radio half a second to actually transmit before Windows
        is told it may continue. quick=True is the last-chance resend (two writes, no wait).
        Clearing `frozen` (shutdown cancelled) lets writes through again."""
        self.frozen = True
        self.alert_cancel, self.onair_on = True, False
        self._pending.clear()
        if not self._connected():
            return False
        for _ in range(2 if quick else 3):
            await self._write(cmd_off(), track=False, force=True)
            await asyncio.sleep(0.05 if quick else 0.15)
        if not quick:
            await asyncio.sleep(0.5)
        return True

    async def _write(self, payload, track=True, force=False):
        if self.frozen and not force:
            return                           # shutting down: only the off command goes out
        if not self._connected():
            if not self._want_connected:
                return                       # user disconnected: don't silently reconnect
            await self._connect()
        if self._connected():
            try:
                await self.client.write_gatt_char(self.write_uuid, self._calibrated(payload),
                                                  response=False)
                if track:                    # remember the strip's state for alert restore
                    kind = payload[2:3]      # frame type: power / color / effect / speed
                    self._last.pop(kind, None)
                    self._last[kind] = payload
            except Exception as e:
                self.status_cb(f"Write failed: {e} — reconnecting...")
                await self._drop_client()    # release it so the strip is free to reconnect
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
        self.extract_mode = "Dominant"      # "Dominant" (vivid) | "Average"
        self.dark_mode = "Dim"              # "Dim" | "Glow" | "Keep"
        self.sat = SYNC_STYLES["Balanced"][0]  # live saturation factor (Style sets it)
        self.alpha = 0.30                   # smoothing: higher = snappier
        self.area = "Whole screen"          # which part of the screen to sample
        self.crop_bars = True               # ignore letterbox / pillarbox black bars
        self.calm = False                   # "Reduce flashing": cap how fast it can react
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
        """arr = HxWx4 BGRA uint8. Returns (r, g, b, level): color plus a 0..1
        brightness multiplier the caller applies (used by the 'Dim' dark mode)."""
        _sat0, gamma, center = SYNC_STYLES.get(self.style, SYNC_STYLES["Balanced"])
        sat = self.sat                      # live-tunable, overrides the style's sat
        region = crop_black_bars(arr) if self.crop_bars else arr
        h, w = region.shape[:2]
        if center:
            # sample the central 60% for a punchier dominant color
            y0, y1 = int(h * 0.2), int(h * 0.8)
            x0, x1 = int(w * 0.2), int(w * 0.8)
            region = region[y0:y1, x0:x1]
        # subsample for speed, then keep only the chosen part of the screen; BGR float
        sub = select_area(region[::8, ::8, :3], self.area).astype(np.float32)
        b_ch, g_ch, r_ch = sub[..., 0], sub[..., 1], sub[..., 2]

        if self.extract_mode == "Dominant":
            # saturation-weighted mean: colorful pixels dominate, grays are ignored,
            # so the scene's subject color pops instead of averaging to mud.
            mx = np.maximum(np.maximum(r_ch, g_ch), b_ch)
            mn = np.minimum(np.minimum(r_ch, g_ch), b_ch)
            s = (mx - mn) / (mx + 1e-3)     # per-pixel saturation 0..1
            wgt = s * s + 0.05              # +floor so an all-gray frame still resolves
            wsum = wgt.sum()
            r = (r_ch * wgt).sum() / wsum
            g = (g_ch * wgt).sum() / wsum
            b = (b_ch * wgt).sum() / wsum
        else:                               # "Average" — flat mean
            r, g, b = r_ch.mean(), g_ch.mean(), b_ch.mean()
        rgb = np.array([r, g, b])

        # gamma (brighten mids for Vivid)
        if gamma != 1.0:
            rgb = 255.0 * np.power(np.clip(rgb / 255.0, 0, 1), gamma)

        # saturation boost in HSV
        if sat != 1.0:
            hh, ss, vv = colorsys.rgb_to_hsv(*(np.clip(rgb, 0, 255) / 255.0))
            ss = min(1.0, ss * sat)
            rgb = np.array(colorsys.hsv_to_rgb(hh, ss, vv)) * 255.0

        luma = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        level = 1.0
        if self.dark_mode == "Dim":
            # dark scene -> dim the strip but keep the real hue. Normalize color up
            # to a visible value so the hue shows, then pull brightness via level.
            level = float(np.clip(luma / 48.0, 0.0, 1.0))
            if luma > 1.0:
                rgb = rgb * (min(200.0, luma * 4.0) / luma)  # lift hue to visible
            self._last_bright = rgb.copy()
        elif self.dark_mode == "Glow":
            # dark / DRM-black -> dim glow of the last bright color
            if luma < SYNC_BLACK_LUMA:
                rgb = self._last_bright * SYNC_GLOW_SCALE
            else:
                self._last_bright = rgb.copy()
        # "Keep" -> raw color, level stays 1.0

        # temporal smoothing (anti-strobe); "Reduce flashing" caps how snappy it can be
        alpha = min(self.alpha, 0.25) if self.calm else self.alpha
        if self._prev is None:
            self._prev = rgb
        else:
            self._prev = self._prev * (1 - alpha) + rgb * alpha
        out = np.clip(self._prev, 0, 255).astype(int)
        return int(out[0]), int(out[1]), int(out[2]), level

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
                    r, g, b, level = self._extract(arr)
                    scale = (self.get_brightness() / 100.0) * level
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


class AudioAnalyzer:
    """Pure DSP: feed mono sample blocks, get normalised bass/mid/high/level 0..1,
    beat flags and spectral centroid. Auto-gain: each band is divided by its own
    slowly-decaying recent peak, so quiet and loud music both fill the range."""
    BANDS = {"bass": (20, 250), "mid": (250, 2000), "high": (2000, 8000)}
    NOISE = 0.004            # below this a band counts as silence (no auto-gain blow-up)

    def __init__(self, rate=48000):
        self.rate = rate
        self.peak = {k: self.NOISE for k in ("bass", "mid", "high", "level")}
        self.bass_avg = 0.0
        self.since_beat = 1.0

    def feed(self, mono, dt, sensitivity=50):
        n = len(mono)
        win = np.hanning(n)
        spec = np.abs(np.fft.rfft(mono * win)) / (win.sum() / 2)   # sine amp A -> ~A
        freqs = np.fft.rfftfreq(n, 1.0 / self.rate)
        raw = {b: float(np.sqrt(np.sum(spec[(freqs >= lo) & (freqs < hi)] ** 2)))
               for b, (lo, hi) in self.BANDS.items()}
        raw["level"] = float(np.sqrt(np.mean(mono ** 2)))
        gain = 2.0 ** ((sensitivity - 50) / 25.0)                   # 0.25x .. 4x
        decay = 0.5 ** (dt / 3.0)                                   # peak half-life 3 s
        out = {}
        for k, v in raw.items():
            self.peak[k] = max(v, self.peak[k] * decay, self.NOISE)
            out[k] = min(1.0, v / self.peak[k] * gain) if v > self.NOISE else 0.0
        # beat = bass jumps well above its own ~1 s average (refractory 180 ms)
        self.since_beat += dt
        thresh = 1.65 - 0.5 * (sensitivity / 100.0)                 # 1.15 .. 1.65
        beat = (raw["bass"] > self.bass_avg * thresh and out["bass"] > 0.35
                and self.since_beat > 0.18)
        if beat:
            self.since_beat = 0.0
        a = min(1.0, dt / 1.0)
        self.bass_avg += (raw["bass"] - self.bass_avg) * a
        band = (freqs >= 60) & (freqs < 8000)
        tot = spec[band].sum()
        out["centroid"] = float((freqs[band] * spec[band]).sum() / tot) if tot > 0 else 0.0
        out["beat"] = beat
        out["silent"] = raw["level"] <= self.NOISE
        return out


class AudioReactive:
    """Captures system audio (WASAPI loopback) or a mic in its own thread, runs
    AudioAnalyzer, and drives the strip through the coalescing 'color' slot."""
    def __init__(self, ble, status_cb, color_cb, get_brightness, get_base_color, stopped_cb):
        self.ble, self.status_cb, self.color_cb = ble, status_cb, color_cb
        self.get_brightness, self.get_base_color = get_brightness, get_base_color
        self.stopped_cb = stopped_cb        # called (from the thread) when capture ends
        self.mode = "Pulse"
        self.calm = False                   # "Reduce flashing": soften beat flashes
        self._limiter = FlashLimiter(0.3)
        self.source = SYSTEM_AUDIO          # or a microphone name
        self.sensitivity = 50               # 1..100
        self.smoothing = 40                 # 0..100 -> release 0.05..1.0 s
        self.floor = 8                      # min glow %, so the strip never goes black
        self.feat = None                    # latest analysis, read by the UI meter
        self.out_rgb = ((0, 0, 0), 0.0)     # latest (rgb, level) sent, for the meter
        self.last_beat = 0.0                # perf_counter of the last beat, for the meter
        self._running = False
        self._reset()

    def _reset(self):
        self.env = 0.0
        self.flash = 0.0
        self.hue = 0.0
        self.spec_rgb = np.zeros(3)

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            return
        self._running = True
        self._reset()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._running = False

    @staticmethod
    def list_sources():
        import soundcard as sc
        return [SYSTEM_AUDIO] + [m.name for m in sc.all_microphones()]

    def render(self, f, dt):
        """features -> (rgb at full brightness, level 0..1). Pure given state."""
        tau = 0.05 + 0.95 * self.smoothing / 100.0
        rel = math.exp(-dt / tau)

        def follow(cur, x):                 # instant attack, smooth release
            return x if x > cur else cur * rel + x * (1 - rel)

        fl = self.floor / 100.0
        base = self.get_base_color()
        if self.mode == "Pulse":
            self.env = follow(self.env, f["bass"])
            return base, fl + (1 - fl) * self.env
        if self.mode == "Beat flash":
            self.flash = 1.0 if f["beat"] else self.flash * rel
            return base, fl + (1 - fl) * self.flash
        if self.mode == "Beat colors":
            if f["beat"]:
                self.hue = (self.hue + 0.381966) % 1.0      # golden step: far-apart hues
                self.flash = 1.0
            else:
                self.flash *= rel
            self.env = follow(self.env, f["level"])
            return hs_rgb(self.hue, 1.0), fl + (1 - fl) * max(self.flash, 0.4 * self.env)
        if self.mode == "Spectrum":
            tgt = np.array([f["bass"], f["mid"], f["high"]])
            self.spec_rgb = np.where(tgt > self.spec_rgb, tgt,
                                     self.spec_rgb * rel + tgt * (1 - rel))
            m = float(self.spec_rgb.max())
            rgb = tuple(int(c / m * 255) for c in self.spec_rgb) if m > 0 else base
            # bands set the hue mix; overall loudness sets brightness (a steady hiss
            # auto-gains to ~1 in its band, so the max band would pin it at full)
            self.env = follow(self.env, f["level"])
            return rgb, fl + (1 - fl) * self.env
        # "Pitch": spectral centroid 150 Hz .. 5 kHz -> hue red .. violet
        c = f["centroid"]
        if c > 0 and not f["silent"]:
            t = (math.log2(max(c, 150)) - math.log2(150)) / (math.log2(5000) - math.log2(150))
            tgt = 0.75 * min(1.0, t)
            self.hue += (tgt - self.hue) * (1 - rel)
        self.env = follow(self.env, f["level"])
        return hs_rgb(self.hue, 1.0), fl + (1 - fl) * self.env

    def _run(self):
        import warnings
        rate, block = 48000, 1024                    # ~21 ms per block, ~47 analyses/s
        try:
            import soundcard as sc
            if self.source == SYSTEM_AUDIO:
                mic = sc.get_microphone(id=str(sc.default_speaker().name),
                                        include_loopback=True)
            else:
                mic = next((m for m in sc.all_microphones() if m.name == self.source), None)
                if mic is None:
                    raise RuntimeError(f"microphone not found: {self.source}")
            an = AudioAnalyzer(rate)
            ring = np.zeros(2048, dtype=np.float32)  # FFT window = last ~43 ms
            last_ui = 0.0
            self.status_cb(f"Audio reactive ON — {self.mode}")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")      # soundcard warns on harmless gaps
                with mic.recorder(samplerate=rate, channels=1, blocksize=block) as rec:
                    while self._running:
                        data = rec.record(numframes=block)
                        mono = data.reshape(len(data), -1).mean(axis=1).astype(np.float32)
                        ring = np.concatenate([ring, mono])[-2048:]
                        dt = len(mono) / rate
                        f = an.feed(ring, dt, self.sensitivity)
                        rgb, lvl = self.render(f, dt)
                        self.feat, self.out_rgb = f, (tuple(rgb), lvl)
                        if f["beat"]:
                            self.last_beat = time.perf_counter()
                        s = self.get_brightness() / 100.0 * lvl
                        r, g, b = rgb
                        frame = (int(r * s), int(g * s), int(b * s))
                        if self.calm:
                            frame = self._limiter.step(frame, dt)
                        else:
                            self._limiter.reset()
                        self.ble.queue("color", cmd_color(*frame))
                        now = time.perf_counter()
                        if now - last_ui > SEND_INTERVAL:   # UI tint at the strip's rate
                            last_ui = now
                            self.color_cb(tuple(rgb))
        except Exception as e:
            self.status_cb(f"Audio stopped: {e}")
        finally:
            self._running = False
            self.feat = None
            self.stopped_cb()


class NotificationWatcher:
    """Polls Windows' notification center about once a second in its own thread.
    on_new(is_call) fires for each new toast; on_call_end() when every ringing call
    toast is gone (answered/declined). Toasts already there at start are ignored."""
    def __init__(self, on_new, on_call_end, status_cb):
        self.on_new, self.on_call_end, self.status_cb = on_new, on_call_end, status_cb
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            self._stop.clear()               # stop() was asked but it hasn't exited yet
            return
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._poll()), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _text(note):
        """App name + visible text lines of a toast (empty on anything odd)."""
        parts = []
        try:
            parts.append(note.app_info.display_info.display_name)
        except Exception:
            pass
        try:
            b = note.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
            parts += [t.text for t in b.get_text_elements()] if b else []
        except Exception:
            pass
        return " ".join(p for p in parts if p)

    async def _poll(self):
        try:
            listener = UserNotificationListener.current
            status = await listener.request_access_async()
        except Exception as e:
            self.status_cb(f"Notification alerts unavailable: {e}")
            return
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            self.status_cb("Windows blocked notification access — allow it in "
                           "Settings › Privacy & security › Notifications")
            return
        seen, calls = None, set()
        while not self._stop.is_set():
            try:
                notes = {n.id: n for n in
                         await listener.get_notifications_async(NotificationKinds.TOAST)}
            except Exception:
                await asyncio.sleep(5.0)     # notification service hiccup: try again
                continue
            if seen is not None:             # first pass only learns what's already there
                for nid in notes.keys() - seen:
                    call = is_call(self._text(notes[nid]))
                    if call:
                        calls.add(nid)
                    self.on_new(call)
            if calls and not calls & notes.keys():
                self.on_call_end()
            calls &= notes.keys()
            seen = set(notes)
            await asyncio.sleep(1.0)


SHUTDOWN_LOG = os.path.join(APP_DIR, "shutdown.log")


def shutdown_log(msg, path=None):
    """One timestamped line in %APPDATA%\\RGBZap\\shutdown.log (kept small). When Windows ends
    the session RGBZap is killed moments later, so this file is the only way to see what
    happened: whether it was told, whether the strip was connected, whether off went out."""
    path = path or SHUTDOWN_LOG
    try:
        if os.path.exists(path) and os.path.getsize(path) > 20000:
            with open(path, encoding="utf-8", errors="replace") as f:
                keep = f.readlines()[-100:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(keep)
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


class Tooltip:
    """A hover bubble: appears after a short pause beside the widget, hides when the
    pointer leaves or you click."""
    def __init__(self, widget, text, wrap=270):
        self.widget, self.text, self.wrap = widget, text, wrap
        self._tip, self._job = None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._hide()
        self._job = self.widget.after(250, self._show)

    def _hide(self, _e=None):
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _show(self):
        self._job = None
        w = self.widget
        tip = tk.Toplevel(w)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        border = tk.Frame(tip, bg=LINE, padx=1, pady=1)
        border.pack()
        tk.Label(border, text=self.text, justify="left", wraplength=self.wrap, bg=PANEL2,
                 fg=TEXT, font=FONT_BODY, padx=10, pady=8).pack()
        tip.update_idletasks()
        x = w.winfo_rootx() + 12
        x = max(8, min(x, w.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
        y = w.winfo_rooty() + w.winfo_height() + 6
        if y + tip.winfo_reqheight() > w.winfo_screenheight() - 8:   # no room below: go above
            y = w.winfo_rooty() - tip.winfo_reqheight() - 6
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip


class App:
    def __init__(self, root, lock=None):
        self.root = root
        self._wheel_targets = {}            # tab path -> canvas the mouse wheel should scroll
        root.bind_all("<MouseWheel>", self._on_wheel)
        self.ble = BleWorker(self.set_status, self.set_connected)
        cfg = load_settings()
        self._saved = cfg                   # last state written to disk
        self.base_color = tuple(cfg["color"])  # current color before brightness scaling
        self.brightness = cfg["brightness"]    # percent
        self.mode = cfg["mode"]             # what the strip shows: "color" | "effect"
        self.custom = [tuple(c) for c in cfg["custom"]]   # saved picker colors
        self.scenes = {n: dict(sc) for n, sc in cfg["scenes"].items()}  # name -> snapshot
        self._picker = None                 # open picker window, if any
        self.soft = None                    # running software effect, if any
        self._soft_job = None              # its pending root.after id

        self.sync = ScreenSync(self.ble, self.set_status,
                               self.set_sync_swatch, lambda: self.brightness)
        self.sync.style = cfg["style"]
        self.sync.extract_mode = cfg["extract"]
        self.sync.dark_mode = cfg["dark"]

        self.audio = AudioReactive(self.ble, self.set_status, self.set_sync_swatch,
                                   lambda: self.brightness, lambda: self.base_color,
                                   lambda: self.root.after(0, self._audio_stopped))
        self.audio.mode = cfg["audio_mode"]
        self.audio.source = cfg["audio_source"]
        self.audio.sensitivity = cfg["audio_sens"]
        self.audio.smoothing = cfg["audio_smooth"]
        self.audio.floor = cfg["audio_floor"]

        # plain bools (not Tk vars): the watcher thread reads them
        self.notify_flash, self.notify_call = cfg["notify_flash"], cfg["notify_call"]
        self.update_check = cfg["update_check"]
        self.update_info = None             # (version, page url) once a newer release is seen
        self._update_job = None             # pending root.after id of the next check
        self._update_busy = False
        self.notifier = NotificationWatcher(
            self._on_notification, lambda: setattr(self.ble, "alert_cancel", True),
            self.set_status)

        # lights off with the PC: Windows' "session ending" messages arrive on this watcher
        self.off_on_shutdown = cfg["off_on_shutdown"]
        self._shutdown_prev = True          # were the lights on when the shutdown began?
        self.session = session_mod.SessionWatcher(self._on_session_query, self._on_session_end)

        # comfort: fades, reduce flashing, strip color balance
        self.fade_name = cfg["fade"]
        self.reduce_flash = cfg["reduce_flash"]
        self.balance_pct = [cfg["bal_r"], cfg["bal_g"], cfg["bal_b"]]
        self.light_on = True                # what we last told the strip (for the on/off hotkey)
        self._fade_job = None               # pending root.after id of the running fade
        self._limiter = FlashLimiter()      # soft effects, when reducing flashing
        self._fx_limiters = {}              # one per preview tile, same reason
        self.sync.area, self.sync.crop_bars = cfg["area"], cfg["crop_bars"]
        self._apply_comfort()
        self.ui = SimpleNamespace(           # what rgbzap_tools needs to draw in our style
            INK=INK, PANEL=PANEL, PANEL2=PANEL2, LINE=LINE, TEXT=TEXT, MUTE=MUTE, GOOD=GOOD,
            BAD=BAD, SYNC_BLUE=SYNC_BLUE, FONT_BODY=FONT_BODY, FONT_MONO=FONT_MONO,
            FONT_EYE=FONT_EYE, FONT_BTN=FONT_BTN, HOTKEY_ACTIONS=HOTKEY_ACTIONS,
            ONAIR_MAX_S=ONAIR_MAX_S, APP_VERSION=APP_VERSION)

        root.title("RGBZap — LED Console")
        root.configure(bg=INK, padx=14, pady=14)
        root.resizable(False, True)   # fixed width, resizable height
        try:                          # title bar + taskbar icon
            root.iconbitmap(default=os.path.join(ASSETS, "rgbzap.ico"))
        except tk.TclError:
            pass
        self._init_style(root)

        # === header: app mark + live connection pill =====================
        head = tk.Frame(root, bg=INK)
        head.pack(fill="x")
        try:
            self._logo = tk.PhotoImage(file=os.path.join(ASSETS, "header.png"))
            tk.Label(head, image=self._logo, bg=INK, bd=0).pack(side="left")
        except tk.TclError:           # assets missing: plain wordmark
            tk.Label(head, text="RGBZAP", font=FONT_H1, fg=TEXT, bg=INK).pack(side="left")
        tk.Label(head, text="LED CONSOLE", font=FONT_EYE, fg=MUTE, bg=INK
                 ).pack(side="left", padx=(10, 0), pady=(20, 0))
        pill = tk.Frame(head, bg=INK)
        pill.pack(side="right")
        self.link_lbl = tk.Label(pill, text="Offline", font=FONT_MONO, fg=BAD, bg=INK)
        self.link_lbl.pack(side="right")
        self.dot = tk.Canvas(pill, width=14, height=14, bg=INK, highlightthickness=0)
        self.dot.pack(side="right", padx=(0, 6))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=BAD, outline="")

        # === aura bar: the signature — glows the strip's current color ====
        self.aura = tk.Frame(root, bg="#ffffff", height=5)
        self.aura.pack(fill="x", pady=(12, 12))

        # === status readout (built early so worker callbacks are safe) ====
        self.status = tk.Label(root, text="Ready", anchor="w", fg=MUTE, bg=PANEL2,
                               font=FONT_MONO, padx=10, pady=6)
        self.status.pack(side="bottom", fill="x", pady=(12, 0))

        # === tabbed deck ==================================================
        self.tabbar = tk.Canvas(root, height=36, bg=INK, highlightthickness=0, bd=0)
        self.tabbar.pack(fill="x")
        # hidden until a newer release is found; packed above the tabs by _set_update
        self.update_banner = tk.Label(root, font=FONT_BODY, fg="white", bg=SYNC_BLUE,
                                      cursor="hand2", pady=6)
        self.update_banner.bind("<Button-1>", lambda _e: self.open_update())
        nb = ttk.Notebook(root, style="Deck.TNotebook")
        nb.pack(fill="both", expand=True)
        self.nb = nb
        light = self._tab(nb, "LIGHT")
        fx = self.fx_tab = self._tab(nb, "EFFECTS")
        screen = self._tab(nb, "SCREEN")
        audio = self._tab(nb, "AUDIO")
        tools = self._tab(nb, "TOOLS", scroll=True)
        system = self._tab(nb, "SYSTEM")
        self._build_tabbar()

        # ---- LIGHT tab ----
        # strip picker: pick a strip to connect + save it; ↻ scans; switch = link on/off
        self.strips = [list(s) for s in cfg["strips"]]   # saved [name, address]
        self._found = []                                 # last scan, not yet saved
        self._found_names = {}                           # address -> name seen in a scan
        self._choices = []                               # (name, address) per combo row
        self._eyebrow(light, "Light strip").pack(fill="x")
        lr = tk.Frame(light, bg=PANEL)
        lr.pack(fill="x", pady=(4, 0))
        self.strip_var = tk.StringVar()
        self.strip_cb = self._combo(lr, self.strip_var, [], 20, self.on_strip_pick)
        self.strip_cb.pack(side="left", fill="x", expand=True)
        self.link_sw = tk.Canvas(lr, width=44, height=24, bg=PANEL, highlightthickness=0,
                                 cursor="hand2")
        self.link_sw.pack(side="right", padx=(8, 0))
        self.link_sw.bind("<Button-1>", lambda _e: self.on_link_toggle())
        self.scan_btn = self._flat(lr, "↻", self.on_strip_scan, pady=3)
        self.scan_btn.config(padx=8, font=("Segoe UI Symbol", 11))
        self.scan_btn.pack(side="right", padx=(6, 0))
        self.all_devs_var = tk.BooleanVar(value=False)
        tk.Checkbutton(light, text="Show all Bluetooth devices (strip not listed?)",
                       variable=self.all_devs_var, anchor="w", font=("Segoe UI", 9),
                       fg=MUTE, bg=PANEL, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=TEXT, highlightthickness=0, bd=0
                       ).pack(fill="x", pady=(2, 14))
        self._refresh_strips(self._saved_address())
        self._draw_link(True)                 # launch auto-connects right away

        self._eyebrow(light, "Current color").pack(fill="x")
        self.swatch = tk.Label(light, bg="#ffffff", height=3, bd=0)
        self.swatch.pack(fill="x", pady=(4, 6))
        hexrow = tk.Frame(light, bg=PANEL)
        hexrow.pack(fill="x")
        self.hex_val = tk.Label(hexrow, text="#FFFFFF", font=FONT_HEX, fg=TEXT, bg=PANEL)
        self.hex_val.pack(side="left")
        self._flat(hexrow, "Pick color", self.on_pick).pack(side="right")

        pw = tk.Frame(light, bg=PANEL)
        pw.pack(fill="x", pady=(12, 0))
        self._flat(pw, "Turn on", self.on_on, bg=GOOD, fg=INK,
                   hover="#5FE3B4").pack(side="left", expand=True, fill="x", padx=(0, 5))
        self._flat(pw, "Turn off", self.on_off, bg=PANEL2, fg=TEXT,
                   hover=LINE).pack(side="left", expand=True, fill="x")

        # scenes: pick one to apply it; Save stores the current look under a name
        self._eyebrow(light, "Scenes").pack(fill="x", pady=(14, 2))
        sr = tk.Frame(light, bg=PANEL)
        sr.pack(fill="x")
        self.scene_var = tk.StringVar()
        self.scene_cb = self._combo(sr, self.scene_var, list(self.scenes), 20,
                                    lambda _e: self.apply_scene(self.scene_var.get()))
        self.scene_cb.pack(side="left", fill="x", expand=True)
        self._flat(sr, "Delete", self.on_scene_delete).pack(side="right", padx=(6, 0))
        self._flat(sr, "Save", self.on_scene_save).pack(side="right", padx=(6, 0))

        # saved picker colors sit above the presets; right-click a chip to remove it
        self._eyebrow(light, "Custom").pack(fill="x", pady=(14, 6))
        self.custom_row = tk.Frame(light, bg=PANEL, height=22)
        self.custom_row.pack(fill="x")
        self._render_custom()

        self._eyebrow(light, "Presets").pack(fill="x", pady=(14, 6))
        presets = tk.Frame(light, bg=PANEL)
        presets.pack(fill="x")
        # small nameless color chips — one tight row, the color is the label
        for _label, rgb in PRESETS:
            self._chip(presets, rgb)

        self._eyebrow(light, "Brightness").pack(fill="x", pady=(14, 2))
        self.bright = self._gauge(light, 1, 100, self.on_brightness, cfg["brightness"], "%")

        # ---- EFFECTS tab: every effect as a live preview tile; click one to apply ----
        self.effect_var = tk.StringVar(value=cfg["effect"])
        self._eyebrow(fx, "Speed").pack(fill="x", pady=(0, 2))
        self.speed = self._gauge(fx, 1, 100, self.on_speed, cfg["speed"], "%")
        fr = tk.Frame(fx, bg=PANEL)
        fr.pack(fill="both", expand=True, pady=(12, 0))
        self.fx_canvas = tk.Canvas(fr, bg=PANEL, highlightthickness=0, bd=0, height=360)
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.fx_canvas.yview,
                           style="RGBZap.Vertical.TScrollbar")
        self.fx_canvas.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(6, 0))
        self.fx_canvas.pack(side="left", fill="both", expand=True)
        self.fx_canvas.bind("<Configure>", self._fx_layout)
        self._wheel_targets[str(self.fx_tab)] = self.fx_canvas   # shared mouse-wheel handler
        self._fx_tiles = {}                  # effect name (None = no effect) -> canvas items
        self._fx_w = 0
        self._fx_shown = {}                  # tile -> last drawn color (skip no-op redraws)
        self._fx_active = False              # last highlighted tile (False = not drawn yet)
        self._fx_t = 0.0                     # preview clock for the strip's built-ins
        self._fx_prev = {n: make() for n, make in SOFT_EFFECTS.items()}  # own instances
        self.root.after(80, self._fx_tick)

        # ---- SCREEN tab ----
        try:
            self._monitors = ScreenSync.list_monitors()   # [(index, label), ...]
        except Exception as e:
            self._monitors = []
            self.set_status(f"Couldn't detect displays: {e}")

        self._eyebrow(screen, "Display").pack(fill="x")
        self.display_var = tk.StringVar()
        labels = [lbl for _, lbl in self._monitors] or ["No display found"]
        self.display_cb = self._combo(screen, self.display_var, labels, 24,
                                      self.on_display_change)
        # restore the saved display by label; fall back to the first if it's gone
        self.display_cb.current(labels.index(cfg["display"])
                                if cfg["display"] in labels else 0)
        self.display_cb.pack(fill="x", pady=(4, 0))

        grid = tk.Frame(screen, bg=PANEL)
        grid.pack(fill="x", pady=(12, 0))
        self.style_var = tk.StringVar(value=cfg["style"])
        self.extract_var = tk.StringVar(value=cfg["extract"])
        self.dark_var = tk.StringVar(value=cfg["dark"])
        self._field(grid, 0, "Style", self.style_var, list(SYNC_STYLES),
                    self.on_style_change)
        self._field(grid, 1, "Extract", self.extract_var, ["Dominant", "Average"],
                    lambda e: setattr(self.sync, "extract_mode", self.extract_var.get()))
        self._field(grid, 2, "Dark scenes", self.dark_var, ["Dim", "Glow", "Keep"],
                    lambda e: setattr(self.sync, "dark_mode", self.dark_var.get()),
                    help="What the strip does when the picture goes dark. Dim keeps the "
                         "scene's color but lowers the light; Glow holds a faint glow of "
                         "the last bright color; Keep shows the raw color.")
        self.area_var = tk.StringVar(value=cfg["area"])
        self._field(grid, 3, "Sample from", self.area_var, list(AREAS), self.on_area_change,
                    help="Which part of the screen the strip copies. Whole screen is the "
                         "classic Ambilight look. Edges ignores the middle and matches the "
                         "border, which suits bias lighting behind a monitor. Left, Right, "
                         "Top or Bottom follow just that side.")
        grid.columnconfigure(1, weight=1)
        self.bars_var = tk.BooleanVar(value=cfg["crop_bars"])
        bars = tk.Frame(screen, bg=PANEL)
        bars.pack(fill="x", pady=(4, 0))
        tk.Checkbutton(bars, text="Ignore black bars", variable=self.bars_var,
                       command=self.on_bars_toggle, anchor="w", font=FONT_BODY, fg=TEXT,
                       bg=PANEL, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=TEXT, highlightthickness=0, bd=0).pack(side="left")
        self._help(bars, "Widescreen films have black bars above and below the picture. "
                         "With this on, RGBZap skips them, so the strip matches the picture "
                         "instead of looking dark. It never crops more than 15% from a side."
                   ).pack(side="left", padx=(6, 0))

        self._eyebrow(screen, "Saturation").pack(fill="x", pady=(14, 2))
        self.sat_scale = self._gauge(screen, 100, 250, self.on_sat, cfg["sat"], "%")
        self.on_sat(cfg["sat"])
        self._eyebrow(screen, "Smoothing").pack(fill="x", pady=(10, 2))
        self.smooth = self._gauge(screen, 5, 100, self.on_smooth, cfg["smooth"], "%")
        self.on_smooth(cfg["smooth"])

        self.sync_btn = self._flat(screen, "▶  Start screen sync", self.on_toggle_sync,
                                   bg=SYNC_BLUE, fg="white", hover="#5A97F8", pady=11,
                                   hoverable=False)
        self.sync_btn.pack(fill="x", pady=(16, 0))
        self.on_display_change()

        # ---- AUDIO tab ----
        try:
            sources = AudioReactive.list_sources()
        except Exception as e:                  # soundcard missing / no audio devices
            sources = [SYSTEM_AUDIO]
            self.set_status(f"Audio devices unavailable: {e}")
        self._eyebrow(audio, "Listen to").pack(fill="x")
        self.audio_src_var = tk.StringVar(
            value=cfg["audio_source"] if cfg["audio_source"] in sources else SYSTEM_AUDIO)
        self.audio.source = self.audio_src_var.get()
        self._combo(audio, self.audio_src_var, sources, 24,
                    lambda _e: self._audio_set("source", self.audio_src_var.get())
                    ).pack(fill="x", pady=(4, 0))

        self._eyebrow(audio, "Mode").pack(fill="x", pady=(12, 2))
        self.audio_mode_var = tk.StringVar(value=cfg["audio_mode"])
        self._combo(audio, self.audio_mode_var, list(AUDIO_MODES), 24,
                    self.on_audio_mode).pack(fill="x")
        self.audio_desc = tk.Label(audio, text=AUDIO_MODES[cfg["audio_mode"]],
                                   font=FONT_BODY, fg=MUTE, bg=PANEL, anchor="w")
        self.audio_desc.pack(fill="x", pady=(4, 0))

        # live meter: bass / mid / high bars, a beat light, and the output color
        self._eyebrow(audio, "Live").pack(fill="x", pady=(12, 4))
        self.meter = tk.Canvas(audio, height=74, bg=PANEL2, highlightthickness=0, bd=0)
        self.meter.pack(fill="x")
        self._meter_bars = {}
        for i, (key, label) in enumerate((("bass", "BASS"), ("mid", "MID"), ("high", "HIGH"))):
            y = 12 + i * 20
            self.meter.create_text(10, y + 5, text=label, anchor="w", fill=MUTE, font=FONT_EYE)
            self.meter.create_rectangle(52, y, 52, y + 10, fill=PANEL, outline="", tags="track")
            self._meter_bars[key] = self.meter.create_rectangle(52, y, 52, y + 10,
                                                                fill=TEXT, outline="")
        self._meter_beat = self.meter.create_oval(0, 0, 0, 0, fill=PANEL, outline=LINE)
        self._meter_out = self.meter.create_rectangle(0, 0, 0, 0, fill=PANEL, outline=LINE)
        self.meter.bind("<Configure>", lambda _e: self._draw_meter())

        self._eyebrow(audio, "Sensitivity").pack(fill="x", pady=(12, 2))
        self._gauge(audio, 1, 100, lambda v: self._audio_set("sensitivity", int(float(v))),
                    cfg["audio_sens"], "%")
        self._eyebrow(audio, "Smoothing").pack(fill="x", pady=(8, 2))
        self._gauge(audio, 0, 100, lambda v: self._audio_set("smoothing", int(float(v))),
                    cfg["audio_smooth"], "%")
        self._eyebrow(audio, "Min glow").pack(fill="x", pady=(8, 2))
        self._gauge(audio, 0, 50, lambda v: self._audio_set("floor", int(float(v))),
                    cfg["audio_floor"], "%")

        self.audio_btn = self._flat(audio, "▶  Start audio reactive", self.on_toggle_audio,
                                    bg=AUDIO_PINK, fg="white", hover="#E86AB1", pady=11,
                                    hoverable=False)
        self.audio_btn.pack(fill="x", pady=(16, 0))

        # ---- SYSTEM tab ----
        self._eyebrow(system, "Startup").pack(fill="x", pady=(0, 4))
        migrate_startup()
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        cb = self._check(system, "Open automatically when Windows starts",
                         self.startup_var, self.on_startup_toggle)
        if winreg is None:
            cb.config(state="disabled")  # non-Windows: not supported
        self.shutdown_var = tk.BooleanVar(value=self.off_on_shutdown)
        sdr = tk.Frame(system, bg=PANEL)
        sdr.pack(fill="x")
        tk.Checkbutton(sdr, text="Turn the lights off when this PC shuts down",
                       variable=self.shutdown_var, command=self.on_shutdown_toggle,
                       anchor="w", font=FONT_BODY, fg=TEXT, bg=PANEL, selectcolor=PANEL2,
                       activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
                       bd=0).pack(side="left")
        self._help(sdr, "Switches the strip off when you shut down, restart or sign out of "
                        "Windows, so it doesn't stay lit after the PC is off. RGBZap sends "
                        "the off command just before Windows starts closing programs. If "
                        "the shutdown gets cancelled, the lights come back as they were."
                   ).pack(side="left", padx=(6, 0))

        # notification alerts: the strip flashes / pulses, then goes back to what it showed
        self._eyebrow(system, "Notifications").pack(fill="x", pady=(16, 4))
        self.flash_var = tk.BooleanVar(value=self.notify_flash)
        self.call_var = tk.BooleanVar(value=self.notify_call)
        checks = [self._check(system, "Flash when a notification arrives", self.flash_var,
                              self.on_notify_toggle),
                  self._check(system, "Pulse green while a call is ringing", self.call_var,
                              self.on_notify_toggle)]
        tr = tk.Frame(system, bg=PANEL)
        tr.pack(fill="x", pady=(8, 0))
        tests = [self._flat(tr, "Test notification", lambda: self.on_alert_test("notify")),
                 self._flat(tr, "Test call", lambda: self.on_alert_test("call"))]
        tests[0].pack(side="left", expand=True, fill="x", padx=(0, 5))
        tests[1].pack(side="left", expand=True, fill="x")
        if UserNotificationListener is None:  # winrt notification packages missing
            for w in checks:
                w.config(state="disabled")
            tk.Label(system, text="Needs the winrt notification packages (requirements.txt)",
                     font=FONT_BODY, fg=MUTE, bg=PANEL, anchor="w").pack(fill="x", pady=(6, 0))
        self.on_notify_toggle()

        self._heading(system, "Comfort", pady=(16, 4))
        fr = tk.Frame(system, bg=PANEL)
        fr.pack(fill="x")
        tk.Label(fr, text="Fade between colors", font=FONT_BODY, fg=MUTE, bg=PANEL
                 ).pack(side="left")
        self.fade_var = tk.StringVar(value=self.fade_name)
        self._combo(fr, self.fade_var, list(FADES), 9, self.on_fade_change
                    ).pack(side="left", padx=(10, 0))
        self._help(fr, "How colors blend when you pick a new color, apply a scene, or "
                       "turn the lights on or off, instead of snapping. Quick is 0.3 s, "
                       "Smooth 0.7 s, Slow 1.5 s. It doesn't slow down effects, screen "
                       "sync or the brightness slider."
                   ).pack(side="left", padx=(8, 0))
        self.calm_var = tk.BooleanVar(value=self.reduce_flash)
        rf = tk.Frame(system, bg=PANEL)
        rf.pack(fill="x", pady=(6, 0))
        tk.Checkbutton(rf, text="Reduce flashing", variable=self.calm_var,
                       command=self.on_calm_toggle, anchor="w", font=FONT_BODY, fg=TEXT,
                       bg=PANEL, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=TEXT, highlightthickness=0, bd=0).pack(side="left")
        self._help(rf, "Makes the lights gentler for people sensitive to flashing. Fast "
                       "effects (Lightning, Police, Party, Neon flicker, Fireworks), "
                       "notification flashes, beat flashes and screen-sync cuts are "
                       "smoothed so nothing flashes more than a few times a second. The "
                       "strip's own Blink and Jump effects are switched off, because they "
                       "run inside the strip where RGBZap can't soften them. Also applies "
                       "to the effect previews on screen."
                   ).pack(side="left", padx=(6, 0))

        self._eyebrow(system, "Updates").pack(fill="x", pady=(16, 4))
        self.update_var = tk.BooleanVar(value=self.update_check)
        self._check(system, "Check for updates automatically", self.update_var,
                    self.on_update_toggle)

        # about footer, pinned to the bottom of the tab
        about = tk.Frame(system, bg=PANEL)
        about.pack(side="bottom", fill="x")
        tk.Frame(about, bg=LINE, height=1).pack(fill="x", pady=(0, 10))
        ver = tk.Frame(about, bg=PANEL)
        ver.pack(fill="x")
        tk.Label(ver, text=f"RGBZAP {APP_VERSION}", font=FONT_EYE, fg=MUTE, bg=PANEL
                 ).pack(side="left")
        self._link(ver, "Check for updates", cmd=lambda: self._run_update_check(manual=True)
                   ).pack(side="right")
        by = tk.Frame(about, bg=PANEL)
        by.pack(fill="x", pady=(2, 0))
        tk.Label(by, text="© 2026", font=FONT_BODY, fg=TEXT, bg=PANEL).pack(side="left")
        self._link(by, "Dilshan Rangaka", AUTHOR_URL).pack(side="left")
        tk.Label(by, text="· Free and open source (MIT)", font=FONT_BODY, fg=TEXT, bg=PANEL
                 ).pack(side="left")
        self._link(about, APP_URL.removeprefix("https://"), APP_URL).pack(anchor="w")

        # ---- TOOLS tab: hotkeys, On-Air light, OBS link, local control, color balance
        self.tools = rgbzap_tools.ToolsTab(self, tools, cfg)

        # lock a consistent width; let height be resized (min keeps it usable)
        root.update_idletasks()
        width = max(380, root.winfo_reqwidth())
        root.geometry(f"{width}x{root.winfo_reqheight()}")
        root.minsize(width, 480)
        root.maxsize(width, root.winfo_screenheight())

        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._tray = None
        self._tray_hint_shown = False
        try:
            self._start_tray()
        except Exception as e:                # tray is a convenience, never fatal
            self._tray = None
            self.set_status(f"Tray unavailable: {e}")
        if lock is not None:
            self._listen_for_second_launch(lock)
        self._paint(self.base_color)          # show the restored color
        # play the "connected" ripple on user/launch connects, unless a sync owns the strip
        self.ble.fx_provider = self._fx_params
        # auto-connect on launch, then resume the saved effect if one was running
        resume = (cfg["effect"], cfg["speed"]) if self.mode == "effect" else None
        self.ble.submit(self._launch(resume))
        self.root.after(2000, self._autosave)
        self.tools.start()                    # hotkeys, On-Air watcher, OBS link, local API
        self.session.start()                  # hear Windows shut down / restart / sign out
        shutdown_log(f"RGBZap {APP_VERSION} started; shutdown listener "
                     f"{'ready' if self.session._hwnd else 'NOT created'}; lights-off-with-PC "
                     f"{'on' if self.off_on_shutdown else 'off'}")
        if self.update_check:                 # first look a few seconds after launch
            self._schedule_update_check(5000)

    # ---- update check (notify only: the user downloads and installs) ----
    def _schedule_update_check(self, delay_ms):
        if self._update_job is not None:
            self.root.after_cancel(self._update_job)
        self._update_job = self.root.after(delay_ms, self._update_tick)

    def _update_tick(self):
        self._update_job = None
        if self.update_check:
            self._run_update_check(manual=False)

    def _run_update_check(self, manual):
        if self._update_busy:
            return
        self._update_busy = True

        def work():                           # network off the Tk thread
            try:
                result = ("ok", parse_release(fetch_latest_release()))
            except Exception:
                result = ("fail", None)
            self.root.after(0, self._update_done, result, manual)
        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, result, manual):
        self._update_busy = False
        state, info = result
        if state == "ok":
            self._set_update(info)
            if manual:
                self.set_status(f"RGBZap {info[0]} is available — click the blue bar to "
                                "download" if info else "RGBZap is up to date")
        elif manual:
            self.set_status("Couldn't check for updates — are you online?")
        if self.update_check:                 # next look: a day on success, an hour if offline
            self._schedule_update_check(UPDATE_EVERY_MS if state == "ok" else UPDATE_RETRY_MS)

    def _set_update(self, info):
        self.update_info = info
        if info:
            self.update_banner.config(text=f"↑  RGBZap {info[0]} is available — click to download")
            if not self.update_banner.winfo_ismapped():
                self.update_banner.pack(before=self.tabbar, fill="x", pady=(0, 10))
        else:
            self.update_banner.pack_forget()

    def open_update(self):
        if self.update_info:
            webbrowser.open(self.update_info[1])

    def on_update_toggle(self):
        self.update_check = self.update_var.get()
        if self.update_check:
            self._schedule_update_check(2000)
        else:
            if self._update_job is not None:
                self.root.after_cancel(self._update_job)
                self._update_job = None
            self._set_update(None)

    async def _launch(self, resume):
        await self.ble._connect(fx=True)
        if not resume:
            return
        name, speed = resume
        if name in SOFT_EFFECTS:
            self.root.after(0, self._start_soft, name)   # flame ticks on the Tk thread
        else:
            await self.ble._write(cmd_effect(EFFECTS[name]))
            await self.ble._write(cmd_effect_speed(speed))

    # ---- settings persistence ----
    def _snapshot(self):
        return {
            "color": list(self.base_color), "brightness": self.brightness,
            "mode": self.mode, "effect": self.effect_var.get(),
            "speed": int(float(self.speed.get())), "display": self.display_var.get(),
            "style": self.sync.style, "extract": self.sync.extract_mode,
            "dark": self.sync.dark_mode, "sat": round(self.sync.sat * 100),
            "smooth": round(self.sync.alpha * 100),
            "custom": [list(c) for c in self.custom],
            "strips": [list(s) for s in self.strips],
            "notify_flash": self.notify_flash, "notify_call": self.notify_call,
            "update_check": self.update_check, "off_on_shutdown": self.off_on_shutdown,
            "fade": self.fade_name, "reduce_flash": self.reduce_flash,
            "area": self.sync.area, "crop_bars": self.sync.crop_bars,
            "bal_r": self.balance_pct[0], "bal_g": self.balance_pct[1],
            "bal_b": self.balance_pct[2],
            **self.tools.snapshot(),
            "audio_mode": self.audio.mode, "audio_source": self.audio.source,
            "audio_sens": self.audio.sensitivity, "audio_smooth": self.audio.smoothing,
            "audio_floor": self.audio.floor,
            # copies, so later edits to self.scenes still differ from the saved state
            "scenes": {n: {**sc, "color": list(sc["color"])} for n, sc in self.scenes.items()},
        }

    def _save(self):
        """Write settings only if something changed since the last write."""
        cfg = self._snapshot()
        if cfg == self._saved:
            return
        try:
            save_settings(cfg)
            self._saved = cfg
        except OSError as e:
            self.set_status(f"Couldn't save settings: {e}")

    def _autosave(self):
        # ponytail: 2s poll instead of a save hook in every callback; also survives
        # Windows shutdown, which can kill the app without calling on_close.
        self._save()
        self.root.after(2000, self._autosave)

    def _fx_params(self):
        if self.sync.running or self.audio.running or self.reduce_flash:
            return None                      # don't hijack a sync; the ripple bounces: skip it
                                             # when reducing flashing
        return self.base_color, self.brightness

    # ---- theme + widget factories ----
    def _init_style(self, root):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Deck.TNotebook", background=INK, borderwidth=0,
                        bordercolor=INK, lightcolor=INK, darkcolor=INK, tabmargins=0)
        # native tabs hidden: _build_tabbar draws RGBZap's own (clam's tabs grow when
        # selected and can't take the lit-strip indicator)
        style.layout("Deck.TNotebook.Tab", [])
        style.configure("Dark.TCombobox", fieldbackground=PANEL2, background=PANEL2,
                        foreground=TEXT, arrowcolor=MUTE, bordercolor=LINE,
                        lightcolor=PANEL2, darkcolor=PANEL2, borderwidth=1,
                        padding=5, selectbackground=PANEL2, selectforeground=TEXT)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", PANEL2)],
                  bordercolor=[("focus", LINE)])
        # light slider knob on a dark trough so it's clearly visible
        style.configure("RGBZap.Horizontal.TScale", troughcolor=PANEL2,
                        background=TEXT, bordercolor=LINE, lightcolor=TEXT,
                        darkcolor=MUTE, borderwidth=0)
        style.map("RGBZap.Horizontal.TScale",
                  background=[("active", "#FFFFFF")])
        # slim arrowless scrollbar for the effects grid
        style.layout("RGBZap.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                          {"expand": "1", "sticky": "nswe"})]})])
        style.configure("RGBZap.Vertical.TScrollbar", troughcolor=PANEL, background="#3A4354",
                        bordercolor=PANEL, lightcolor="#3A4354", darkcolor="#3A4354",
                        arrowsize=6, width=6, gripcount=0)
        style.map("RGBZap.Vertical.TScrollbar", background=[("active", MUTE)])
        root.option_add("*TCombobox*Listbox.background", PANEL2)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", LINE)
        root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        root.option_add("*TCombobox*Listbox.font", FONT_BODY)

    def _tab(self, nb, title, scroll=False):
        """A tab page. scroll=True wraps it in a scrollable canvas (for tall pages) and
        returns the inner frame to fill."""
        if not scroll:
            f = tk.Frame(nb, bg=PANEL, padx=16, pady=16)
            nb.add(f, text=title)
            return f
        outer = tk.Frame(nb, bg=PANEL)
        nb.add(outer, text=title)
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0, bd=0, height=420)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                           style="RGBZap.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=PANEL, padx=16, pady=16)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 4), pady=8)
        canvas.pack(side="left", fill="both", expand=True)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        self._wheel_targets[str(outer)] = canvas
        return inner

    def _on_wheel(self, e):
        """One mouse-wheel handler for every scrollable tab: scrolls the selected one."""
        try:
            canvas = self._wheel_targets.get(self.nb.select())
        except tk.TclError:
            return
        if canvas is not None:
            canvas.yview_scroll(int(-e.delta / 120), "units")

    def _eyebrow(self, parent, text):
        return tk.Label(parent, text=text.upper(), font=FONT_EYE, fg=MUTE, bg=PANEL,
                        anchor="w")

    def _help(self, parent, text):
        """A small round "?" that explains itself on hover."""
        c = tk.Canvas(parent, width=16, height=16, bg=PANEL, highlightthickness=0,
                      cursor="question_arrow")
        ring = c.create_oval(1, 1, 15, 15, outline=MUTE)
        mark = c.create_text(8, 8, text="?", fill=MUTE, font=("Segoe UI Semibold", 8))
        c.bind("<Enter>", lambda _e: (c.itemconfig(ring, outline=TEXT),
                                      c.itemconfig(mark, fill=TEXT)), add="+")
        c.bind("<Leave>", lambda _e: (c.itemconfig(ring, outline=MUTE),
                                      c.itemconfig(mark, fill=MUTE)), add="+")
        Tooltip(c, text)
        return c

    def _heading(self, parent, text, help=None, pady=(14, 4)):
        """Section heading, optionally with a "?" help icon beside it."""
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=pady)
        self._eyebrow(row, text).pack(side="left")
        if help:
            self._help(row, help).pack(side="left", padx=(6, 0))
        return row

    def _flat(self, parent, text, cmd, bg=PANEL2, fg=TEXT, hover=LINE, pady=8,
              hoverable=True):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, bd=0,
                      relief="flat", font=FONT_BTN, cursor="hand2",
                      highlightthickness=0, activebackground=hover,
                      activeforeground=fg, padx=12, pady=pady)
        if hoverable:
            # toggle buttons manage their own bg by state, so they opt out
            b.bind("<Enter>", lambda e: b.config(bg=hover))
            b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def _link(self, parent, text, url=None, cmd=None):
        """Blue text that opens url in the browser (or runs cmd); underlines on hover."""
        lb = tk.Label(parent, text=text, font=FONT_BODY, fg=SYNC_BLUE, bg=PANEL,
                      cursor="hand2")
        lb.bind("<Button-1>", lambda _e: cmd() if cmd else webbrowser.open(url))
        lb.bind("<Enter>", lambda _e: lb.config(font=FONT_BODY + ("underline",)))
        lb.bind("<Leave>", lambda _e: lb.config(font=FONT_BODY))
        return lb

    def _check(self, parent, text, var, cmd):
        cb = tk.Checkbutton(parent, text=text, variable=var, command=cmd, anchor="w",
                            font=FONT_BODY, fg=TEXT, bg=PANEL, selectcolor=PANEL2,
                            activebackground=PANEL, activeforeground=TEXT,
                            highlightthickness=0, bd=0)
        cb.pack(fill="x")
        return cb

    def _gauge(self, parent, frm, to, cmd, init, suffix=""):
        """A themed slider with a light knob and a live Consolas value readout.
        Packs itself into `parent`; returns the ttk.Scale (for .set / reads)."""
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x")
        val = tk.Label(row, text=f"{init}{suffix}", font=FONT_MONO, fg=TEXT, bg=PANEL,
                       width=5, anchor="e")
        val.pack(side="right")

        def wrapped(v):
            val.config(text=f"{int(float(v))}{suffix}")
            cmd(v)

        s = ttk.Scale(row, from_=frm, to=to, orient="horizontal", command=wrapped,
                      style="RGBZap.Horizontal.TScale")
        s.set(init)
        s.pack(side="left", fill="x", expand=True, padx=(0, 10))
        return s

    def _combo(self, parent, var, values, width, cmd=None):
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly",
                          width=width, style="Dark.TCombobox", font=FONT_BODY)
        if cmd:
            cb.bind("<<ComboboxSelected>>", cmd)
        return cb

    def _field(self, grid, row, label, var, values, cmd, help=None):
        tk.Label(grid, text=label, font=FONT_BODY, fg=MUTE, bg=PANEL, anchor="w"
                 ).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        self._combo(grid, var, values, 14, cmd).grid(row=row, column=1, sticky="ew",
                                                      pady=4)
        if help:
            self._help(grid, help).grid(row=row, column=2, padx=(8, 0))

    # ---- thread-safe UI updates ----
    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def set_connected(self, state):
        color = GOOD if state else BAD
        txt = "Connected" if state else "Offline"
        def _apply():
            self.dot.itemconfig(self._dot_id, fill=color)
            self.link_lbl.config(text=txt, fg=color)
            addr = self.ble.address
            if state and addr:                # auto-connected strips get saved too
                self._remember_strip(self._found_names.get(addr, "Light strip"), addr)
                self._refresh_strips(addr)
            self._draw_link()
            if state and getattr(self, "tools", None) is not None:
                self.tools.on_strip_connected()      # re-light On-Air if it's already on air
        self.root.after(0, _apply)

    def send(self, payload):
        self.ble.submit(self.ble._write(payload))

    def _apply_current_color(self, fade=False):
        """Send the steady color. fade=True blends from what the strip shows now (a
        discrete change: a chip, a scene, Effect off); sliders and live drags pass
        False so they stay instant."""
        r, g, b = self.base_color
        s = self.brightness / 100.0
        target = (int(r * s), int(g * s), int(b * s))
        self._cancel_fade()
        ms = FADES.get(self.fade_name, 0)
        if (fade and ms and not (self.sync.running or self.audio.running
                                 or self.soft is not None)):
            begin = self.ble.last_color()
            if begin is not None and begin != target:
                self._run_fade(fade_steps(begin, target, max(1, round(ms / (SEND_INTERVAL * 1000)))))
                return
        # coalesced channel: rapid slider drags collapse to the latest color
        self.ble.queue("color", cmd_color(*target))

    def _run_fade(self, steps, done=None, i=0):
        """One fade step per strip update, on the Tk thread (the writer coalesces)."""
        if self.sync.running or self.audio.running or self.soft is not None:
            self._fade_job = None             # something else took over the strip
            return
        if i >= len(steps):
            self._fade_job = None
            if done:
                done()
            return
        self.ble.queue("color", cmd_color(*steps[i]))
        self._fade_job = self.root.after(int(SEND_INTERVAL * 1000), self._run_fade,
                                         steps, done, i + 1)

    def _cancel_fade(self):
        if self._fade_job is not None:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None

    def _apply_comfort(self):
        """Push the reduce-flashing flag and the color balance to the parts that use them."""
        self.sync.calm = self.audio.calm = self.ble.calm = self.reduce_flash
        self.ble.balance = tuple(p / 100.0 for p in self.balance_pct)

    def on_fade_change(self, _evt=None):
        self.fade_name = self.fade_var.get()

    def on_calm_toggle(self):
        self.reduce_flash = self.calm_var.get()
        self._apply_comfort()
        self._limiter.reset()
        self._fx_limiters.clear()
        self._fx_relayout()                   # marks / unmarks the strip's blink + jump tiles
        if self.reduce_flash:
            name = self.effect_var.get()
            if self.mode == "effect" and self._flashing_builtin(name):
                self.on_effect_off()          # one of those is running: stop it
            self.set_status("Reduce flashing is on")
        else:
            self.set_status("Reduce flashing is off")

    @staticmethod
    def _flashing_builtin(name):
        """The strip's own Blink / Jump effects run inside the strip, so RGBZap can't
        soften them: Reduce flashing switches them off."""
        return name in EFFECTS and name.startswith(("Blink ", "Jump "))

    def on_area_change(self, _evt=None):
        self.sync.area = self.area_var.get()

    def on_bars_toggle(self):
        self.sync.crop_bars = self.bars_var.get()

    def _paint(self, rgb):
        """Tint the live-color UI (swatch, aura bar, hex readout) to rgb."""
        hexc = "#%02x%02x%02x" % rgb
        self.swatch.config(bg=hexc)
        self.aura.config(bg=hexc)
        self.hex_val.config(text=hexc.upper())
        self._tab_glow(rgb)

    # ---- tab bar: spaced labels over a hairline; the selected one sits on a tiny
    # lit LED strip in the strip's live color that slides between tabs ----
    _GLOW_ROWS = 7                            # light spill above the strip, px

    def _build_tabbar(self):
        c = self.tabbar
        self._tab_labels = []
        for i, f in enumerate(self.nb.tabs()):
            title = self.nb.tab(f, "text")
            t = c.create_text(0, 15, text=title, font=FONT_TAB, fill=MUTE,
                              tags=f"tab{i}")
            hit = c.create_rectangle(0, 0, 0, 0, fill=INK, outline="", tags=f"tab{i}")
            c.tag_lower(hit)
            c.tag_bind(f"tab{i}", "<Button-1>", lambda _e, i=i: self.nb.select(i))
            c.tag_bind(f"tab{i}", "<Enter>", lambda _e, i=i: self._tab_hover(i, True))
            c.tag_bind(f"tab{i}", "<Leave>", lambda _e, i=i: self._tab_hover(i, False))
            self._tab_labels.append((t, hit))
        self._tab_rule = c.create_line(0, 35, 0, 35, fill=LINE)
        self._tab_glow_ids = [c.create_line(0, 0, 0, 0) for _ in range(self._GLOW_ROWS)]
        self._tab_core = c.create_rectangle(0, 0, 0, 0, outline="")
        self._tab_x = None                    # indicator's current (x0, x1)
        self._tab_rgb = (255, 255, 255)
        self._tab_job = None
        c.bind("<Configure>", lambda _e: self._tab_layout(animate=False))
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self._tab_layout(animate=True))

    def _tab_target(self):
        """Indicator span under the selected label: label width plus a little lip."""
        x0, _y0, x1, _y1 = self.tabbar.bbox(self._tab_labels[self.nb.index("current")][0])
        return x0 - 4, x1 + 4

    def _tab_layout(self, animate):
        c = self.tabbar
        w = c.winfo_width()
        if w <= 1:
            return
        n, cur = len(self._tab_labels), self.nb.index("current")
        seg = w / n                           # equal slots: nothing shifts on select
        for i, (t, hit) in enumerate(self._tab_labels):
            c.coords(t, seg * (i + 0.5), 15)
            c.coords(hit, seg * i, 0, seg * (i + 1), 36)
            c.itemconfig(t, fill=TEXT if i == cur else MUTE)
        c.coords(self._tab_rule, 0, 35, w, 35)
        target = self._tab_target()
        if not animate or self._tab_x is None:
            self._tab_x = target
            self._tab_draw()
        elif self._tab_job is None:
            self._tab_slide()

    def _tab_slide(self):
        # ease toward the selected tab; re-reads the target so fast clicks just retarget
        (a0, a1), (b0, b1) = self._tab_x, self._tab_target()
        k = 0.35
        self._tab_x = (a0 + (b0 - a0) * k, a1 + (b1 - a1) * k)
        if abs(b0 - self._tab_x[0]) < 0.5 and abs(b1 - self._tab_x[1]) < 0.5:
            self._tab_x, self._tab_job = (b0, b1), None
        else:
            self._tab_job = self.root.after(16, self._tab_slide)
        self._tab_draw()

    def _tab_draw(self):
        c, (x0, x1) = self.tabbar, self._tab_x
        c.coords(self._tab_core, x0, 33, x1, 36)
        for r, item in enumerate(self._tab_glow_ids):    # r=0 nearest the strip
            y = 32 - r
            c.coords(item, x0 + r, y, x1 - r, y)          # spill narrows as it rises
        self._tab_glow(self._tab_rgb)

    def _tab_glow(self, rgb):
        if not hasattr(self, "_tab_core"):
            return
        if max(rgb) < 40:                     # strip dark/off: keep the indicator visible
            rgb = (232, 236, 244)             # TEXT
        self._tab_rgb = rgb
        c, ink = self.tabbar, (0x0A, 0x0C, 0x12)
        mix = lambda f: "#%02x%02x%02x" % tuple(int(b + (x - b) * f) for x, b in zip(rgb, ink))
        c.itemconfig(self._tab_core, fill=mix(1.0))
        for r, item in enumerate(self._tab_glow_ids):
            c.itemconfig(item, fill=mix(0.42 * (1 - r / self._GLOW_ROWS) ** 2))

    def _tab_hover(self, i, on):
        if i != self.nb.index("current"):
            self.tabbar.itemconfig(self._tab_labels[i][0], fill="#B4BFD0" if on else MUTE)

    def set_color(self, rgb, fade=True):
        self.base_color = rgb
        if getattr(self.soft, "PICKS_COLOR", False):
            self._stop_soft()                 # Rainbow etc. ignore color: the pick wins
        if self.soft is None:              # a brightness-only effect keeps going, new color
            self.mode = "color"
        self.light_on = True
        self._paint(rgb)
        self._apply_current_color(fade=fade)
        if getattr(self, "_tray", None) is not None:   # tray dot follows the color
            self._tray.icon = self._tray_image(rgb)

    # ---- light strip picker ----
    @staticmethod
    def _saved_address():
        try:
            with open(ADDR_FILE) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def _remember_strip(self, name, addr):
        known = {s[1]: s for s in self.strips}
        if addr not in known:
            self.strips.append([name or "Light strip", addr])
        elif name and known[addr][0] == "Light strip":
            known[addr][0] = name             # a scan learned the real name

    def _refresh_strips(self, select=None):
        saved = {a for _, a in self.strips}
        self._choices = [tuple(s) for s in self.strips] + \
                        [f for f in self._found if f[1] not in saved]
        labels = [f"{n}  ·  {a[-5:]}" for n, a in self._choices]
        self.strip_cb.config(values=labels)
        addrs = [a for _, a in self._choices]
        if select in addrs:
            self.strip_cb.current(addrs.index(select))
        else:
            self.strip_var.set("Pick your light strip" if self._choices
                               else "No strip saved — tap ↻ to scan")

    def on_strip_pick(self, _evt=None):
        i = self.strip_cb.current()
        if i < 0:
            return
        name, addr = self._choices[i]
        if not (is_strip_name(name) or any(a == addr for _, a in self.strips)):
            # not a known strip name: RGBZap would send light commands to it, so ask
            if not messagebox.askokcancel(
                    "Not a known light strip",
                    f"RGBZap doesn't recognize \"{name}\" as an LED strip.\n\n"
                    "It will send light commands to this device. Only continue if it "
                    "is your light strip.", parent=self.root):
                self._refresh_strips(self.ble.address)   # put the dropdown back
                return
            # saved to the list only once it actually connects (see set_connected)
        else:
            self._remember_strip(name, addr)
        self._refresh_strips(addr)
        self._draw_link(True)
        self.ble.submit(self.ble._select(addr))

    def on_strip_scan(self):
        self.scan_btn.config(state="disabled")
        everything = self.all_devs_var.get()
        self.set_status("Scanning for Bluetooth devices..." if everything
                        else "Scanning for strips...")
        fut = self.ble.submit(self.ble._scan_all(everything=everything))
        fut.add_done_callback(lambda f: self.root.after(0, self._strip_scan_done, f))

    def _strip_scan_done(self, fut):
        self.scan_btn.config(state="normal")
        try:
            self._found = fut.result()
        except Exception as e:
            self.set_status(f"Scan failed: {e}")
            return
        self._found_names.update({a: n for n, a in self._found})
        for name, addr in self._found:        # refresh names of strips we already saved
            if any(a == addr for _, a in self.strips):
                self._remember_strip(name, addr)
        self._refresh_strips(self.ble.address)
        n = len(self._found)
        if self.all_devs_var.get():
            self.set_status(f"Found {n} Bluetooth device{'s' * (n != 1)} — closest first, "
                            "pick your strip above" if n else "No Bluetooth devices found")
        else:
            self.set_status(f"Found {n} strip{'s' * (n != 1)} — pick one above" if n
                            else "No strips found — is it on and the phone app closed? "
                                 "Try \"Show all Bluetooth devices\"")

    def on_link_toggle(self):
        if self.ble._want_connected:
            self.ble.submit(self.ble._disconnect())
            self._draw_link(False)
        else:
            self.ble.submit(self.ble._connect(fx=True))
            self._draw_link(True)

    def _draw_link(self, on=None):
        """iOS-style switch: knob right = link wanted; green once actually connected."""
        if on is None:
            on = self.ble._want_connected
        c = self.link_sw
        c.delete("all")
        track = (GOOD if self.ble._connected() else MUTE) if on else LINE
        c.create_oval(2, 2, 22, 22, fill=track, outline="")
        c.create_oval(22, 2, 42, 22, fill=track, outline="")
        c.create_rectangle(12, 2, 32, 22, fill=track, outline="")
        x = 24 if on else 4
        c.create_oval(x, 4, x + 16, 20, fill=TEXT if on else MUTE, outline="")

    # ---- color chips (presets + saved custom colors) ----
    def _chip(self, parent, rgb, on_remove=None):
        chip = tk.Frame(parent, bg="#%02x%02x%02x" % rgb, width=22, height=22,
                        cursor="hand2", highlightthickness=1, highlightbackground=LINE)
        chip.pack_propagate(False)
        chip.pack(side="left", padx=(0, 8))
        chip.bind("<Button-1>", lambda _e: self.set_color(rgb))
        if on_remove:
            chip.bind("<Button-3>", lambda _e: on_remove(rgb))
        return chip

    def _render_custom(self):
        for w in self.custom_row.winfo_children():
            w.destroy()
        for rgb in self.custom:
            self._chip(self.custom_row, rgb, on_remove=self._remove_custom)
        if not self.custom:
            tk.Label(self.custom_row, text="Pick color → Save to keep it here",
                     font=FONT_BODY, fg=MUTE, bg=PANEL).pack(side="left")

    def _add_custom(self, rgb):
        self.custom = [rgb] + [c for c in self.custom if c != rgb][:CUSTOM_MAX - 1]
        self._render_custom()

    def _remove_custom(self, rgb):
        self.custom = [c for c in self.custom if c != rgb]
        self._render_custom()

    # ---- live color picker: the strip follows the cursor while dragging ----
    def on_pick(self):
        if self._picker is not None and self._picker.winfo_exists():
            self._picker.lift()
            return
        W, H = 300, 180
        orig = self.base_color
        top = self._picker = tk.Toplevel(self.root, bg=PANEL, padx=14, pady=14)
        top.title("Pick a color")
        top.resizable(False, False)
        top.transient(self.root)

        img = tk.PhotoImage(width=W, height=H)
        img.put(hs_image_data(W, H))
        cv = tk.Canvas(top, width=W, height=H, bd=0, highlightthickness=0,
                       cursor="crosshair", bg=PANEL)
        cv.create_image(0, 0, anchor="nw", image=img)
        cv.image = img                       # keep a reference or Tk drops the image
        cv.pack()
        ring = cv.create_oval(0, 0, 0, 0, outline="white", width=2)

        def place(x, y):
            cv.coords(ring, x - 7, y - 7, x + 7, y + 7)

        h, s, _v = colorsys.rgb_to_hsv(*(c / 255.0 for c in orig))
        place(h * (W - 1), (1 - s) * (H - 1))

        def pick(e):
            x, y = min(max(e.x, 0), W - 1), min(max(e.y, 0), H - 1)
            place(x, y)
            # set_color queues on the coalescing "color" slot: a fast drag sends
            # only the newest color at the strip's real rate — live, never laggy
            self.set_color(hs_rgb(x / (W - 1), 1 - y / (H - 1)), fade=False)
        cv.bind("<Button-1>", pick)
        cv.bind("<B1-Motion>", pick)

        def cancel():
            self.set_color(orig, fade=False)
            top.destroy()

        def save():
            self._add_custom(self.base_color)
            top.destroy()

        row = tk.Frame(top, bg=PANEL)
        row.pack(fill="x", pady=(12, 0))
        self._flat(row, "Save to Custom", save, bg=GOOD, fg=INK, hover="#5FE3B4"
                   ).pack(side="left", expand=True, fill="x", padx=(0, 5))
        self._flat(row, "Done", top.destroy).pack(side="left", expand=True, fill="x",
                                                 padx=(0, 5))
        self._flat(row, "Cancel", cancel).pack(side="left", expand=True, fill="x")
        top.protocol("WM_DELETE_WINDOW", top.destroy)   # X keeps the live color

    def on_brightness(self, val):
        self.brightness = int(float(val))
        self._apply_current_color()

    def on_effect(self):
        name = self.effect_var.get()
        if self.reduce_flash and self._flashing_builtin(name):
            self.on_effect_off()
            self.set_status(f"{name} is off while Reduce flashing is on")
            return
        self.mode = "effect"
        self.light_on = True
        if name in SOFT_EFFECTS:
            self._cancel_fade()
            self._start_soft(name)
            return
        self._stop_soft()
        self._cancel_fade()
        self._stop_sync()                     # sync frames would overwrite the effect
        self._stop_audio()
        self.ble._last.pop(b"\x05", None)     # the strip's color is no longer what we sent
        # same "color" slot as the flame frames, so a pending one can't land after
        # this and freeze the strip on a static color
        self.ble.queue("color", cmd_effect(EFFECTS[name]))
        self.set_status(f"{name} effect ON")

    # ---- EFFECTS tab grid ----
    @staticmethod
    def _rrect(c, x0, y0, x1, y1, r, **kw):
        """Rounded rectangle on a Tk canvas (smoothed polygon)."""
        pts = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
        return c.create_polygon(pts, smooth=True, **kw)

    def _fx_layout(self, evt):
        """(Re)build the tile grid for the canvas width: no-effect + app effects,
        then the strip's built-ins grouped by kind. Labels say only the variant."""
        if evt.width == self._fx_w:
            return
        self._fx_w = w = evt.width
        c = self.fx_canvas
        c.delete("all")
        self._fx_tiles, self._fx_shown, self._fx_active = {}, {}, False
        cols, gap, edge = FX_COLS, 10, 5                 # edge: room for the active ring
        tw = (w - 2 * edge - gap * (cols - 1)) // cols   # tile width
        th = round(tw / FX_ASPECT)                       # tile height
        pitch = th + 28                                  # row to row: tile + label + air
        groups = [(f"{title} · stops when the app closes",
                   ([(None, "None")] if k == 0 else []) + [(n, n) for n in names])
                  for k, (title, names) in enumerate(SOFT_GROUPS)]
        for kind in ("Jump", "Crossfade", "Blink"):
            groups.append((f"On the strip · {kind}", [(n, n.partition(" ")[2])
                                                      for n in EFFECTS
                                                      if n.startswith(kind + " ")]))
        y = 0
        for title, items in groups:
            c.create_text(edge, y + 8, text=title.upper(), anchor="w", fill=MUTE,
                          font=FONT_EYE)
            y += 24
            for i, (name, label) in enumerate(items):
                x0 = edge + (i % cols) * (tw + gap)
                y0 = y + (i // cols) * pitch
                tag = f"fx{len(self._fx_tiles)}"
                ring = self._rrect(c, x0 - 4, y0 - 4, x0 + tw + 4, y0 + th + 4, 12,
                                   fill="", outline="", width=2, tags=tag)
                glow = self._rrect(c, x0 - 2, y0 - 2, x0 + tw + 2, y0 + th + 2, 10,
                                   fill=PANEL, outline="", tags=tag)
                if name is None:                 # empty tile, red cross: no effect
                    face = self._rrect(c, x0, y0, x0 + tw, y0 + th, 8, fill=PANEL,
                                       outline=LINE, dash=(3, 3), tags=tag)
                    cx, cy, h = x0 + tw / 2, y0 + th / 2, th * 0.28
                    for ya, yb in ((cy - h, cy + h), (cy + h, cy - h)):
                        c.create_line(cx - h, ya, cx + h, yb, fill=BAD, width=2,
                                      capstyle="round", tags=tag)
                else:
                    face = self._rrect(c, x0, y0, x0 + tw, y0 + th, 8, fill=PANEL2,
                                       outline=LINE, tags=tag)
                locked = self.reduce_flash and name is not None and self._flashing_builtin(name)
                text = c.create_text(x0 + tw / 2, y0 + th + 12,
                                     text=label + " ⚠" if locked else label, fill=MUTE,
                                     font=("Segoe UI", 8), tags=tag)
                self._fx_tiles[name] = (ring, glow, face, text)
                c.tag_bind(tag, "<Button-1>", lambda _e, n=name: self.on_fx_pick(n))
                c.tag_bind(tag, "<Enter>", lambda _e, f=face: (
                    c.itemconfig(f, outline=MUTE), c.config(cursor="hand2")))
                c.tag_bind(tag, "<Leave>", lambda _e, f=face: (
                    c.itemconfig(f, outline=LINE), c.config(cursor="")))
            y += ((len(items) + cols - 1) // cols) * pitch + 6
        c.config(scrollregion=(0, -6, w, y))
        self._fx_render()

    def _fx_relayout(self):
        """Rebuild the tile grid now (e.g. after Reduce flashing changes which tiles lock)."""
        self._fx_w = 0
        w = self.fx_canvas.winfo_width()
        if w > 1:
            self._fx_layout(SimpleNamespace(width=w))

    def on_fx_pick(self, name):
        if name is None:
            self.on_effect_off()
        elif self.reduce_flash and self._flashing_builtin(name):
            self.set_status(f"{name} is off while Reduce flashing is on (System > Comfort)")
            return
        else:
            self.effect_var.set(name)
            self.on_effect()
        self._fx_render()

    def _fx_current(self):
        """Tile to highlight: the running effect, None for a steady color, or ""
        (nothing) while screen sync / audio reactive owns the strip."""
        if self.sync.running or self.audio.running:
            return ""
        return self.effect_var.get() if self.mode == "effect" else None

    def _fx_tick(self):
        # always ticking (cheap), but only animates while the tab is on screen
        self.root.after(80, self._fx_tick)
        if self.nb.select() != str(self.fx_tab) or self.root.state() != "normal":
            return
        speed = int(float(self.speed.get()))
        self._fx_t += 0.08 * _sf(speed)
        for name, prev in self._fx_prev.items():
            out = prev.step(0.08, speed)
            if not isinstance(out, tuple):      # level effect: shown on the user's color
                out = tuple(int(c * out) for c in self.base_color)
            if self.reduce_flash:               # previews flash on screen too: smooth them
                out = self._fx_limiters.setdefault(name, FlashLimiter()).step(out, 0.08)
            self._fx_paint(name, out)
        for name in EFFECTS:
            if self.reduce_flash and self._flashing_builtin(name):
                self._fx_paint(name, (34, 38, 48))      # locked: a still, dim tile
            else:
                self._fx_paint(name, builtin_preview(name, self._fx_t))
        self._fx_render()

    def _fx_paint(self, name, rgb):
        items = self._fx_tiles.get(name)
        if items is None or self._fx_shown.get(name) == rgb:
            return
        self._fx_shown[name] = rgb
        _ring, glow, face, _text = items
        bg = (0x12, 0x15, 0x1E)                         # PANEL: the glow bleeds into it
        bleed = tuple(int(b + (c - b) * 0.35) for c, b in zip(rgb, bg))
        self.fx_canvas.itemconfig(face, fill="#%02x%02x%02x" % rgb)
        self.fx_canvas.itemconfig(glow, fill="#%02x%02x%02x" % bleed)

    def _fx_render(self):
        """Move the highlight ring + bright label to the active tile."""
        cur = self._fx_current()
        if cur == self._fx_active or not self._fx_tiles:
            return
        c = self.fx_canvas
        old = self._fx_tiles.get(self._fx_active)
        if old:
            c.itemconfig(old[0], outline="")
            c.itemconfig(old[3], fill=MUTE, font=("Segoe UI", 8))
        new = self._fx_tiles.get(cur)
        if new:
            c.itemconfig(new[0], outline=TEXT)
            c.itemconfig(new[3], fill=TEXT, font=("Segoe UI Semibold", 8))
        self._fx_active = cur

    # ---- scenes ----
    def _scene_snapshot(self):
        return {"color": list(self.base_color), "brightness": self.brightness,
                "mode": self.mode, "effect": self.effect_var.get(),
                "speed": int(float(self.speed.get()))}

    def _refresh_scenes(self):
        self.scene_cb.config(values=list(self.scenes))

    def apply_scene(self, name):
        sc = self.scenes.get(name)
        if sc is None:
            return
        self._stop_sync()                     # a scene owns the strip, like an effect
        self._stop_soft()
        self._stop_audio()
        self.effect_var.set(sc["effect"])
        self.bright.set(sc["brightness"])     # fires on_brightness
        self.speed.set(sc["speed"])
        self.set_color(tuple(sc["color"]))
        if sc["mode"] == "effect":
            self.on_effect()
        else:
            self.on_effect_off()
        self.scene_var.set(name)
        self.set_status(f"Scene: {name}")

    def on_scene_save(self):
        current = self.scene_var.get()
        name = simpledialog.askstring(
            "Save scene", "Name for the current color, brightness and effect:",
            initialvalue=current if current in self.scenes else "", parent=self.root)
        name = (name or "").strip()[:40]
        if not name:
            return
        existed = name in self.scenes
        self.scenes[name] = self._scene_snapshot()
        self._refresh_scenes()
        self.scene_var.set(name)
        self.set_status(f"Scene {'updated' if existed else 'saved'}: {name}")

    def on_scene_delete(self):
        name = self.scene_var.get()
        if name not in self.scenes:
            self.set_status("Pick a scene to delete first")
            return
        if not messagebox.askyesno("Delete scene", f"Delete the scene \"{name}\"?",
                                   parent=self.root):
            return
        del self.scenes[name]
        self._refresh_scenes()
        self.scene_var.set("")
        self.set_status(f"Scene deleted: {name}")

    def on_effect_off(self):
        """Stop any effect (flame or built-in) and go back to the steady color."""
        self._stop_soft()
        self._stop_audio()
        self.mode = "color"
        self._apply_current_color(fade=True)  # a color frame ends built-in effects too
        self.set_status("Effect off")

    def on_on(self):
        """Turn the lights on; if they were faded out, fade back up to the color."""
        self.light_on = True
        self.send(cmd_on())
        if self.mode == "color" and not (self.sync.running or self.audio.running):
            self._apply_current_color(fade=True)

    def on_off(self):
        self._stop_soft()
        self._stop_sync()
        self._stop_audio()
        self._cancel_fade()
        self.light_on = False
        ms, cur = FADES.get(self.fade_name, 0), self.ble.last_color()
        if ms and cur is not None and cur != (0, 0, 0):   # fade out, then switch off
            self._run_fade(fade_steps(cur, (0, 0, 0), max(1, round(ms / (SEND_INTERVAL * 1000)))),
                           done=lambda: self.ble.queue("color", cmd_off()))
        else:
            self.ble.queue("color", cmd_off())

    # ---- one control surface for hotkeys, the local API and OBS (Tk thread only) ----
    def toggle_power(self):
        self.on_on() if not self.light_on else self.on_off()

    def step_brightness(self, delta):
        v = max(1, min(100, int(round(float(self.bright.get()))) + delta))
        self.bright.set(v)                    # fires on_brightness
        self.set_status(f"Brightness {v}%")

    def set_brightness_value(self, v):
        self.bright.set(max(1, min(100, int(v))))

    def cycle_scene(self, step):
        names = list(self.scenes)
        if not names:
            self.set_status("No scenes saved yet")
            return
        cur = self.scene_var.get()
        i = (names.index(cur) + step) % len(names) if cur in names else (0 if step > 0 else -1)
        self.apply_scene(names[i])

    def set_effect(self, name):
        """Start the named effect, or stop effects with None."""
        if name is None:
            self.on_effect_off()
        else:
            self.effect_var.set(name)
            self.on_effect()
            self._fx_render()

    def set_sync(self, on):
        """on: True / False / 'toggle'"""
        want = (not self.sync.running) if on == "toggle" else bool(on)
        if want != self.sync.running:
            self.on_toggle_sync()

    def set_audio(self, on):
        want = (not self.audio.running) if on == "toggle" else bool(on)
        if want != self.audio.running:
            self.on_toggle_audio()

    def flash(self, rgb):
        """A short alert flash in rgb, then back to what the strip was showing."""
        if self.ble._connected():
            self.ble.submit(self.ble._alert("flash", self.brightness, color=tuple(rgb)))

    def effect_names(self):
        return list(SOFT_EFFECTS) + list(EFFECTS)

    def set_balance(self, idx, pct):
        """Strip color balance: gain per channel in percent (100 = untouched)."""
        self.balance_pct[idx] = int(pct)
        self._apply_comfort()
        if self.mode == "color" and self.soft is None and not (self.sync.running
                                                               or self.audio.running):
            self._apply_current_color()       # show the change right away

    def status_info(self):
        r, g, b = self.base_color
        return {"version": APP_VERSION, "connected": self.ble._connected(),
                "power": "on" if self.light_on else "off",
                "color": "#%02X%02X%02X" % (r, g, b), "brightness": self.brightness,
                "mode": self.mode, "effect": self.effect_var.get() if self.mode == "effect" else None,
                "scene": self.scene_var.get() or None, "sync": self.sync.running,
                "audio": self.audio.running}

    def run_hotkey(self, action):
        """A global hotkey fired (already on the Tk thread)."""
        {"toggle_power": self.toggle_power,
         "brightness_up": lambda: self.step_brightness(+10),
         "brightness_down": lambda: self.step_brightness(-10),
         "toggle_sync": lambda: self.set_sync("toggle"),
         "toggle_audio": lambda: self.set_audio("toggle"),
         "next_scene": lambda: self.cycle_scene(+1),
         "prev_scene": lambda: self.cycle_scene(-1),
         "effect_off": self.on_effect_off}.get(action, lambda: None)()

    # ---- software effects (SOFT_EFFECTS: flames, Breathe, Rainbow, ...) ----
    def _start_soft(self, name):
        self._stop_sync()
        self._stop_audio()
        self.light_on = True
        self.send(cmd_on())
        self._limiter.reset()
        self.soft = SOFT_EFFECTS[name]()
        self.set_status(f"{name} effect ON")
        if self._soft_job is None:
            self._soft_tick()

    def _stop_soft(self):
        if self.soft is not None:
            self._paint(self.base_color)      # a color-picking effect left the UI tinted
        self.soft = None
        if self._soft_job is not None:
            self.root.after_cancel(self._soft_job)
            self._soft_job = None

    def _soft_tick(self):
        # Tk thread: reads sliders safely; the writer paces/coalesces to the strip
        out = self.soft.step(SEND_INTERVAL, int(float(self.speed.get())))
        s = self.brightness / 100.0
        if isinstance(out, tuple):            # effect picks the color: show it live
            r, g, b = out
            self._paint(out)
        else:                                 # brightness-only effect on the user's color
            r, g, b = self.base_color
            s *= out
        frame = (int(r * s), int(g * s), int(b * s))
        if self.reduce_flash:                 # soften fast flashing before it reaches the strip
            frame = self._limiter.step(frame, SEND_INTERVAL)
        self.ble.queue("color", cmd_color(*frame))
        self._soft_job = self.root.after(int(SEND_INTERVAL * 1000), self._soft_tick)

    def on_speed(self, val):
        self.ble.queue("speed", cmd_effect_speed(int(float(val))))

    # ---- screen sync callbacks ----
    def set_sync_swatch(self, rgb):
        # show the live synced color across the UI (thread-safe)
        self.root.after(0, lambda: self._paint(rgb))

    def on_display_change(self, _evt=None):
        if not self._monitors:
            return
        idx = self.display_cb.current()
        self.sync.monitor_index = self._monitors[idx][0]

    def on_style_change(self, _evt=None):
        self.sync.style = self.style_var.get()
        # adopt the style's default saturation and reflect it on the slider
        self.sync.sat = SYNC_STYLES[self.sync.style][0]
        self.sat_scale.set(int(self.sync.sat * 100))
        if self.sync.running:
            self.set_status(f"Screen Sync ON — {self.sync.style}")

    def on_smooth(self, val):
        # slider 5..100 -> alpha 0.05..1.0 (higher = snappier, lower = smoother)
        self.sync.alpha = max(0.05, int(float(val)) / 100.0)

    def on_sat(self, val):
        self.sync.sat = int(float(val)) / 100.0

    def on_startup_toggle(self):
        want = self.startup_var.get()
        if set_startup(want):
            self.set_status("Will run at startup" if want else "Startup entry removed")
        else:
            self.startup_var.set(not want)  # revert the checkbox on failure
            self.set_status("Couldn't change startup setting")

    # ---- notification / call alerts ----
    def on_notify_toggle(self):
        self.notify_flash, self.notify_call = self.flash_var.get(), self.call_var.get()
        if UserNotificationListener is None:
            return
        if self.notify_flash or self.notify_call:
            self.notifier.start()
        else:
            self.notifier.stop()

    def _on_notification(self, call):
        # watcher thread: only reads plain bools and hands work to the BLE loop
        if call and self.notify_call:
            self.ble.submit(self.ble._alert("call", self.brightness))
        elif not call and self.notify_flash:
            self.ble.submit(self.ble._alert("notify", self.brightness))

    def on_alert_test(self, kind):
        if not self.ble._connected():
            self.set_status("Connect a light strip first")
            return
        self.set_status("Test call — pulsing for 4 s" if kind == "call"
                        else "Test notification")
        self.ble.submit(self.ble._alert(kind, self.brightness, max_s=4.0))

    def on_toggle_sync(self):
        if self.sync.running:
            self._stop_sync()
        else:
            if not self._monitors:
                self.set_status("No display detected for sync")
                return
            self._stop_soft()
            self._stop_audio()
            self.light_on = True
            self.send(cmd_on())          # make sure the strip is on
            self.sync.start()
            self.sync_btn.config(text="■  Stop screen sync", bg=BAD, fg=INK,
                                 activebackground="#FF8080")

    # ---- audio reactive ----
    def _audio_set(self, attr, value):
        setattr(self.audio, attr, value)      # read live by the capture thread
        if attr == "source" and self.audio.running:   # new device: reopen capture
            self.audio.stop()
            self.root.after(300, self._start_audio)

    def on_audio_mode(self, _evt=None):
        mode = self.audio_mode_var.get()
        self.audio.mode = mode
        self.audio._reset()                   # fresh envelope/hue for the new mode
        self.audio_desc.config(text=AUDIO_MODES[mode])
        if self.audio.running:
            self.set_status(f"Audio reactive ON — {mode}")

    def on_toggle_audio(self):
        if self.audio.running:
            self._stop_audio()
        else:
            self._start_audio()

    def _start_audio(self):
        if self.audio.running:
            return
        self._stop_sync()
        self._stop_soft()
        self.light_on = True
        self.send(cmd_on())
        self.audio.start()
        self.audio_btn.config(text="■  Stop audio reactive", bg=BAD, fg=INK,
                              activebackground="#FF8080")
        self._meter_tick()

    def _stop_audio(self):
        if self.audio.running:
            self.audio.stop()                 # thread ends -> _audio_stopped resets UI

    def _audio_stopped(self):
        """Tk thread, after capture ends (stopped or failed): reset button + meter."""
        self.audio_btn.config(text="▶  Start audio reactive", bg=AUDIO_PINK, fg="white",
                              activebackground="#E86AB1")
        self._paint(self.base_color)
        self._draw_meter()
        if self.soft is None and not self.sync.running:
            self._apply_current_color()       # settle on the steady color, not a mid-beat frame

    def _meter_tick(self):
        self._draw_meter()
        if self.audio.running:
            self.root.after(50, self._meter_tick)

    def _draw_meter(self):
        c = self.meter
        w = max(c.winfo_width(), 200)
        f = self.audio.feat if self.audio.running else None
        span = w - 52 - 80                    # bars leave room for beat light + swatch
        for i, key in enumerate(("bass", "mid", "high")):
            y = 12 + i * 20
            v = f[key] if f else 0.0
            c.coords(self._meter_bars[key], 52, y, 52 + span * v, y + 10)
        for item in c.find_withtag("track"):
            x0, y0, _x1, y1 = c.coords(item)
            c.coords(item, x0, y0, 52 + span, y1)
        hot = f is not None and time.perf_counter() - self.audio.last_beat < 0.12
        c.coords(self._meter_beat, w - 66, 26, w - 50, 42)
        c.itemconfig(self._meter_beat, fill=AUDIO_PINK if hot else PANEL)
        rgb, lvl = self.audio.out_rgb if f else ((0, 0, 0), 0.0)
        c.coords(self._meter_out, w - 38, 10, w - 10, 64)
        c.itemconfig(self._meter_out, fill="#%02x%02x%02x" % tuple(int(x * lvl) for x in rgb))

    def _stop_sync(self):
        if self.sync.running:
            self.sync.stop()
            self.sync_btn.config(text="▶  Start screen sync", bg=SYNC_BLUE, fg="white",
                                 activebackground="#5A97F8")

    # ---- system tray: closing the window hides it here; Quit really exits ----
    _tray_bolt = None

    @classmethod
    def _tray_image(cls, rgb):
        """64px tray icon: the RGBZap bolt with a badge in the strip's live color."""
        if cls._tray_bolt is None:
            try:
                cls._tray_bolt = Image.open(os.path.join(ASSETS, "icon.png")).convert(
                    "RGBA").resize((64, 64), Image.LANCZOS)
            except OSError:
                cls._tray_bolt = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        img = cls._tray_bolt.copy()
        d = ImageDraw.Draw(img)
        d.ellipse((38, 38, 63, 63), fill=INK)
        d.ellipse((42, 42, 59, 59), fill=tuple(rgb))
        return img

    def _start_tray(self):
        if pystray is None:
            return
        Item, Menu = pystray.MenuItem, pystray.Menu

        def act(fn):
            # pystray calls from its own thread; Tk must only be touched on the main one
            return lambda _icon, _item: self.root.after(0, fn)

        def colors():
            named = [Item(name, act(lambda c=rgb: self.set_color(c))) for name, rgb in PRESETS]
            mine = [Item("#%02X%02X%02X" % c, act(lambda c=c: self.set_color(c)))
                    for c in self.custom]
            return mine + ([Menu.SEPARATOR] if mine else []) + named

        def scenes():
            return ([Item(n, act(lambda n=n: self.apply_scene(n))) for n in self.scenes]
                    or [Item("No scenes yet", None, enabled=False)])

        def pick_effect(name):
            self.effect_var.set(name)
            self.on_effect()

        effects = [Item(title, Menu(*[Item(n, act(lambda n=n: pick_effect(n)))
                                      for n in names]))
                   for title, names in SOFT_GROUPS]
        effects.append(Item("On the strip", Menu(*[Item(n, act(lambda n=n: pick_effect(n)))
                                                   for n in EFFECTS])))
        effects += [Menu.SEPARATOR, Item("Effect off", act(self.on_effect_off))]
        bright = [Item(f"{v}%", act(lambda v=v: self.bright.set(v)),
                       checked=lambda _i, v=v: self.brightness == v, radio=True)
                  for v in (100, 75, 50, 25, 10)]

        menu = Menu(
            Item("Show RGBZap", act(self.show_window), default=True),   # left-click
            Item(lambda _i: f"Update to {self.update_info[0]} available"
                 if self.update_info else "Update available",
                 act(self.open_update), visible=lambda _i: bool(self.update_info)),
            Menu.SEPARATOR,
            Item("Turn on", act(self.on_on)),
            Item("Turn off", act(self.on_off)),
            Item("Scenes", Menu(scenes)),                 # rebuilt on open: saves show at once
            Item("Colors", Menu(colors)),                 # rebuilt on open: custom stays current
            Item("Brightness", Menu(*bright)),
            Item("Effects", Menu(*effects)),
            Item("Screen sync", act(self.on_toggle_sync),
                 checked=lambda _i: self.sync.running),
            Item("Audio reactive", act(self.on_toggle_audio),
                 checked=lambda _i: self.audio.running),
            Item("On-Air light", act(self.tools.toggle_onair),
                 checked=lambda _i: self.tools.onair_enabled),
            Menu.SEPARATOR,
            Item("Quit RGBZap", act(self.quit)),
        )
        self._tray = pystray.Icon("RGBZap", self._tray_image(self.base_color),
                                  "RGBZap — LED Console", menu)
        self._tray.run_detached()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        if self._tray is None:
            self.quit()                       # no tray available: X quits as before
            return
        self.root.withdraw()
        if not self._tray_hint_shown:         # tell them once per run where it went
            self._tray_hint_shown = True
            try:
                self._tray.notify("RGBZap is still running here. Right-click for options, "
                                  "or Quit RGBZap to exit.", "RGBZap")
            except Exception:
                pass

    def _listen_for_second_launch(self, lock):
        """A second launch connects to our lock port instead of starting: show the
        window, since it may be hidden in the tray."""
        def run():
            while True:
                try:
                    conn, _ = lock.accept()
                    conn.close()
                    self.root.after(0, self.show_window)
                except OSError:
                    return
        threading.Thread(target=run, daemon=True).start()

    # ---- lights off with the PC ----
    def on_shutdown_toggle(self):
        self.off_on_shutdown = self.shutdown_var.get()

    def _on_session_query(self):
        """Windows is about to shut down, restart or sign out (watcher thread). Switch the
        strip off now, while Bluetooth is still up, and wait for the command to go out."""
        if not self.off_on_shutdown:
            shutdown_log("session ending: 'lights off with the PC' is unticked, lights left alone")
            return
        self._shutdown_prev = self.light_on
        t0 = time.monotonic()
        shutdown_log(f"session ending: strip connected={self.ble._connected()}, "
                     f"lights on={self.light_on}; sending off")
        try:
            sent = self.ble.submit(self.ble._shutdown_off()).result(timeout=2.5)
            shutdown_log(f"off command {'sent' if sent else 'NOT sent (strip not connected)'} "
                         f"({(time.monotonic() - t0) * 1000:.0f} ms)")
        except Exception as e:
            shutdown_log(f"off command FAILED: {e!r}")      # never hold up the shutdown

    def _on_session_end(self, ended):
        """The verdict (watcher thread). Cancelled: lights back. Went ahead: one last off,
        which is also the only message when Windows forces a shutdown through."""
        shutdown_log("shutdown went ahead" if ended else "shutdown cancelled")
        if ended:
            if self.off_on_shutdown:
                try:
                    sent = self.ble.submit(self.ble._shutdown_off(quick=True)).result(timeout=1.5)
                    shutdown_log("final off command " + ("sent" if sent else "NOT sent (not connected)"))
                except Exception as e:
                    shutdown_log(f"final off command FAILED: {e!r}")
            return
        if self.ble.frozen:
            self.root.after(0, self._shutdown_cancelled)

    def _shutdown_cancelled(self):
        """Windows cancelled the shutdown (another program objected): lights back as they were."""
        self.ble.frozen = False
        if self._shutdown_prev:
            self.on_on()
            if self.mode == "effect" and self.effect_var.get() in EFFECTS and self.soft is None:
                self.on_effect()              # the strip's own effects need starting again
        self.set_status("Shutdown cancelled - lights restored")

    def quit(self):
        self.session.stop()
        self.tools.stop()                     # release hotkeys, close the API port, stop OBS
        if self._tray is not None:
            self._tray.visible = False        # stop() alone leaves a ghost icon on Windows
            self._tray.stop()
        self.on_close()

    def on_close(self):
        # Block until the BLE disconnect actually completes before tearing down.
        # The worker thread is a daemon, so if we destroy the window and exit while
        # the disconnect is still in flight, the strip is left in a ghost connection
        # and stops advertising — the "have to unplug it to reconnect" bug.
        self.sync.stop()
        self.audio.stop()
        self._stop_soft()                    # no tick may fire into a destroyed window
        self._save()
        self.set_status("Disconnecting...")
        try:
            fut = self.ble.submit(self.ble._disconnect())
            fut.result(timeout=4.0)   # wait for a clean GATT disconnect
        except Exception:
            pass                      # never block the close on an error/timeout
        self.root.destroy()


def _selftest():
    """Prove the Dominant (saturation-weighted) extractor pops a small colored
    subject that a flat Average washes out. Run: python elk_gui.py --selftest"""
    # black BGRA frame with a small red patch (R=255 -> B=0,G=0,R=255)
    frame = np.zeros((200, 320, 4), dtype=np.uint8)
    frame[..., 3] = 255
    frame[80:120, 140:180, 2] = 255  # red patch, ~2.5% of the frame

    s = ScreenSync(None, lambda *_: None, lambda *_: None, lambda: 100)
    s.dark_mode, s.sat, s.style = "Keep", 1.0, "Accurate"

    s.extract_mode, s._prev = "Dominant", None
    dr, dg, db, _ = s._extract(frame)
    s.extract_mode, s._prev = "Average", None
    ar, ag, ab, _ = s._extract(frame)

    assert dr > dg and dr > db, f"Dominant should be red-ish, got {(dr, dg, db)}"
    assert dr > ar + 40, f"Dominant red ({dr}) should beat Average red ({ar})"
    assert ar < 30, f"Average of a mostly-black frame should be dark, got {ar}"
    print(f"OK  Dominant={(dr, dg, db)}  Average={(ar, ag, ab)}")

    # connect-ripple envelope: starts dark, settles bright, stays in range, bounces
    samples = [ripple_envelope(i / 60) for i in range(61)]
    assert all(0.0 <= v <= 1.0 for v in samples), "envelope out of [0,1]"
    assert samples[0] < 0.05, f"ripple should start dark, got {samples[0]:.3f}"
    assert samples[-1] > 0.9, f"ripple should settle bright, got {samples[-1]:.3f}"
    peak = max(range(61), key=lambda i: samples[i])
    assert any(samples[j] < samples[j - 1] for j in range(peak + 1, 61)), \
        "ripple should dip after its first peak (a bounce, not a plain fade)"
    print("OK  ripple envelope: dark-start, bounce, bright-settle")

    # settings: round-trip, and bad values fall back to defaults instead of crashing
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "s.json")
    good = dict(DEFAULT_SETTINGS, color=[10, 20, 30], brightness=40, mode="effect",
                effect="Blink Red", style="Vivid", sat=200)
    save_settings(good, p)
    assert load_settings(p) == good, "settings round-trip failed"
    with open(p, "w") as f:
        json.dump({"color": [300, 0, 0], "brightness": 0, "effect": "Nope",
                   "speed": True, "sat": "x", "smooth": 60}, f)
    got = load_settings(p)
    assert got == dict(DEFAULT_SETTINGS, smooth=60), f"bad values not rejected: {got}"
    with open(p, "w") as f:
        f.write("{not json")
    assert load_settings(p) == DEFAULT_SETTINGS, "corrupt file should give defaults"
    with open(p, "w") as f:
        json.dump({"custom": [[1, 2, 3], [999, 0, 0], "x", [4, 5, 6]] + [[7, 7, 7]] * 20}, f)
    got = load_settings(p)["custom"]
    assert got[:2] == [[1, 2, 3], [4, 5, 6]] and len(got) == CUSTOM_MAX, got
    with open(p, "w") as f:
        json.dump({"strips": [["Desk", "AA:BB"], ["", "CC"], ["x"], 5, ["Bed", "DD:EE"]]}, f)
    assert load_settings(p)["strips"] == [["Desk", "AA:BB"], ["Bed", "DD:EE"]], "strips"
    print("OK  settings round-trip + validation + corrupt fallback + custom colors")

    # update check: numeric version compare, only public newer releases on our own
    # GitHub page, and a setting that defaults on but honours a saved off
    assert DEFAULT_SETTINGS["update_check"] is True
    for raw, want in (({"update_check": False}, False), ({"update_check": "no"}, True)):
        with open(p, "w") as f:
            json.dump(raw, f)
        assert load_settings(p)["update_check"] is want, f"update_check {raw}"
    assert parse_version("v1.0.1") == (1, 0, 1) and parse_version("1.10.0") == (1, 10, 0)
    assert all(parse_version(x) is None for x in ("1.0", "v1.0.1-beta", "", None, "latest", 5))
    rel = {"tag_name": "v1.0.1", "html_url": APP_URL + "/releases/tag/v1.0.1",
           "draft": False, "prerelease": False}
    assert parse_release(rel, "1.0.0") == ("1.0.1", rel["html_url"]), "newer not seen"
    assert parse_release(rel, "1.0.1") is None and parse_release(rel, "1.0.2") is None
    assert parse_release(dict(rel, tag_name="v1.10.0"), "1.9.0")[0] == "1.10.0", "1.10 > 1.9"
    assert parse_release(dict(rel, draft=True), "1.0.0") is None, "draft offered"
    assert parse_release(dict(rel, prerelease=True), "1.0.0") is None, "prerelease offered"
    assert parse_release(dict(rel, html_url="https://evil.example/x"), "1.0.0") is None
    assert parse_release(dict(rel, html_url=None), "1.0.0") is None
    assert parse_release(dict(rel, tag_name="nightly"), "1.0.0") is None
    assert parse_release("junk") is None and parse_release(None) is None
    import io
    seen = []

    def fake_open(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        return io.BytesIO(json.dumps(rel).encode())
    assert fetch_latest_release(fake_open) == rel
    assert seen == [(RELEASES_API, f"RGBZap/{APP_VERSION}", 8)], seen
    print("OK  update check: numeric compare, public+own-page only, request is one "
          "plain GET, setting defaults on")

    # comfort / tools settings: valid values kept (shortcuts normalized), junk falls back
    mine = {"fade": "Slow", "reduce_flash": True, "area": "Edges", "crop_bars": False,
            "bal_r": 90, "bal_g": 80, "bal_b": 100, "hotkeys_on": False,
            "hotkeys": {"toggle_power": "Alt+Ctrl+Home", "brightness_up": "", "bogus": "ctrl+alt+x"},
            "onair": True, "onair_mic": True, "obs_on": True, "obs_host": "pc.local",
            "obs_port": 4456, "obs_password": "pw",
            "obs_scenes": {"Game": "Focus", "": "x", "Bad": 5}, "api_on": True,
            "api_port": 50000, "api_token": "a" * 32}
    with open(p, "w") as f:
        json.dump(mine, f)
    got = load_settings(p)
    assert (got["fade"], got["reduce_flash"], got["area"], got["crop_bars"]) == \
        ("Slow", True, "Edges", False), got
    assert (got["bal_r"], got["bal_g"], got["bal_b"]) == (90, 80, 100)
    assert got["hotkeys"]["toggle_power"] == "ctrl+alt+home" and got["hotkeys"]["brightness_up"] == ""
    assert set(got["hotkeys"]) == set(HOTKEY_ACTIONS), "unknown actions must not appear"
    assert got["hotkeys"]["brightness_down"] == DEFAULT_SETTINGS["hotkeys"]["brightness_down"]
    assert got["obs_scenes"] == {"Game": "Focus"}, got["obs_scenes"]
    assert (got["obs_host"], got["obs_port"], got["api_port"], got["api_token"]) == \
        ("pc.local", 4456, 50000, "a" * 32)
    junk = {"fade": "Warp", "area": "Everywhere", "bal_r": 10, "bal_g": 200, "obs_port": 0,
            "api_port": 80, "api_token": "not-hex", "hotkeys": {"toggle_power": "a"},
            "obs_host": "x\n", "obs_scenes": [], "onair": "yes"}
    with open(p, "w") as f:
        json.dump(junk, f)
    got = load_settings(p)
    for k in ("fade", "area", "bal_r", "bal_g", "obs_port", "api_port", "api_token", "obs_host",
              "obs_scenes", "onair"):
        assert got[k] == DEFAULT_SETTINGS[k], (k, got[k])
    assert got["hotkeys"]["toggle_power"] == DEFAULT_SETTINGS["hotkeys"]["toggle_power"]
    assert DEFAULT_SETTINGS["api_on"] is False and DEFAULT_SETTINGS["obs_on"] is False
    assert DEFAULT_SETTINGS["onair"] is False and DEFAULT_SETTINGS["onair_mic"] is False
    assert DEFAULT_SETTINGS["off_on_shutdown"] is True, "lights should go off with the PC"
    for raw, want in (({"off_on_shutdown": False}, False), ({"off_on_shutdown": "no"}, True)):
        with open(p, "w") as f:
            json.dump(raw, f)
        assert load_settings(p)["off_on_shutdown"] is want, raw
    assert not hotkeys.find_conflicts(DEFAULT_SETTINGS["hotkeys"]), "default shortcuts clash"
    assert all(hotkeys.normalize(c) for c in DEFAULT_SETTINGS["hotkeys"].values() if c)
    print("OK  settings: comfort/tools values validated; risky features default off; default "
          "shortcuts valid and distinct")

    # picker: the drawn field and the click mapping agree pixel-for-pixel
    W, H = 300, 180
    rows = [r.strip("{}").split() for r in hs_image_data(W, H).split("} {")]
    assert len(rows) == H and all(len(r) == W for r in rows), "picker image size"
    for x, y in [(0, 0), (W - 1, 0), (W // 3, H // 2), (W - 1, H - 1), (123, 45)]:
        want = hs_rgb(x / (W - 1), 1 - y / (H - 1))
        got = tuple(int(rows[y][x][i:i + 2], 16) for i in (1, 3, 5))
        assert all(abs(a - b) <= 1 for a, b in zip(want, got)), f"({x},{y}) {want}!={got}"
    assert hs_rgb(0, 1) == (255, 0, 0) and hs_rgb(0.5, 0) == (255, 255, 255)
    print("OK  picker field matches click mapping (red top-left, white bottom)")

    # flames: stay in [floor, 1], move smoothly, keep moving; Fire wilder than Candle
    import random
    spans = {}
    for name, params in FLAME_EFFECTS.items():
        f = Flame(*params, rng=random.Random(1))
        lv = [f.step(SEND_INTERVAL, 50) for _ in range(1500)]   # ~2 min of ticks
        assert all(params[0] - 1e-9 <= v <= 1.0 for v in lv), f"{name} out of range"
        jump = max(abs(a - b) for a, b in zip(lv, lv[1:]))
        assert jump < 0.4, f"{name} jumps {jump:.2f} in one tick (not smooth)"
        assert len({round(v, 4) for v in lv}) > 300, f"{name} barely changes"
        spans[name] = float(np.std(lv))
    assert spans["Candle"] < spans["Torch"] < spans["Fire"], f"intensity order {spans}"
    print("OK  flames in range, smooth, alive, Candle<Torch<Fire " +
          str({k: round(v, 3) for k, v in spans.items()}))

    # every software effect: valid output for ~2 min at slow/normal/fast speed
    def ok_out(o):
        if isinstance(o, tuple):
            return len(o) == 3 and all(type(c) is int and 0 <= c <= 255 for c in o)
        return 0.0 <= o <= 1.0
    for name, make in SOFT_EFFECTS.items():
        for sp in (1, 50, 100):
            fx = make()
            if hasattr(fx, "rng"):
                fx.rng = random.Random(3)
            outs = [fx.step(SEND_INTERVAL, sp) for _ in range(1500)]
            assert all(ok_out(o) for o in outs), f"{name}@{sp} bad output"
            assert len(set(outs)) > 1, f"{name}@{sp} never changes"
    b = Breathe()
    lv = [b.step(SEND_INTERVAL, 50) for _ in range(150)]            # 12 s = 2 breaths
    assert max(lv) > 0.95 and min(lv) < 0.15, "Breathe should swing dim <-> full"
    h = Heartbeat()
    lv = [h.step(SEND_INTERVAL, 50) for _ in range(125)]            # 10 s at 60 bpm
    peaks = sum(1 for a, c, d in zip(lv, lv[1:], lv[2:]) if c > a and c >= d and c > 0.6)
    assert 15 <= peaks <= 25, f"Heartbeat should double-pulse ~10x in 10 s, got {peaks}"
    lt = Lightning(rng=random.Random(5))
    outs = [lt.step(SEND_INTERVAL, 50) for _ in range(1500)]
    assert any(o[0] > 200 for o in outs) and outs.count(Lightning.SKY) > 1000, \
        "Lightning should be mostly dark sky with some bright strikes"
    rb = Rainbow()
    hues = {round(colorsys.rgb_to_hsv(*(c / 255 for c in rb.step(SEND_INTERVAL, 50)))[0], 1)
            for _ in range(400)}                                   # 32 s ~ one lap
    assert len(hues) >= 9, f"Rainbow should cover the wheel, saw {sorted(hues)}"
    print("OK  software effects: valid output at all speeds; breathe/heartbeat/lightning/"
          "rainbow behave")

    # the Effects tab / tray grouping covers every app effect exactly once
    grouped = [n for _t, names in SOFT_GROUPS for n in names]
    assert sorted(grouped) == sorted(SOFT_EFFECTS), "SOFT_GROUPS out of step with SOFT_EFFECTS"
    assert not set(SOFT_EFFECTS) & set(EFFECTS), "app and strip effects share a name"

    # Drift: each palette is walked end to end and back, only neighbours blend, fades
    # are smooth (no jumps between 80 ms ticks), and the swell never goes dark
    for name, (pal, swell) in DRIFT_PALETTES.items():
        d = Drift(pal, swell, rng=random.Random(7))
        outs = [d.step(SEND_INTERVAL, 50) for _ in range(3000)]           # ~4 min
        assert all(ok_out(o) for o in outs), f"{name} bad output"
        jump = max(max(abs(a - b) for a, b in zip(o1, o2)) for o1, o2 in zip(outs, outs[1:]))
        assert jump <= 25, f"{name} jumps {jump} in one tick"
        floor = (1 - swell) * 0.8 * min(max(c) for c in pal)   # mid-blend dips ~15-20%
        assert min(max(o) for o in outs) >= floor, f"{name} swells too dark"
        walker, visited = Drift(pal, swell, rng=random.Random(7)), set()
        for _ in range(6000):
            walker.step(SEND_INTERVAL, 50)
            visited.add(walker.i)
            assert abs(walker.i - walker.j) == 1, f"{name} blends non-neighbours"
        assert visited == set(range(len(pal))), f"{name} never reached all its colors"

    # 4-7-8 breathing: 4 s up, 7 s held at full, 8 s down, 19 s per breath
    b = Breath478()
    lv = [b.step(0.05, 50) for _ in range(380)]                            # 19 s
    assert lv[0] < 0.1 and lv[79] > 0.97, "inhale should rise over 4 s"
    assert all(v == 1.0 for v in lv[84:218]), "hold should stay at full for 7 s"
    assert lv[-1] < 0.12 and max(lv[220:]) < 1.0, "exhale should ease down over 8 s"

    # neon: mostly steady, with real dropouts that always recover to bright
    nf = NeonFlicker(rng=random.Random(11))
    lv = [nf.step(SEND_INTERVAL, 50) for _ in range(3000)]
    assert sum(v >= 0.85 for v in lv) > 0.85 * len(lv), "neon should mostly glow steadily"
    assert min(lv) < 0.5 and lv[-1] >= 0.85 or nf.seq, "neon should stutter and recover"
    dips = sum(1 for a, c in zip(lv, lv[1:]) if a >= 0.85 and c < 0.5)
    assert 10 <= dips <= 120, f"neon stutter rate off: {dips} dips in 4 min"

    # fireworks: dark most of the time, bursts in saturated colors, <= ~1 burst/second
    fw = Fireworks(rng=random.Random(5))
    outs = [fw.step(SEND_INTERVAL, 50) for _ in range(3000)]
    bright = [o for o in outs if max(o) > 120]
    assert 0.03 < len(bright) / len(outs) < 0.35, "fireworks should be mostly dark sky"
    assert len({tuple(c > 120 for c in o) for o in bright}) >= 3, "bursts should vary in color"
    rises = sum(1 for a, c in zip(outs, outs[1:]) if max(c) > 120 >= max(a))
    assert 20 <= rises <= 140, f"{rises} bursts in 4 min"

    # party: hops stay at or below ~2.2 per second even at top speed
    for sp in (1, 50, 100):
        pt = Party(rng=random.Random(2))
        hues, last = [], None
        for _ in range(1250):                                             # 100 s
            rgb = pt.step(SEND_INTERVAL, sp)
            if pt.hue != last:
                hues.append(pt.hue)
                last = pt.hue
        assert len(hues) / 100 <= 2.3, f"party hops {len(hues) / 100:.1f}/s at speed {sp}"
    print("OK  new effects: palettes walk neighbours smoothly, 4-7-8 timing, neon stutter, "
          "fireworks bursts, party hop cap")

    # EFFECTS tab previews: every built-in animates with valid colors, in its own hues
    for name in EFFECTS:
        outs = [builtin_preview(name, i * 0.08) for i in range(150)]
        assert all(ok_out(o) for o in outs), f"{name} preview bad color"
        assert len(set(outs)) > 1, f"{name} preview never changes"
    assert (0, 0, 0) in {builtin_preview("Blink Red", i * 0.08) for i in range(30)}
    assert {builtin_preview("Jump RGB", i * 0.08) for i in range(40)} == \
        {(255, 0, 0), (0, 255, 0), (0, 0, 255)}, "Jump RGB should hit exactly R, G, B"
    print("OK  effect previews: all built-ins animate with valid colors")

    # alerts: call wording detected, ordinary messages not; frames valid; notify ends
    for txt in ("Phone Link Incoming call Mom", "WhatsApp Incoming voice call",
                "Teams Alex is calling you", "Discord Ringing...", "Call from +94 77 123"):
        assert is_call(txt), f"missed call: {txt!r}"
    for txt in ("WhatsApp Mom: call me later", "Outlook Meeting recap", "", None):
        assert not is_call(txt), f"false call: {txt!r}"
    nf = [alert_frame("notify", i * SEND_INTERVAL) for i in range(20)]
    assert nf[-1] is None and all(ok_out(f) for f in nf if f), "notify frames"
    assert sum(1 for a, b, c in zip(nf, nf[1:], nf[2:]) if a and b and c
               and b[0] > a[0] and b[0] >= c[0]) == 2, "notify should pulse twice"
    cf = [alert_frame("call", i * SEND_INTERVAL) for i in range(100)]
    assert all(ok_out(f) and f[1] >= f[0] for f in cf), "call frames should be green"
    print("OK  alerts: call detection, two-pulse notify, green call swell")

    # alert shapes for the newer kinds: colored flash, calm (single swell), steady on-air red
    fl = [alert_frame("flash", i * SEND_INTERVAL, (0, 255, 0)) for i in range(20)]
    assert fl[-1] is None and all(f is None or (f[0] == 0 and f[2] == 0) for f in fl), "flash color"
    assert any(f and f[1] > 200 for f in fl), "flash should reach full color"
    calm_n = [alert_frame("notify", i * SEND_INTERVAL, None, True) for i in range(25)]
    assert calm_n[17] is not None and calm_n[18] is None, "calm notify lasts ~1.4 s"
    live = [f for f in calm_n if f]
    peak = max(range(len(live)), key=lambda i: live[i][0])
    assert all(live[i][0] <= live[i + 1][0] for i in range(peak)) and \
        all(live[i][0] >= live[i + 1][0] for i in range(peak, len(live) - 1)), \
        "calm notify should be one smooth swell (up once, down once)"
    assert alert_frame("onair", 0) == ONAIR_RED == alert_frame("onair", 99999)
    assert alert_frame("onair", 5, (1, 2, 3)) == (1, 2, 3)
    print("OK  alerts: colored flash, calm single swell, steady on-air red")

    # reduce flashing, on the real effects: fast ones flash raw, <= 3 per second once softened
    from rgbzap_comfort import FlashLimiter as _FL, max_flashes_per_second as _mfps
    for name in ("Police", "Lightning", "Party", "Neon flicker", "Fireworks"):
        fx = SOFT_EFFECTS[name]()
        if hasattr(fx, "rng"):
            fx.rng = random.Random(4)
        raw = []
        for _ in range(1500):
            o = fx.step(SEND_INTERVAL, 100)
            raw.append(o if isinstance(o, tuple) else (int(255 * o),) * 3)
        lim = _FL()
        soft = [lim.step(f, SEND_INTERVAL) for f in raw]
        assert _mfps(soft, SEND_INTERVAL) <= 3, f"{name} still flashes with Reduce flashing on"
        assert _mfps(soft, SEND_INTERVAL) <= _mfps(raw, SEND_INTERVAL), f"{name} got worse"
        if name in ("Police", "Lightning"):
            assert _mfps(raw, SEND_INTERVAL) >= 3, f"{name} raw should be a flasher"
    print("OK  reduce flashing: Police / Lightning / Party / Neon / Fireworks stay <= 3 flashes/s")

    # screen sync quality: bars ignored, and the chosen area decides the color
    film = np.zeros((1080, 1920, 4), dtype=np.uint8)
    film[..., 3] = 255
    film[140:940, :, 2] = 200                           # red picture between ~13% black bars
    ss = ScreenSync(None, lambda *_: None, lambda *_: None, lambda: 100)
    ss.dark_mode, ss.sat, ss.style, ss.extract_mode = "Keep", 1.0, "Accurate", "Average"
    ss.crop_bars = True
    r_crop = ss._extract(film)[0]
    ss._prev, ss.crop_bars = None, False
    r_raw = ss._extract(film)[0]
    assert r_crop > 190 and r_raw < 160, f"bars should be ignored: {r_crop} vs {r_raw}"
    halves = np.zeros((200, 320, 4), dtype=np.uint8)
    halves[..., 3] = 255
    halves[:, :160, 2] = 255                             # left half red (BGRA: index 2 = R)
    halves[:, 160:, 0] = 255                             # right half blue
    ss.crop_bars = False
    for area, red_wins in (("Left", True), ("Right", False)):
        ss._prev, ss.area = None, area
        r, gg, b, _lvl = ss._extract(halves)
        assert (r > 200 and b < 30) if red_wins else (b > 200 and r < 30), (area, r, gg, b)
    ss._prev, ss.area = None, "Whole screen"
    r, gg, b, _lvl = ss._extract(halves)
    assert 100 < r < 160 and 100 < b < 160, "whole screen should blend both halves"
    ss.calm = True
    ss._prev, ss.alpha = np.array([0.0, 0.0, 0.0]), 1.0   # snappy setting, but Reduce flashing caps it
    r, _gg, _b, _lvl = ss._extract(np.full((50, 80, 4), 255, dtype=np.uint8))
    assert r < 100, f"Reduce flashing should cap sync reaction speed, got {r}"
    print("OK  screen sync: black bars ignored, area picks the right side, calm caps reaction")

    # scenes: starters valid; a bad scene is dropped, good ones kept
    assert all(all(_valid_setting(f, sc[f]) for f in SCENE_FIELDS)
               for sc in DEFAULT_SETTINGS["scenes"].values()), "a starter scene is invalid"
    good_sc = {"color": [1, 2, 3], "brightness": 50, "mode": "effect", "effect": "Rainbow",
               "speed": 70}
    with open(p, "w") as f:
        json.dump({"scenes": {"Mine": good_sc, "Bad": dict(good_sc, effect="Nope"),
                              "": good_sc, "Junk": 5}}, f)
    assert load_settings(p)["scenes"] == {"Mine": good_sc}, "scene validation"
    with open(p, "w") as f:
        json.dump({"scenes": {}}, f)
    assert load_settings(p)["scenes"] == {}, "deleting every scene must stick"
    print("OK  scenes: starters valid, bad scenes dropped, empty list kept")

    # audio: band split, silence, beat detection, and every mode's output (synthetic signals)
    RATE, BLK = 48000, 1024

    def analyse(sig, sens=50):
        an, ring, feats = AudioAnalyzer(RATE), np.zeros(2048, np.float32), []
        for i in range(0, len(sig) - BLK + 1, BLK):
            ring = np.concatenate([ring, sig[i:i + BLK]])[-2048:]
            feats.append(an.feed(ring, BLK / RATE, sens))
        return feats
    tt = np.arange(RATE * 3) / RATE
    bass = analyse((0.5 * np.sin(2 * np.pi * 60 * tt)).astype(np.float32))[-1]
    treble = analyse((0.5 * np.sin(2 * np.pi * 4000 * tt)).astype(np.float32))[-1]
    quiet = analyse(np.zeros(RATE, np.float32))[-1]
    assert bass["bass"] > 0.8 and bass["bass"] > 3 * max(bass["mid"], bass["high"]), bass
    assert treble["high"] > 0.8 and treble["high"] > 3 * max(treble["bass"], treble["mid"]), treble
    assert quiet["silent"] and quiet["bass"] == quiet["level"] == 0.0, quiet
    assert bass["centroid"] < 300 < 3000 < treble["centroid"], "centroid should track pitch"

    # 120 bpm kick (60 Hz thump decaying ~80 ms) over soft hiss, 8 s -> ~16 beats
    t8 = np.arange(RATE * 8) / RATE
    kick = np.sin(2 * np.pi * 60 * t8) * np.exp(-(t8 % 0.5) / 0.08)
    hiss = 0.02 * np.random.default_rng(0).standard_normal(len(t8))
    feats = analyse((0.6 * kick + hiss).astype(np.float32))
    beats = sum(f["beat"] for f in feats)
    assert 13 <= beats <= 17, f"expected ~16 beats at 120 bpm in 8 s, got {beats}"

    ar = AudioReactive(None, None, None, lambda: 100, lambda: (255, 120, 0), None)
    for mode in AUDIO_MODES:
        ar.mode = mode
        ar._reset()
        outs = [ar.render(f, BLK / RATE) for f in feats]
        assert all(len(rgb) == 3 and all(0 <= c <= 255 for c in rgb) and 0 <= lv <= 1
                   for rgb, lv in outs), f"{mode} bad output"
        lv = [o[1] for o in outs]
        assert max(lv) - min(lv) > 0.3, f"{mode} should move with the music"
        assert min(lv) >= ar.floor / 100 - 1e-9, f"{mode} dropped under Min glow"
    ar.mode = "Beat colors"
    ar._reset()
    hues = {ar.render(f, BLK / RATE)[0] for f in feats}
    assert len(hues) >= 8, "Beat colors should change color on beats"
    print(f"OK  audio: bands, silence, {beats} beats at 120 bpm, all 5 modes react")

    # BLE: concurrent connects must yield ONE client; a dropped link reconnects on write
    class _Ch:
        uuid, properties = "0000fff3-0000-1000-8000-00805f9b34fb", ["write-without-response"]

    class _Svc:
        characteristics = [_Ch()]

    class FakeClient:
        made = 0
        def __init__(self, target, timeout=0, disconnected_callback=None):
            FakeClient.made += 1
            self.target = target
            self.is_connected, self.services, self.writes = False, [_Svc()], 0
        async def connect(self):
            await asyncio.sleep(0.05)
            self.is_connected = True
        async def disconnect(self):
            self.is_connected = False
        async def write_gatt_char(self, uuid, data, response=False):
            self.writes += 1
            FakeClient.log.append(bytes(data))
        log = []

    class _Dev:
        def __init__(self, name, address): self.name, self.address = name, address

    class _Adv:
        def __init__(self, name, rssi): self.local_name, self.rssi = name, rssi

    class FakeScanner:
        # one strip, a near-by pair of headphones, a unnamed phone, an unrecognised strip
        nearby = [("ELK-BLEDOM", "AA:01", -80), ("WH-1000XM4", "AA:02", -40),
                  (None, "AA:03", -30), ("MyStrip123", "AA:04", -60)]

        @staticmethod
        async def discover(timeout=5.0, return_adv=False, **_kw):
            if return_adv:
                return {a: (_Dev(n, a), _Adv(n, r)) for n, a, r in FakeScanner.nearby}
            return [_Dev(n, a) for n, a, _r in FakeScanner.nearby]

    global BleakClient, BleakScanner, ADDR_FILE
    real, BleakClient = BleakClient, FakeClient
    real_scanner, BleakScanner = BleakScanner, FakeScanner
    real_addr_file, ADDR_FILE = ADDR_FILE, os.path.join(os.path.dirname(p), "addr.txt")
    try:
        w = BleWorker(lambda *_: None, lambda *_: None)
        w.address = "AA:BB:CC:DD:EE:FF"

        async def burst():
            await asyncio.gather(*(w._connect() for _ in range(5)))
        w.submit(burst()).result(timeout=5)
        assert FakeClient.made == 1, f"{FakeClient.made} clients for 5 concurrent connects"
        w.client.is_connected = False                     # strip drops the link
        w.submit(w._write(b"x")).result(timeout=5)
        assert FakeClient.made == 2 and w.client.writes == 1, "no reconnect on write"
        w.submit(w._disconnect()).result(timeout=5)
        w.submit(w._write(b"x")).result(timeout=5)
        assert FakeClient.made == 2, "wrote after user disconnect (should stay off)"

        # picking a strip in the LIGHT tab: switches the one client, saves the address
        w.submit(w._select("11:22:33:44:55:66")).result(timeout=5)
        old = w.client
        assert w._connected() and old.target == "11:22:33:44:55:66", "select didn't connect"
        w.submit(w._select("AA:BB:CC:DD:EE:FF")).result(timeout=5)
        assert not old.is_connected and w.client.target == "AA:BB:CC:DD:EE:FF", \
            "switching strips must release the old one"
        made = FakeClient.made
        w.submit(w._select("AA:BB:CC:DD:EE:FF")).result(timeout=5)
        assert FakeClient.made == made, "re-picking the connected strip must not reconnect"
        assert open(ADDR_FILE).read() == "AA:BB:CC:DD:EE:FF", "picked strip not saved"

        # alert: plays over the current state, holds sync frames, then restores both
        steady, sync_frame = cmd_color(1, 2, 3), cmd_color(9, 9, 9)

        async def alert_mid_sync():
            await w._write(steady)
            task = asyncio.ensure_future(w._alert("notify", 100))
            await asyncio.sleep(0.3)
            w._set_pending("color", sync_frame)   # a sync frame arrives mid-alert
            assert w._held and "color" not in w._pending, "frame not held during alert"
            await task
            await asyncio.sleep(0.4)              # writer sends the held frame
        FakeClient.log.clear()
        w.submit(alert_mid_sync()).result(timeout=10)
        log = FakeClient.log
        flashes = [p for p in log if p not in (steady, sync_frame, cmd_on())]
        assert len(flashes) >= 10, "alert frames not played"
        last_flash = max(i for i, p in enumerate(log) if p in flashes)
        assert steady in log[last_flash:] and log[-1] == sync_frame, \
            "after the alert: restore the steady color, then the held sync frame"
        assert not w._alert_on and not w._held, "alert state left behind"

        # on-air: steady red for as long as onair_on holds; notifications and calls don't
        # override it; releasing it puts the strip back
        steady2 = cmd_color(7, 8, 9)

        async def onair_run():
            await w._write(steady2)
            w.onair_on = True
            task = asyncio.ensure_future(w._alert("onair", 100, max_s=30))
            await asyncio.sleep(0.9)
            assert w._alert_on and w._alert_kind == "onair"
            await w._alert("notify", 100)               # ignored while on air
            await w._alert("call", 100)                 # a ringing call doesn't override it
            await w._alert("onair", 100)                # a second request is a no-op
            assert w._alert_kind == "onair" and not task.done()
            w.onair_on = False
            await asyncio.wait_for(task, 3)
        FakeClient.log.clear()
        w.submit(onair_run()).result(timeout=10)
        reds = [pl for pl in FakeClient.log if pl[2:4] == b"\x05\x03" and pl[4:7] == bytes((255, 0, 0))]
        assert 1 <= len(reds) <= 4, f"steady red should need few writes, got {len(reds)}"
        assert steady2 in FakeClient.log[-3:] and not w._alert_on, "strip not restored after on-air"
        # an on-air request whose time has passed must not blip the strip
        FakeClient.log.clear()
        w.onair_on = False
        w.submit(w._alert("onair", 100)).result(timeout=5)
        assert not FakeClient.log, "stale on-air request wrote to the strip"

        # color balance is applied on the wire only; what we remember stays uncorrected
        w.balance = (1.0, 0.5, 1.0)
        FakeClient.log.clear()
        w.submit(w._write(cmd_color(200, 100, 50))).result(timeout=5)
        assert FakeClient.log[-1][4:7] == bytes((200, 50, 50)), FakeClient.log[-1]
        assert w.last_color() == (200, 100, 50), "remembered color must be pre-balance"
        w.submit(w._write(cmd_on())).result(timeout=5)
        assert FakeClient.log[-1] == cmd_on(), "non-color frames must pass through untouched"
        w.balance = (1.0, 1.0, 1.0)

        # shutdown: off goes out (twice, in case one is dropped), nothing can relight the
        # strip afterwards, and unfreezing (shutdown cancelled) lets writes through again
        FakeClient.log.clear()
        w.onair_on = True
        t_off = time.monotonic()
        assert w.submit(w._shutdown_off()).result(timeout=5) is True
        took = time.monotonic() - t_off
        assert FakeClient.log == [cmd_off()] * 3 and w.frozen, FakeClient.log
        assert 0.8 <= took < 2.0, f"should wait ~1 s for the radio before answering Windows, {took:.2f}s"
        assert not w.onair_on and w.alert_cancel, "alerts must stop for the shutdown"
        FakeClient.log.clear()                           # the last-chance resend: quick, two writes
        assert w.submit(w._shutdown_off(quick=True)).result(timeout=5) is True
        assert FakeClient.log == [cmd_off()] * 2, FakeClient.log
        FakeClient.log.clear()
        n = len(FakeClient.log)
        w.queue("color", cmd_color(1, 2, 3))
        w.submit(w._write(cmd_color(4, 5, 6))).result(timeout=5)
        time.sleep(0.3)                                   # let the writer have its chance
        assert len(FakeClient.log) == n, "something wrote to the strip after the freeze"
        w.frozen = False
        w.submit(w._write(cmd_color(7, 8, 9))).result(timeout=5)
        assert FakeClient.log[-1] == cmd_color(7, 8, 9), "writes should resume after unfreeze"
        w.client.is_connected = False                      # strip not connected: say so, don't hang
        w._want_connected = False
        assert w.submit(w._shutdown_off()).result(timeout=5) is False
        w.frozen, w._want_connected = False, True
        w.submit(w._connect()).result(timeout=5)

        # the shutdown log: timestamped lines, and it never grows without bound
        import tempfile as _tf
        lp = os.path.join(_tf.mkdtemp(), "shutdown.log")
        shutdown_log("session ending: test", lp)
        shutdown_log("off command sent (900 ms)", lp)
        lines = open(lp, encoding="utf-8").read().splitlines()
        assert len(lines) == 2 and lines[0][4] == "-" and lines[0].endswith("session ending: test")
        with open(lp, "a", encoding="utf-8") as f:
            f.write(("x" * 99 + "\n") * 400)
        shutdown_log("after trim", lp)
        lines = open(lp, encoding="utf-8").read().splitlines()
        assert len(lines) <= 102 and lines[-1].endswith("after trim"), len(lines)
        shutdown_log("never raises", os.path.join(lp, "not", "a", "dir"))

        # scan: normal = known strips only; everything = strips first, then closest
        got = w.submit(w._scan_all()).result(timeout=5)
        assert got == [("ELK-BLEDOM", "AA:01")], f"normal scan changed: {got}"
        got = w.submit(w._scan_all(everything=True)).result(timeout=5)
        assert got == [("ELK-BLEDOM", "AA:01"), ("WH-1000XM4", "AA:02"),
                       ("MyStrip123", "AA:04")], f"all-devices scan order: {got}"
        FakeScanner.nearby = [(f"Dev{i}", f"BB:{i:02d}", -90 + i) for i in range(40)]
        assert len(w.submit(w._scan_all(everything=True)).result(timeout=5)) == 20, "cap"
    finally:
        BleakClient, BleakScanner, ADDR_FILE = real, real_scanner, real_addr_file
    print("OK  BLE: single client under concurrent connects, reconnect, honours disconnect,"
          " strip switching, alert restore, all-devices scan")

    # the feature modules carry their own checks: hotkeys (incl. a real Win32 round trip),
    # On-Air detection, the OBS link against a fake OBS, the local API's security rules, the
    # command-line tool end to end, fades / flash limiter, screen-sync helpers, Tools tab logic
    import rgbzap_cli
    import rgbzap_comfort
    import rgbzap_mcp
    import rgbzap_sync
    for mod in (hotkeys, onair_mod, obs_mod, api_mod, rgbzap_cli, rgbzap_mcp, rgbzap_comfort,
                rgbzap_sync, session_mod, rgbzap_tools):
        mod._selftest()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)

    _lock = acquire_single_instance_lock()
    if _lock is None:
        # Already running (maybe hidden in the tray): ask it to show its window.
        try:
            socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2).close()
            raise SystemExit(0)
        except OSError:
            pass
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

    if sys.platform == "win32":
        # own taskbar identity, so the RGBZap icon shows instead of python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RGBZap.LEDConsole")
    root = tk.Tk()
    App(root, lock=_lock)
    root.mainloop()
