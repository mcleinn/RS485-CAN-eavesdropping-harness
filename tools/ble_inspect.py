"""Read-only Bluetooth LE inspection of a device: scan, connect, list its
services and characteristics, read every readable characteristic,
subscribe to every notify/indicate characteristic and print what arrives.

Nothing is ever written to the device. Used 2026-10-24 on the control board
of our Huawei-based charger (docs/CHARGER-HUAWEI-UNITS.md 3.4).

    pip install bleak
    python -X utf8 tools/ble_inspect.py --name HW            # by name prefix
    python -X utf8 tools/ble_inspect.py --address 80:B5:4E:E6:DD:FA --listen 30

`-X utf8` keeps non-ASCII device names printable on a Windows console.
"""
from __future__ import annotations

import argparse
import asyncio
import time

from bleak import BleakScanner, BleakClient


async def scan(seconds: float, name_prefix: str | None, address: str | None):
    found = {}

    def cb(dev, adv):
        found[dev.address] = (dev, adv)
    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()
    target = None
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        hit = (address and addr.lower() == address.lower()) or \
              (name_prefix and name.startswith(name_prefix))
        print(f"  {addr}  rssi={adv.rssi:4}  name={name!r}  services={list(adv.service_uuids)}  "
              f"mfr={ {k: v.hex() for k, v in adv.manufacturer_data.items()} }"
              + ("  <--" if hit else ""), flush=True)
        if hit:
            target = dev
    return target


async def inspect(target, listen_s: float) -> None:
    print(f"\nconnecting to {target.address} ...", flush=True)
    async with BleakClient(target, timeout=20.0) as cl:
        print("connected:", cl.is_connected, "mtu:", getattr(cl, "mtu_size", "?"))
        notify = []
        for svc in cl.services:
            print(f"service {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                print(f"   char {ch.uuid}  props={ch.properties}  {ch.description}")
                for d in ch.descriptors:
                    print(f"      desc {d.uuid} {d.description}")
                if "read" in ch.properties:
                    try:
                        v = bytes(await cl.read_gatt_char(ch)).rstrip(b"\0")
                        print(f"      read ({len(v)} B): {v.hex(' ')}  {v!r}")
                    except Exception as e:
                        print(f"      read failed: {e!r}")
                if "notify" in ch.properties or "indicate" in ch.properties:
                    notify.append(ch)
        t0 = time.perf_counter()

        def handler(uuid):
            def h(_, data):
                b = bytes(data)
                print(f"+{time.perf_counter() - t0:7.3f}s notify {uuid[:8]}: {b.hex(' ')}  {b!r}",
                      flush=True)
            return h
        for ch in notify:
            try:
                await cl.start_notify(ch, handler(str(ch.uuid)))
                print("subscribed", ch.uuid)
            except Exception as e:
                print("subscribe failed", ch.uuid, repr(e))
        if listen_s > 0:
            print(f"listening {listen_s:.0f} s (read-only) ...", flush=True)
            await asyncio.sleep(listen_s)
        for ch in notify:
            try:
                await cl.stop_notify(ch)
            except Exception:
                pass
    print("disconnected")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", help="device name prefix to connect to")
    ap.add_argument("--address", help="device address to connect to")
    ap.add_argument("--scan", type=float, default=8.0, help="scan seconds (default 8)")
    ap.add_argument("--listen", type=float, default=20.0,
                    help="seconds to listen for notifications after connecting (default 20)")
    a = ap.parse_args()
    print(f"scanning {a.scan:.0f} s ...", flush=True)
    target = await scan(a.scan, a.name, a.address)
    if not (a.name or a.address):
        return 0
    if target is None:
        print("target not found")
        return 1
    await inspect(target, a.listen)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
