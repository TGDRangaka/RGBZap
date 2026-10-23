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

The address is remembered in %APPDATA%/RGBZap/address.txt (shared with the GUI)
after the first successful scan,
so you normally don't need to pass it. You can also set it explicitly:
    python elk_control.py --address XX:XX:XX:XX:XX:XX on
"""
import asyncio
import sys
import os
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

from elk_protocol import (is_strip_name, pick_write_char, cmd_on, cmd_off,
                          cmd_color, cmd_white, cmd_bright, data_file)

# shared with the GUI: %APPDATA%\RGBZap\address.txt
ADDR_FILE = data_file("address.txt", legacy=os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "elk_address.txt"))


async def find_device(timeout=8.0):
    print(f"Scanning for ELK-BLEDOM devices ({timeout:.0f}s)...")
    devices = await BleakScanner.discover(timeout=timeout)
    matches = [d for d in devices if is_strip_name(d.name)]
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
        # cached address may be stale/shared — rescan and try the found strip.
        print("Cached address not advertising, rescanning...")
        address = await cmd_scan()
        if not address:
            return
        target = await BleakScanner.find_device_by_address(address, timeout=12.0)
        if target is None:
            print("Strip not advertising. Make sure it's powered on and the phone")
            print("app is fully closed (only one connection at a time).")
            return
    print(f"Connecting to {address}...")
    async with BleakClient(target, timeout=20.0) as client:
        print("Connected.")
        write_uuid = await pick_write_char(client)
        if write_uuid is None:
            print("This strip model isn't supported yet (no writable characteristic).")
            return
        for p in payloads:
            await client.write_gatt_char(write_uuid, p, response=False)
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
