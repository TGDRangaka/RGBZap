#!/usr/bin/env python3
"""
Shared BLE protocol for ELK-BLEDOM–style LED strips.

Both the GUI (elk_gui.py) and the CLI (elk_control.py) import from here so the
device protocol lives in exactly one place.

Multi-model support
-------------------
The cheap BLE strips sold under many names (ELK-BLEDOM, MELK, ELK-BULB, XSL-,
"LED LIGHT STRIP", LEDBLE, JACKYLED, …) almost all speak the same classic
9-byte `0x7e .. 0xef` frame set. What actually differs between them is the GATT
*write characteristic*: most expose `fff3`, some expose `ffe1`, a few use a
Nordic-UART service. Rather than hardcode one UUID (which silently fails on the
others), `pick_write_char()` inspects the connected device and picks the first
writable characteristic — preferring the known candidates. This adapts to far
more hardware than a fixed UUID, and still resolves to `fff3` on a classic
ELK-BLEDOM.

ponytail: frames are assumed shared across models (true for the whole fff3/fff4
family). A handful of ffe1 models use a different byte order / on-off value; if
one of those turns up, add a per-model frame set here — not built now because it
can't be verified without that hardware. Protocol reference:
dave-code-ruiz/elkbledom (models.json).

Frames, effect codes, name prefixes and UUIDs are based on dave-code-ruiz/elkbledom,
MIT License, Copyright (c) 2019 — full notice in THIRD_PARTY_NOTICES.md.
"""
import os
import shutil

# Per-user data folder (settings, remembered strip). Not next to the program: an
# installed app in Program Files can't write there.
APP_DIR = os.path.join(os.environ.get("APPDATA") or
                       os.path.join(os.path.expanduser("~"), ".config"), "RGBZap")


def data_file(name, legacy=None):
    """Path of `name` in APP_DIR. On first use, copies `legacy` (where versions
    before 1.0 kept the file, next to the program) so nothing is lost on upgrade.
    Copy, not move: the old file stays as a backup."""
    os.makedirs(APP_DIR, exist_ok=True)
    path = os.path.join(APP_DIR, name)
    if legacy and not os.path.exists(path) and os.path.exists(legacy):
        shutil.copy2(legacy, path)
    return path


# Advertised-name prefixes we treat as controllable strips (lower-case).
NAME_PREFIXES = (
    "elk-btc", "elk-bledom", "elk-bledob", "elk-bleddm", "elk-bledom0e",
    "elk-ble", "elk-bulb", "elk-lampl", "melk", "xsl-", "led light strip",
    "ledble", "led-", "jackyled", "xrocker", "dmrrba",
)

# Write characteristics to prefer, in order. First writable one on the device wins.
WRITE_UUID_CANDIDATES = (
    "0000fff3-0000-1000-8000-00805f9b34fb",  # classic ELK-BLEDOM / MELK / ELK-BULB
    "0000ffe1-0000-1000-8000-00805f9b34fb",  # LEDBLE / JACKYLED / XROCKER family
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e",  # Nordic UART (DMRRBA-007)
)

# Kept for callers that still reference a single default UUID.
WRITE_UUID = WRITE_UUID_CANDIDATES[0]


# ---- protocol frames (classic ELK-BLEDOM 9-byte 0x7e..0xef) ----
def cmd_on():           return bytes([0x7e, 0x00, 0x04, 0xf0, 0x00, 0x01, 0xff, 0x00, 0xef])
def cmd_off():          return bytes([0x7e, 0x00, 0x04, 0x00, 0x00, 0x00, 0xff, 0x00, 0xef])
def cmd_color(r, g, b): return bytes([0x7e, 0x00, 0x05, 0x03, r & 0xff, g & 0xff, b & 0xff, 0x00, 0xef])
def cmd_effect(v):      return bytes([0x7e, 0x00, 0x03, v & 0xff, 0x03, 0x00, 0x00, 0x00, 0xef])
def cmd_effect_speed(v):return bytes([0x7e, 0x00, 0x02, v & 0xff, 0x00, 0x00, 0x00, 0x00, 0xef])
def cmd_white(pct):     return bytes([0x7e, 0x00, 0x01, pct & 0xff, 0x00, 0x00, 0x00, 0x00, 0xef])
def cmd_bright(pct):    return bytes([0x7e, 0x00, 0x01, pct & 0xff, 0x00, 0x00, 0x00, 0x00, 0xef])


# effect name -> value (from the elkbledom integration's EFFECTS class)
EFFECTS = {
    "Jump RGB": 135, "Jump 7-color": 136,
    "Crossfade Red": 139, "Crossfade Green": 140, "Crossfade Blue": 141,
    "Crossfade Yellow": 142, "Crossfade Cyan": 143, "Crossfade Magenta": 144,
    "Crossfade White": 145, "Crossfade Red-Green": 146, "Crossfade Red-Blue": 147,
    "Crossfade Green-Blue": 148, "Crossfade RGB": 137, "Crossfade 7-color": 138,
    "Blink Red": 150, "Blink Green": 151, "Blink Blue": 152, "Blink Yellow": 153,
    "Blink Cyan": 154, "Blink Magenta": 155, "Blink White": 156, "Blink 7-color": 149,
}


def is_strip_name(name):
    """True if an advertised BLE name looks like a controllable strip."""
    n = (name or "").lower()
    return any(n.startswith(p) for p in NAME_PREFIXES)


async def pick_write_char(client):
    """Return the UUID of the best writable characteristic on a connected client,
    or None if the device exposes no writable characteristic (unsupported model)."""
    writable = {}
    for service in client.services:
        for ch in service.characteristics:
            if "write" in ch.properties or "write-without-response" in ch.properties:
                writable[ch.uuid.lower()] = ch.uuid
    for cand in WRITE_UUID_CANDIDATES:
        if cand in writable:
            return writable[cand]
    return next(iter(writable.values()), None)  # any writable char, or None


def _selftest():
    """python elk_protocol.py --selftest — checks characteristic preference."""
    import asyncio

    class Ch:
        def __init__(self, uuid, props): self.uuid, self.properties = uuid, props

    class Svc:
        def __init__(self, chars): self.characteristics = chars

    class Client:
        def __init__(self, chars): self.services = [Svc(chars)]

    FFF3 = WRITE_UUID_CANDIDATES[0]
    FFE1 = WRITE_UUID_CANDIDATES[1]
    other = "0000abcd-0000-1000-8000-00805f9b34fb"

    # ffe1 present but fff3 also present -> prefer fff3 (higher in candidate order)
    c = Client([Ch(FFE1, ["write"]), Ch(FFF3, ["write-without-response"])])
    assert asyncio.run(pick_write_char(c)) == FFF3

    # only a non-candidate writable char -> return it (best-effort)
    c = Client([Ch(other, ["write"]), Ch("0000fff4-...", ["notify"])])
    assert asyncio.run(pick_write_char(c)) == other

    # nothing writable -> None (unsupported model)
    c = Client([Ch("0000fff4-...", ["notify", "read"])])
    assert asyncio.run(pick_write_char(c)) is None
    print("OK  pick_write_char preference + fallback + unsupported")

    # data_file: copies the pre-1.0 file once, never overwrites newer data
    import tempfile
    global APP_DIR
    real, tmp = APP_DIR, tempfile.mkdtemp()
    APP_DIR = os.path.join(tmp, "RGBZap")
    try:
        old = os.path.join(tmp, "elk_settings.json")
        with open(old, "w") as f:
            f.write("old")
        p = data_file("settings.json", legacy=old)
        assert open(p).read() == "old" and os.path.exists(old), "legacy not copied"
        with open(p, "w") as f:
            f.write("new")
        assert open(data_file("settings.json", legacy=old)).read() == "new", "overwrote"
        assert not os.path.exists(data_file("address.txt", legacy=os.path.join(tmp, "x")))
    finally:
        APP_DIR = real
    print("OK  data_file: legacy copied once, newer data kept, missing legacy ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
