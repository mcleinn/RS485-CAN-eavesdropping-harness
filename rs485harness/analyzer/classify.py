"""Classify raw bus bytes: Pylontech ASCII, Modbus RTU, or garbage.

Modbus RTU matters twice here:
- Seplos BMS 3.0 speaks Modbus RTU natively (19200 8N1, slave id 0).
- The official 'Voltronic Inverter and BMS 485' protocol (what Axpert-family
  'LIB' probably is) is a Modbus-style binary protocol at 9600 8N1, common
  node id 0x01, function 0x03 reads of registers 0x0000-0x0076.
"""
from __future__ import annotations


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


MODBUS_EXCEPTIONS = {1: "ILLEGAL FUNCTION", 2: "ILLEGAL DATA ADDRESS",
                     3: "ILLEGAL DATA VALUE", 4: "SLAVE DEVICE FAILURE",
                     6: "SLAVE DEVICE BUSY"}

# Known BMS register maps -> which device/setting a Modbus poll points at
KNOWN_REGISTER_MAPS = (
    (0x0000, 0x00FF, "voltronic-lib",
     "Voltronic 'Inverter and BMS 485' map (= the LIB battery protocol on "
     "Axpert clones)"),
    (0x1000, 0x15FF, "seplos-v3-native",
     "Seplos BMS 3.0 native map (PIA/PIB/... blocks, PC-monitor protocol)"),
)


def register_map_hint(start: int) -> str | None:
    for lo, hi, name, _desc in KNOWN_REGISTER_MAPS:
        if lo <= start <= hi:
            return name
    return None


def find_modbus_frames(data: bytes) -> list[dict]:
    """Scan a byte blob for CRC-valid Modbus RTU frames (any offset/length).
    Returns list of {offset, length, slave, function, hex, [start, count,
    map_hint for request-shaped 0x03/0x04 frames]}."""
    hits = []
    n = len(data)
    i = 0
    while i < n - 3:
        found = None
        # try plausible lengths, shortest response is 5 bytes
        for length in range(5, min(n - i, 256) + 1):
            frame = data[i:i + length]
            crc = frame[-2] | (frame[-1] << 8)
            if crc16_modbus(frame[:-2]) == crc and frame[1] in (
                    0x03, 0x04, 0x06, 0x10, 0x83, 0x84, 0x90):
                found = {"offset": i, "length": length, "slave": frame[0],
                         "function": frame[1], "hex": frame.hex().upper()}
                if frame[1] & 0x80 and length == 5:
                    # Modbus exception response: fn|0x80, exception code
                    found["exception"] = frame[2]
                    found["exception_name"] = MODBUS_EXCEPTIONS.get(
                        frame[2], f"exception 0x{frame[2]:02X}")
                    found["for_function"] = frame[1] & 0x7F
                if length == 8 and frame[1] in (0x03, 0x04, 0x06, 0x10):
                    # request-shaped: slave, func, start hi/lo, count hi/lo
                    start = (frame[2] << 8) | frame[3]
                    found["start"] = f"{start:04X}"
                    found["count"] = (frame[4] << 8) | frame[5]
                    hint = register_map_hint(start)
                    if hint:
                        found["map_hint"] = hint
                break
        if found:
            hits.append(found)
            i += found["length"]
        else:
            i += 1
    return hits


def summarize_modbus(hits: list[dict]) -> str:
    """One line a human can act on: who polls what, and which known map."""
    slaves = sorted({h["slave"] for h in hits})
    regs = sorted({h["start"] for h in hits if "start" in h})
    maps = sorted({h["map_hint"] for h in hits if "map_hint" in h})
    parts = [f"{len(hits)} frame(s)", f"slave id(s) {slaves}"]
    if regs:
        parts.append(f"register(s) 0x{', 0x'.join(regs)}")
    if maps:
        descs = {name: desc for _lo, _hi, name, desc in KNOWN_REGISTER_MAPS}
        parts.append("map: " + "; ".join(f"{m} = {descs[m]}" for m in maps))
    return ", ".join(parts)


def classify_raw(data: bytes) -> dict:
    """Best-effort identification of a non-Pylontech byte blob."""
    result: dict = {"bytes": len(data)}
    if not data:
        result["verdict"] = "empty"
        return result
    modbus = find_modbus_frames(data)
    if modbus:
        result["verdict"] = "modbus-rtu"
        result["modbus_frames"] = modbus
        result["modbus_summary"] = summarize_modbus(modbus)
        maps = sorted({h["map_hint"] for h in modbus if "map_hint" in h})
        if maps:
            result["register_maps"] = maps
        return result
    printable = sum(1 for b in data if 32 <= b < 127)
    zeros = data.count(0)
    ffs = data.count(0xFF)
    result["printable_ratio"] = round(printable / len(data), 2)
    if data.count(0x7E):
        result["verdict"] = "pylontech-fragment"
        result["hint"] = "contains '~' but never completed a frame - partial capture or corrupted frames"
    elif (zeros + ffs) / len(data) > 0.5:
        result["verdict"] = "line-noise"
        result["hint"] = ("mostly 00/FF bytes - typical for wrong baud rate, "
                          "A/B polarity swap, or a floating (unterminated/unbiased) bus")
    elif printable / len(data) > 0.8:
        result["verdict"] = "ascii-unknown"
        result["hint"] = "readable ASCII but not Pylontech-framed (console protocol? different SOI?)"
    else:
        result["verdict"] = "binary-unknown"
        result["hint"] = "binary data, no valid Modbus CRC - possibly wrong baud or unknown protocol"
    return result
