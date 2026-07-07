#!/usr/bin/env python3
"""
Standalone ELK-BLEDOM LED strip controller for Windows/Mac/Linux.

Controls ELK-BLEDOM BLE LED strips (the ones sold with the "duoCo Strip" /
"Lotus Lantern" apps) directly from your laptop, no Home Assistant required.

Protocol bytes are taken from the dave-code-ruiz/elkbledom HA integration
(custom_components/elkbledom/models.json, ELK-BLEDOM model).

Usage:
    python elk_control.py scan                 # find your strip, print its address
    python elk_control.py on
    python elk_control.py off
    python elk_control.py color 255 0 0        # R G B  (0-255 each)
    python elk_control.py white 80             # brightness 0-100 (warm white)
    python elk_control.py bright 50            # brightness 0-100
    python elk_control.py demo                 # on -> cycle a few colors -> off

The address is remembered in elk_address.txt after the first successful scan,
so you normally don't need to pass it. You can also set it explicitly:
    python elk_control.py --address XX:XX:XX:XX:XX:XX on
"""
import asyncio
import sys
import os
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
NAME_PREFIXES = ("elk-bledom", "elk-bledob", "elk-bleddm", "melk", "ledble", "elk-ble")
ADDR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elk_address.txt")

# --- Command builders (classic ELK-BLEDOM 0x7e..0xef 9-byte frames) ---
def cmd_on():        return bytes([0x7e, 0x00, 0x04, 0xf0, 0x00, 0x01, 0xff, 0x00, 0xef])
def cmd_off():       return bytes([0x7e, 0x00, 0x04, 0x00, 0x00, 0x00, 0xff, 0x00, 0xef])
def cmd_color(r,g,b):return bytes([0x7e, 0x00, 0x05, 0x03, r & 0xff, g & 0xff, b & 0xff, 0x00, 0xef])
def cmd_white(pct):  return bytes([0x7e, 0x00, 0x01, pct & 0xff, 0x00, 0x00, 0x00, 0x00, 0xef])
def cmd_bright(pct): return bytes([0x7e, 0x00, 0x01, pct & 0xff, 0x00, 0x00, 0x00, 0x00, 0xef])


async def find_device(timeout=8.0):
    print(f"Scanning for ELK-BLEDOM devices ({timeout:.0f}s)...")
    devices = await BleakScanner.discover(timeout=timeout)
    matches = []
    for d in devices:
        name = (d.name or "").lower()
        if any(name.startswith(p) for p in NAME_PREFIXES):
            matches.append(d)
    return matches, devices


async def cmd_scan():
    matches, all_devs = await find_device()
    if not matches:
        print(f"\nNo ELK-BLEDOM-type device found among {len(all_devs)} BLE devices.")
        print("Tips: make sure the strip is powered on, and CLOSE the duoCo Strip")
        print("app on your phone (only one device can connect at a time).")
        print("\nAll BLE devices seen:")
        for d in all_devs:
            print(f"  {d.address}  {d.name!r}")
        return None
    print("\nFound:")
    for d in matches:
        print(f"  {d.address}  {d.name!r}")
    addr = matches[0].address
    with open(ADDR_FILE, "w") as f:
        f.write(addr)
    print(f"\nSaved address {addr} to {ADDR_FILE}")
    return addr


def load_address():
    if os.path.exists(ADDR_FILE):
        return open(ADDR_FILE).read().strip()
    return None


async def send(address, payloads):
    if not address:
        address = await cmd_scan()
        if not address:
            return
    # On Windows, connecting by raw address only works if the device was freshly
    # discovered — resolve a BLEDevice via a scan first, then connect to it.
    print(f"Locating {address}...")
    target = await BleakScanner.find_device_by_address(address, timeout=12.0)
    if target is None:
        print("Strip not advertising. Make sure it's powered on and the duoCo")
        print("Strip phone app is fully closed (only one connection at a time).")
        return
    print(f"Connecting to {address}...")
    async with BleakClient(target, timeout=20.0) as client:
        print("Connected.")
        for p in payloads:
            await client.write_gatt_char(WRITE_UUID, p, response=False)
            print("  sent:", " ".join(f"{b:02x}" for b in p))
            await asyncio.sleep(0.15)
    print("Done.")


async def main():
    args = sys.argv[1:]
    address = None
    if "--address" in args:
        i = args.index("--address")
        address = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return
    if address is None:
        address = load_address()

    action = args[0]
    if action == "scan":
        await cmd_scan()
    elif action == "on":
        await send(address, [cmd_on()])
    elif action == "off":
        await send(address, [cmd_off()])
    elif action == "color":
        r, g, b = (int(x) for x in args[1:4])
        await send(address, [cmd_on(), cmd_color(r, g, b)])
    elif action == "white":
        pct = int(args[1]) if len(args) > 1 else 100
        await send(address, [cmd_on(), cmd_white(pct)])
    elif action == "bright":
        pct = int(args[1]) if len(args) > 1 else 100
        await send(address, [cmd_bright(pct)])
    elif action == "demo":
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 255, 255)]
        payloads = [cmd_on()]
        for c in colors:
            payloads.append(cmd_color(*c))
        await send(address, payloads)
    else:
        print(f"Unknown command: {action}\n")
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
