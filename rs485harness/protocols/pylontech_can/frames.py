"""Pylontech low-voltage CAN frame set (11-bit, 500 kbit/s) as spoken by
Pylontech US2000/3000C/5000, Victron "CAN-bus BMS", Dyness DL5.0F
("DYNESS-L"), LuxPower "Luxpower protocol" and Seplos CAN - decode and
encode. Sources: docs/refs/pylontech/can/NOTES.md (spec V2.0 2021),
docs/refs/victron/NOTES.md, docs/refs/dyness/CAN-NOTES.md,
docs/refs/luxpower/NOTES.md. Disagreements between sources are decoded
both ways and marked, never resolved by guessing:

- 0x356 voltage: u16 (Pylontech V2.0) vs s16 (V1.3 / Seplos / Victron) -
  differs only above 327.67 V, irrelevant for 48 V packs; decoded as s16.
- 0x379 installed capacity: u32 Ah (Pylontech) vs u16 Ah (Victron) -
  both values are reported.
- 0x35A pair encoding on the wire: active = low bit of the pair (0x01),
  OK = high bit (0x02), 0xAA = everything OK (diyBMS, dfch candump).
- LV-HUB: id + 0x1000 * string address (29-bit on the wire).
- Dyness native 29-bit frames 0x18BB000x / 0x18AA01xx (Junior Box decoder,
  units partly unknown - raw values are kept beside the decoded ones).
"""
from __future__ import annotations

import struct

from ...core.canbus import CanMsg

ID_LIMITS, ID_SOC, ID_ANALOG, ID_ALARMS, ID_ALARMS_VIC = 0x351, 0x355, 0x356, 0x359, 0x35A
ID_REQUESTS, ID_BRAND, ID_MODEL = 0x35C, 0x35E, 0x35F
ID_KEEPALIVE, ID_INV_IDENT, ID_HEARTBEAT, ID_TURN_OFF = 0x305, 0x307, 0x1001, 0x4800
ID_NAMES = {
    0x305: "inverter_keepalive", 0x306: "inverter_0x306", 0x307: "inverter_ident",
    0x350: "custom_flags", 0x351: "limits", 0x354: "lg_0x354", 0x355: "soc",
    0x356: "analog", 0x359: "alarms", 0x35A: "alarms_pairs", 0x35B: "events",
    0x35C: "requests", 0x35E: "brand", 0x35F: "model", 0x360: "byd_0x360",
    0x370: "name1", 0x371: "name2", 0x372: "modules", 0x373: "cell_extremes",
    0x374: "min_cell_id", 0x375: "max_cell_id", 0x376: "min_temp_id", 0x377: "max_temp_id",
    0x378: "energy", 0x379: "installed_capacity", 0x380: "serial1", 0x381: "serial2",
    0x382: "family", 0x1001: "inverter_heartbeat", 0x4800: "turn_off",
    0x0020: "protocol_change_20", 0x0060: "protocol_change_60",
}
BATTERY_IDS = {0x350, 0x351, 0x354, 0x355, 0x356, 0x359, 0x35A, 0x35B, 0x35C, 0x35E, 0x35F,
               0x360, 0x370, 0x371, 0x372, 0x373, 0x374, 0x375, 0x376, 0x377, 0x378, 0x379,
               0x380, 0x381, 0x382}
INVERTER_IDS = {0x305, 0x306, 0x307, 0x1001, 0x4800, 0x0020, 0x0060}
EXTENSION_IDS = {0x35A, 0x370, 0x371, 0x372, 0x373, 0x374, 0x375, 0x376, 0x377, 0x378, 0x379}
DYNESS_NATIVE = {0x18BB0001: "dyness_status", 0x18BB0002: "dyness_limits",
                 0x18BB0003: "dyness_cell_extremes", 0x18BB0004: "dyness_temp_extremes",
                 0x18BB0005: "dyness_0x18BB0005", 0x18AA01A1: "dyness_cells_1_4",
                 0x18AA01A2: "dyness_cell_5", 0x18AA01A6: "dyness_temps"}
VIC_IDENT = bytes([0x12, 0x34, 0x56, 0x78]) + b"VIC\x00"

PROT_BITS_0 = {1: "cell_or_module_over_voltage", 2: "cell_or_module_under_voltage",
               3: "cell_over_temperature", 4: "cell_under_temperature",
               7: "discharge_over_current"}
PROT_BITS_1 = {0: "charge_over_current", 3: "system_error"}
ALARM_BITS_2 = {1: "cell_or_module_high_voltage", 2: "cell_or_module_low_voltage",
                3: "cell_high_temperature", 4: "cell_low_temperature",
                7: "discharge_high_current"}
ALARM_BITS_3 = {0: "charge_high_current", 3: "internal_communication_fail",
                4: "cell_failure(seplos)", 7: "system_warning(ArminJo)"}
REQUEST_BITS = {7: "charge_enable", 6: "discharge_enable", 5: "force_charge_1",
                4: "force_charge_2", 3: "full_charge_request"}
# 0x35A: (byte, shift) -> name; alarms bytes 0-3, warnings 4-7 same order
PAIR_NAMES = {(0, 0): "general", (0, 2): "high_voltage", (0, 4): "low_voltage",
              (0, 6): "high_temperature", (1, 0): "low_temperature",
              (1, 2): "high_temperature_charge", (1, 4): "low_temperature_charge",
              (1, 6): "high_current", (2, 0): "high_charge_current", (2, 2): "contactor",
              (2, 4): "short_circuit", (2, 6): "bms_internal", (3, 0): "cell_imbalance"}
PAIR_ACTIVE, PAIR_OK = 0b01, 0b10


def _u16(d: bytes, off: int) -> int | None:
    return struct.unpack_from("<H", d, off)[0] if len(d) >= off + 2 else None


def _s16(d: bytes, off: int) -> int | None:
    return struct.unpack_from("<h", d, off)[0] if len(d) >= off + 2 else None


def _u32(d: bytes, off: int) -> int | None:
    return struct.unpack_from("<I", d, off)[0] if len(d) >= off + 4 else None


def _ascii(d: bytes) -> str:
    return bytes(d).rstrip(b"\0").decode("ascii", "replace")


def _scaled(v: int | None, div: float, nd: int = 2):
    return None if v is None else round(v / div, nd)


def decode_pairs(d: bytes) -> dict:
    """0x35A: alarms (bytes 0-3) and warnings (bytes 4-7) as 2-bit pairs."""
    out = {"alarms": [], "warnings": [], "not_supported": [], "raw_hex": bytes(d).hex().upper()}
    for (byte, shift), name in PAIR_NAMES.items():
        for group, base in (("alarms", 0), ("warnings", 4)):
            if len(d) <= byte + base:
                continue
            pair = (d[byte + base] >> shift) & 3
            if pair == PAIR_ACTIVE:
                out[group].append(name)
            elif pair != PAIR_OK and group == "alarms":
                out["not_supported"].append(name)
    if len(d) >= 8:
        sysstat = (d[7] >> 2) & 3
        out["system_online"] = {PAIR_ACTIVE: True, PAIR_OK: False}.get(sysstat)
    return out


def split_lvhub(can_id: int, ext: bool) -> tuple[int, int]:
    """(base id, string address) - LV-HUB sends id + 0x1000 * address."""
    if ext and (can_id & 0xFFF) in BATTERY_IDS | INVERTER_IDS and 1 <= (can_id >> 12) <= 7:
        return can_id & 0xFFF, can_id >> 12
    return can_id, 0


def decode(msg: CanMsg) -> dict | None:
    """Decode a Pylontech-family frame (or a Dyness native one); None if
    the identifier belongs to none of them."""
    if msg.err:
        return None
    d = bytes(msg.data)
    if msg.ext and msg.can_id in DYNESS_NATIVE:
        return _decode_dyness(msg.can_id, d)
    base, string = split_lvhub(msg.can_id, msg.ext)
    if base not in ID_NAMES:
        return None
    out: dict = {"proto": "pylontech-can", "id": f"0x{base:03X}", "name": ID_NAMES[base],
                 "dlc": len(d)}
    if string:
        out["string"] = string
    if msg.ext and not string:
        out["ext"] = True            # 11-bit id sent as 29-bit (Dyness Junior Box quirk?)
    out["flow"] = "INV->BMS" if base in INVERTER_IDS else "BMS->INV"
    if base == ID_LIMITS:
        out.update({"cvl_v": _scaled(_u16(d, 0), 10, 1), "ccl_a": _scaled(_s16(d, 2), 10, 1),
                    "dcl_a": _scaled(_s16(d, 4), 10, 1), "dvl_v": _scaled(_u16(d, 6), 10, 1)})
        if len(d) < 8:
            out["note"] = f"DLC {len(d)}: no DVL"
    elif base == ID_SOC:
        out.update({"soc_percent": _u16(d, 0), "soh_percent": _u16(d, 2)})
        hr = _u16(d, 4)
        if hr not in (None, 0):
            out["soc_hires_percent"] = round(hr / 100, 2)
    elif base == ID_ANALOG:
        out.update({"voltage_v": _scaled(_s16(d, 0), 100), "current_a": _scaled(_s16(d, 2), 10, 1),
                    "temperature_c": _scaled(_s16(d, 4), 10, 1)})
    elif base == ID_ALARMS:
        prot = [n for b, n in PROT_BITS_0.items() if len(d) > 0 and d[0] >> b & 1]
        prot += [n for b, n in PROT_BITS_1.items() if len(d) > 1 and d[1] >> b & 1]
        al = [n for b, n in ALARM_BITS_2.items() if len(d) > 2 and d[2] >> b & 1]
        al += [n for b, n in ALARM_BITS_3.items() if len(d) > 3 and d[3] >> b & 1]
        out.update({"protections": prot, "alarms": al})
        if len(d) > 4:
            out["module_count"] = d[4]
        if len(d) > 6:
            out["marker"] = d[5:7].decode("ascii", "replace")
            if d[5:7] != b"PN":
                out["note"] = "bytes 5-6 are not 'P','N'"
        if len(d) > 7 and d[7]:
            out["online_bitmap(seplos)"] = f"0x{d[7]:02X}"
    elif base == ID_ALARMS_VIC:
        out.update(decode_pairs(d))
    elif base == ID_REQUESTS:
        b0 = d[0] if d else 0
        out.update({n: bool(b0 >> b & 1) for b, n in REQUEST_BITS.items()})
        if len(d) < 2:
            out["note"] = f"DLC {len(d)}"
    elif base == ID_BRAND:
        out["brand"] = _ascii(d).strip()
        out["brand_padded"] = d.hex().upper()
    elif base == ID_MODEL:
        out.update({"model_u16": _u16(d, 0),
                    "firmware_be": struct.unpack_from(">H", d, 2)[0] if len(d) >= 4 else None,
                    "online_capacity_ah": _u16(d, 4)})
    elif base in (0x370, 0x371, 0x374, 0x375, 0x376, 0x377, 0x380, 0x381, 0x382):
        out["text"] = _ascii(d)
    elif base == 0x372:
        out.update({"modules_ok": _u16(d, 0), "modules_blocking_charge": _u16(d, 2),
                    "modules_blocking_discharge": _u16(d, 4), "modules_offline": _u16(d, 6)})
    elif base == 0x373:
        out.update({"min_cell_mv": _u16(d, 0), "max_cell_mv": _u16(d, 2),
                    "min_temp_k": _u16(d, 4), "max_temp_k": _u16(d, 6)})
        if out["min_temp_k"]:
            out["min_temp_c"] = round(out["min_temp_k"] - 273.15, 1)
        if out["max_temp_k"]:
            out["max_temp_c"] = round(out["max_temp_k"] - 273.15, 1)
    elif base == 0x378:
        out.update({"energy_in_kwh": _scaled(_u32(d, 0), 10, 1),
                    "energy_out_kwh": _scaled(_u32(d, 4), 10, 1)})
    elif base == 0x379:
        out.update({"installed_ah_u16": _u16(d, 0), "installed_ah_u32": _u32(d, 0)})
    elif base == ID_KEEPALIVE:
        out["all_zero"] = not any(d)
        if d and d[0]:
            out["note"] = f"byte0=0x{d[0]:02X} (ArminJo saw 0x21 from one inverter)"
    elif base == ID_INV_IDENT:
        out["ident_ascii"] = _ascii(d[4:7]) if len(d) >= 7 else ""
        out["victron"] = d[:8] == VIC_IDENT or (len(d) >= 7 and d[4:7] == b"VIC")
        out["all_zero"] = not any(d)
    elif base == 0x350:
        b0, b1 = (d[0] if d else 0), (d[1] if len(d) > 1 else 0)
        out.update({"charge_mosfet_failure": bool(b0 & 0x40), "discharge_mosfet_failure": bool(b0 & 0x80),
                    "soc_spread_ge_25": bool(b1 & 0x40), "float_charge_request": bool(b1 & 0x80)})
    return out


def _decode_dyness(can_id: int, d: bytes) -> dict:
    out: dict = {"proto": "dyness-native", "id": f"0x{can_id:08X}", "name": DYNESS_NATIVE[can_id],
                 "dlc": len(d), "flow": "BMS->INV", "raw_hex": d.hex().upper()}
    if can_id == 0x18BB0001:
        out.update({"voltage_v": _scaled(_u16(d, 0), 100), "current_a": _scaled(_s16(d, 2), 10, 1),
                    "soc_percent": _u16(d, 4), "soh_percent": _u16(d, 6)})
    elif can_id == 0x18BB0002:
        out.update({"ccl_raw": _u16(d, 4), "dcl_raw": _u16(d, 6),
                    "ccl_a_if_0.1": _scaled(_u16(d, 4), 10, 1), "dcl_a_if_0.1": _scaled(_u16(d, 6), 10, 1),
                    "note": "units of bytes 4-7 not documented"})
    elif can_id == 0x18BB0003:
        out.update({"max_cell_raw": _u16(d, 0), "min_cell_raw": _u16(d, 4),
                    "note": "unit not documented (mV assumed by the decoder source)"})
    elif can_id == 0x18BB0004:
        out.update({"max_temp_raw": _u16(d, 0), "min_temp_raw": _u16(d, 4)})
    elif can_id == 0x18AA01A1:
        out["cells_mv"] = [_u16(d, i) for i in (0, 2, 4, 6) if _u16(d, i) is not None]
    elif can_id == 0x18AA01A2:
        out["cell5_mv"] = _u16(d, 0)
    elif can_id == 0x18AA01A6:
        out.update({"bms_temp_max_raw": _u16(d, 0), "bms_temp_min_raw": _u16(d, 2),
                    "radiator_temp_raw": _u16(d, 4), "air_temp_raw": _u16(d, 6)})
    return out


# ----------------------------------------------------------------- encode
def _frame(can_id: int, data: bytes, ext: bool = False, string: int = 0) -> CanMsg:
    if string:
        can_id, ext = can_id + 0x1000 * string, True
    return CanMsg(0.0, can_id, data, ext=ext)


def encode_limits(cvl_v: float, ccl_a: float, dcl_a: float, dvl_v: float | None = None,
                  dlc: int = 8, **kw) -> CanMsg:
    data = struct.pack("<Hhh", int(round(cvl_v * 10)), int(round(ccl_a * 10)), int(round(dcl_a * 10)))
    data += struct.pack("<H", int(round((dvl_v or 0) * 10)))
    return _frame(ID_LIMITS, data[:dlc], **kw)


def encode_soc(soc: int, soh: int, dlc: int = 4, hires: float | None = None, **kw) -> CanMsg:
    data = struct.pack("<HH", soc, soh) + struct.pack("<H", int(round((hires or 0) * 100))) + b"\0\0"
    return _frame(ID_SOC, data[:dlc], **kw)


def encode_analog(voltage_v: float, current_a: float, temp_c: float, dlc: int = 6, **kw) -> CanMsg:
    data = struct.pack("<hhh", int(round(voltage_v * 100)), int(round(current_a * 10)),
                       int(round(temp_c * 10))) + b"\0\0"
    return _frame(ID_ANALOG, data[:dlc], **kw)


def encode_alarms(protections=(), alarms=(), module_count: int = 1, dlc: int = 7,
                  online_bitmap: int = 0, **kw) -> CanMsg:
    b = bytearray(8)
    for bit, name in PROT_BITS_0.items():
        if name in protections:
            b[0] |= 1 << bit
    for bit, name in PROT_BITS_1.items():
        if name in protections:
            b[1] |= 1 << bit
    for bit, name in ALARM_BITS_2.items():
        if name in alarms:
            b[2] |= 1 << bit
    for bit, name in ALARM_BITS_3.items():
        if name in alarms:
            b[3] |= 1 << bit
    b[4] = module_count & 0xFF
    b[5:7] = b"PN"
    b[7] = online_bitmap & 0xFF
    return _frame(ID_ALARMS, bytes(b[:dlc]), **kw)


def encode_pairs(alarms=(), warnings=(), supported=None, online: bool = True, **kw) -> CanMsg:
    """0x35A with the wire encoding active = 0b01, OK = 0b10, n/a = 0b00."""
    b = bytearray(8)
    supported = set(PAIR_NAMES.values()) - {"general", "contactor", "short_circuit"} \
        if supported is None else set(supported)
    for (byte, shift), name in PAIR_NAMES.items():
        if name not in supported:
            continue
        b[byte] |= (PAIR_ACTIVE if name in alarms else PAIR_OK) << shift
        b[byte + 4] |= (PAIR_ACTIVE if name in warnings else PAIR_OK) << shift
    b[7] |= (PAIR_ACTIVE if online else PAIR_OK) << 2
    return _frame(ID_ALARMS_VIC, bytes(b), **kw)


def encode_requests(charge_enable=True, discharge_enable=True, force_charge_1=False,
                    force_charge_2=False, full_charge_request=False, dlc: int = 2, **kw) -> CanMsg:
    flags = {"charge_enable": charge_enable, "discharge_enable": discharge_enable,
             "force_charge_1": force_charge_1, "force_charge_2": force_charge_2,
             "full_charge_request": full_charge_request}
    b0 = 0
    for bit, name in REQUEST_BITS.items():
        if flags[name]:
            b0 |= 1 << bit
    return _frame(ID_REQUESTS, bytes([b0, 0, 0, 0, 0, 0, 0, 0])[:dlc], **kw)


def encode_text(can_id: int, text: str, pad: bytes = b"\0", **kw) -> CanMsg:
    return _frame(can_id, text.encode("ascii", "replace")[:8].ljust(8, pad), **kw)


def encode_brand(brand: str, **kw) -> CanMsg:
    return encode_text(ID_BRAND, brand, pad=b" " if brand.startswith("PYLON") else b"\0", **kw)


def encode_model(model: int = 0, firmware: int = 0, online_ah: int = 0, **kw) -> CanMsg:
    return _frame(ID_MODEL, struct.pack("<H", model) + struct.pack(">H", firmware)
                  + struct.pack("<H", online_ah) + b"\0\0", **kw)


def encode_modules(ok: int, blocking_charge: int = 0, blocking_discharge: int = 0,
                   offline: int = 0, **kw) -> CanMsg:
    return _frame(0x372, struct.pack("<4H", ok, blocking_charge, blocking_discharge, offline), **kw)


def encode_cell_extremes(min_mv: int, max_mv: int, min_c: float, max_c: float, **kw) -> CanMsg:
    return _frame(0x373, struct.pack("<4H", min_mv, max_mv, int(round(min_c + 273.15)),
                                     int(round(max_c + 273.15))), **kw)


def encode_installed(ah: int, width: str = "u32", **kw) -> CanMsg:
    data = struct.pack("<I", ah) + b"\0\0\0\0" if width == "u32" else struct.pack("<H", ah) + bytes(6)
    return _frame(0x379, data, **kw)


def encode_keepalive(**kw) -> CanMsg:
    return _frame(ID_KEEPALIVE, bytes(8), **kw)


def encode_inverter_ident(victron: bool = True, **kw) -> CanMsg:
    return _frame(ID_INV_IDENT, VIC_IDENT if victron else bytes(8), **kw)


def encode_dyness_native(voltage_v: float, current_a: float, soc: int, soh: int,
                         cells_mv: list[int], ccl_a: float, dcl_a: float,
                         max_c: float, min_c: float) -> list[CanMsg]:
    cells = list(cells_mv) + [0] * 5
    return [
        CanMsg(0.0, 0x18BB0001, struct.pack("<HhHH", int(round(voltage_v * 100)),
                                            int(round(current_a * 10)), soc, soh), ext=True),
        CanMsg(0.0, 0x18BB0002, bytes(4) + struct.pack("<HH", int(round(ccl_a * 10)),
                                                       int(round(dcl_a * 10))), ext=True),
        CanMsg(0.0, 0x18BB0003, struct.pack("<HHHH", max(cells_mv), 0, min(cells_mv), 0), ext=True),
        CanMsg(0.0, 0x18BB0004, struct.pack("<HHHH", int(round(max_c * 10)), 0,
                                            int(round(min_c * 10)), 0), ext=True),
        CanMsg(0.0, 0x18AA01A1, struct.pack("<4H", *cells[:4]), ext=True),
        CanMsg(0.0, 0x18AA01A2, struct.pack("<H", cells[4]) + bytes(6), ext=True),
    ]
