"""INFO payload encode/decode for Pylontech RS485 commands.

Layouts taken from field-tested reference implementations:
- 6x family (0x61/0x62/0x63): Fahmula/esphome-pylontech-rs485 and
  dalathegreat/Battery-Emulator PYLON-LV-RS485.cpp (verbatim field order,
  incl. duplicated cycles/SOH fields, 0x01xx index tags and deci-Kelvin
  temperatures - details absent from the official PDFs).
- 4x family (0x42/0x47/0x51/0x92/0x93): Frankkkkk/python-pylontech construct
  schemas == machine-readable transcription of the V3.3 spec.

Scaling conventions (NOT uniform across commands):
- voltages: mV (u16) except 0x47 module limits (mV) and 0x42 (mV per cell)
- current: 0x61 centi-amps (i16), 0x63/0x92 deci-amps (i16), 0x42 deci-amps
- temperatures: 6x family deci-Kelvin ((degC+273.15)*10); 4x family K*10
  decoded as (raw-2731)/10 degC
"""
from __future__ import annotations

from dataclasses import dataclass


class HexReader:
    """Cursor over an ASCII-hex INFO string (2 chars = 1 byte, big-endian)."""

    def __init__(self, info: str):
        self.info = info
        self.pos = 0

    @property
    def remaining(self) -> int:
        return (len(self.info) - self.pos) // 2

    def u(self, nbytes: int) -> int:
        s = self.info[self.pos:self.pos + 2 * nbytes]
        if len(s) < 2 * nbytes:
            raise ValueError(f"payload truncated at char {self.pos}")
        self.pos += 2 * nbytes
        return int(s, 16)

    def i16(self) -> int:
        v = self.u(2)
        return v - 0x10000 if v >= 0x8000 else v

    def u8(self) -> int:
        return self.u(1)

    def u16(self) -> int:
        return self.u(2)

    def ascii(self, nbytes: int) -> str:
        raw = bytes.fromhex(self.info[self.pos:self.pos + 2 * nbytes])
        self.pos += 2 * nbytes
        return raw.decode("ascii", "replace").rstrip("\x00 ")


def dk_to_c(raw: int) -> float:
    """deci-Kelvin -> degrees C (6x family temperature encoding)."""
    return round(raw / 10 - 273.15, 1)


def c_to_dk(c: float) -> int:
    # truncation matches the reference C implementations' integer cast;
    # roundtrip accuracy is inherently +-0.1 degC at deci-Kelvin resolution
    return int((c + 273.15) * 10)


def k10_to_c(raw: int) -> float:
    """K*10 -> degC as decoded by python-pylontech for the 4x family."""
    return round((raw - 2731) / 10.0, 1)


def c_to_k10(c: float) -> int:
    return int(round(c * 10 + 2731))


# ---------------------------------------------------------------- 6x family

def build_60(s) -> str:
    """System basic info (V3.5 doc 2.1): battery name (10B ASCII) +
    manufacturer name (20B ASCII) + software version (2B) + battery count
    (1B) + one 16B barcode per battery. This is the RS485 analogue of the
    CAN 0x35E name check some inverters use to accept a battery."""
    name = s.device_name.encode("ascii")[:10].ljust(10, b"\x00")
    manu = s.manufacturer.encode("ascii")[:20].ljust(20, b"\x00")
    barcode = s.serial_number.encode("ascii")[:16].ljust(16, b"\x00")
    return (name.hex().upper() + manu.hex().upper()
            + f"{s.software_version[0]:02X}{s.software_version[1]:02X}"
            + "01" + barcode.hex().upper())


def decode_60(info: str) -> dict:
    r = HexReader(info)
    d = {"battery_name": r.ascii(10), "manufacturer": r.ascii(20),
         "software_version": [r.u8(), r.u8()], "battery_count": r.u8()}
    barcodes = []
    while r.remaining >= 16:
        barcodes.append(r.ascii(16))
    d["barcodes"] = barcodes
    return d


def build_61(s) -> str:
    """System analog data (49 bytes / 98 chars). `s` is a BatteryState."""
    t = c_to_dk(s.temperature_c)
    fields = [
        (s.voltage_mv, 4), (s.current_ca & 0xFFFF, 4), (s.soc_percent, 2),
        (s.cycles, 4), (s.cycles, 4), (s.soh_percent, 2), (s.soh_percent, 2),
        (s.max_cell_mv, 4), (0x0101, 4), (s.min_cell_mv, 4), (0x0108, 4),
        (t, 4), (c_to_dk(s.max_temp_c), 4), (0x0103, 4), (c_to_dk(s.min_temp_c), 4), (0x0109, 4),
        (c_to_dk(s.mosfet_temp_c), 4), (c_to_dk(s.mosfet_temp_c), 4), (0x0101, 4),
        (c_to_dk(s.mosfet_temp_c), 4), (0x0101, 4),
        (c_to_dk(s.bms_temp_c), 4), (c_to_dk(s.bms_temp_c), 4), (0x0101, 4),
        (c_to_dk(s.bms_temp_c), 4), (0x0101, 4),
    ]
    return "".join(f"{v:0{w}X}" for v, w in fields)


def decode_61(info: str) -> dict:
    r = HexReader(info)
    d = {
        "voltage_v": r.u16() / 1000, "current_a": r.i16() / 100,
        "soc_percent": r.u8(), "cycles": r.u16(), "cycles2": r.u16(),
        "soh_percent": r.u8(), "soh2": r.u8(),
        # the 0x0101-style words are module locators per the V3.5 doc:
        # e.g. 0x0304 = module 4 in the battery group with DIP address 3
        "max_cell_v": r.u16() / 1000, "max_cell_module": f"{r.u16():04X}",
        "min_cell_v": r.u16() / 1000, "min_cell_module": f"{r.u16():04X}",
        "avg_cell_temp_c": dk_to_c(r.u16()), "max_cell_temp_c": dk_to_c(r.u16()),
        "max_cell_temp_module": f"{r.u16():04X}",
        "min_cell_temp_c": dk_to_c(r.u16()),
        "min_cell_temp_module": f"{r.u16():04X}",
        "avg_mosfet_temp_c": dk_to_c(r.u16()), "max_mosfet_temp_c": dk_to_c(r.u16()),
        "max_mosfet_temp_module": f"{r.u16():04X}",
        "min_mosfet_temp_c": dk_to_c(r.u16()),
        "min_mosfet_temp_module": f"{r.u16():04X}",
        "avg_bms_temp_c": dk_to_c(r.u16()), "max_bms_temp_c": dk_to_c(r.u16()),
        "max_bms_temp_module": f"{r.u16():04X}",
        "min_bms_temp_c": dk_to_c(r.u16()),
        "min_bms_temp_module": f"{r.u16():04X}",
    }
    return d


ALARM_BITS_62 = [
    # (byte index, bit, name)
    (0, 7, "total_voltage_high_alarm"), (0, 6, "total_voltage_low_alarm"),
    (0, 5, "cell_voltage_high_alarm"), (0, 4, "cell_voltage_low_alarm"),
    (0, 3, "cell_temp_high_alarm"), (0, 2, "cell_temp_low_alarm"),
    (0, 1, "mosfet_temp_high_alarm"), (0, 0, "cell_imbalance_alarm"),
    (1, 7, "cell_temp_imbalance_alarm"), (1, 6, "charge_overcurrent_alarm"),
    (1, 5, "discharge_overcurrent_alarm"), (1, 4, "internal_comm_error"),
    (2, 7, "module_overvoltage_protect"), (2, 6, "module_undervoltage_protect"),
    (2, 5, "cell_overvoltage_protect"), (2, 4, "cell_undervoltage_protect"),
    (2, 3, "cell_overtemp_protect"), (2, 2, "cell_undertemp_protect"),
    (2, 1, "mosfet_overtemp_protect"),
    (3, 6, "charge_overcurrent_protect"), (3, 5, "discharge_overcurrent_protect"),
    (3, 3, "system_fault_protect"),
]


def build_62(s) -> str:
    b = [0, 0, 0, 0]
    for byte_i, bit, name in ALARM_BITS_62:
        if s.alarms.get(name):
            b[byte_i] |= 1 << bit
    return "".join(f"{x:02X}" for x in b)


def decode_62(info: str) -> dict:
    if len(info) < 8:
        return {"error": f"expected 4 bytes, got {len(info) // 2}"}
    r = HexReader(info)
    b = [r.u8() for _ in range(4)]
    d = {"raw_bytes": [f"{x:02X}" for x in b]}
    active = [name for byte_i, bit, name in ALARM_BITS_62 if b[byte_i] & (1 << bit)]
    d["active"] = active
    return d


STATUS_BITS_63 = {7: "charge_enable", 6: "discharge_enable", 5: "charge_immediately",
                  4: "charge_immediately2", 3: "full_charge_request"}


def build_63(s) -> str:
    status = 0
    if s.charge_enable:
        status |= 1 << 7
    if s.discharge_enable:
        status |= 1 << 6
    if s.force_charge:
        status |= 1 << 5
    return (f"{s.charge_voltage_limit_mv:04X}{s.discharge_voltage_min_mv:04X}"
            f"{s.charge_current_limit_da & 0xFFFF:04X}{s.discharge_current_limit_da & 0xFFFF:04X}"
            f"{status:02X}")


def decode_63(info: str) -> dict:
    r = HexReader(info)
    d = {
        "charge_voltage_limit_v": r.u16() / 1000,
        "discharge_voltage_min_v": r.u16() / 1000,
        "charge_current_limit_a": r.i16() / 10,
        "discharge_current_limit_a": r.i16() / 10,
    }
    status = r.u8()
    d["status_raw"] = f"{status:02X}"
    d["status"] = {name: bool(status & (1 << bit)) for bit, name in STATUS_BITS_63.items()}
    return d


# ---------------------------------------------------------------- 4x family

def build_42_single(s, module_number: int = 0) -> str:
    """Analog value for one module, python-pylontech get_values_single layout.
    Leading byte = InfoFlag(=module number here), then module payload."""
    out = [f"{module_number:02X}", f"{module_number:02X}"]  # InfoFlag + NumberOfModule
    out.append(f"{len(s.cell_mv):02X}")
    for mv in s.cell_mv:
        out.append(f"{mv:04X}")
    ntemp = 1 + len(s.group_temps_c)
    out.append(f"{ntemp:02X}")
    out.append(f"{c_to_k10(s.bms_temp_c):04X}")
    for t in s.group_temps_c:
        out.append(f"{c_to_k10(t):04X}")
    out.append(f"{s.current_da & 0xFFFF:04X}")
    out.append(f"{s.voltage_mv:04X}")
    out.append(f"{min(s.remaining_mah, 65535):04X}")
    out.append("02")  # user defined items (<=2 -> 16-bit capacities)
    out.append(f"{min(s.total_mah, 65535):04X}")
    out.append(f"{s.cycles:04X}")
    return "".join(out)


def decode_42(info: str) -> dict:
    """Decode analog value response; handles both the single-module layout
    (InfoFlag + module#) and best-effort multi-module."""
    r = HexReader(info)
    d: dict = {"info_flag": f"{r.u8():02X}"}
    modules = []
    try:
        # Peek: next byte is either NumberOfModules (multi) or module number
        # (single). Heuristic: try single-module first (matches inverter use).
        first = r.u8()
        ncells = r.u8()
        if not (1 <= ncells <= 32):
            # treat `first` as module count for the FF/all-packs variant
            raise ValueError("not single layout")
        m = _decode_42_module(r, ncells)
        m["module_number"] = first
        modules.append(m)
    except (ValueError, IndexError):
        r = HexReader(info)
        d = {"info_flag": f"{r.u8():02X}"}
        try:
            nmod = r.u8()
            for _ in range(nmod):
                ncells = r.u8()
                modules.append(_decode_42_module(r, ncells))
        except (ValueError, IndexError) as e:
            d["decode_error"] = str(e)
    d["modules"] = modules
    return d


def _decode_42_module(r: HexReader, ncells: int) -> dict:
    m: dict = {"n_cells": ncells}
    m["cell_v"] = [r.i16() / 1000 for _ in range(ncells)]
    ntemp = r.u8()
    m["bms_temp_c"] = k10_to_c(r.i16())
    m["group_temps_c"] = [k10_to_c(r.i16()) for _ in range(max(0, ntemp - 1))]
    m["current_a"] = r.i16() / 10
    m["voltage_v"] = r.u16() / 1000
    rem1 = r.u16()
    user_items = r.u8()
    tot1 = r.u16()
    m["cycles"] = r.u16()
    if user_items > 2 and r.remaining >= 6:
        m["remaining_ah"] = r.u(3) / 1000
        m["total_ah"] = r.u(3) / 1000
    else:
        m["remaining_ah"] = rem1 / 1000
        m["total_ah"] = tot1 / 1000
    return m


def build_47(s) -> str:
    """System parameters (fixed limits), python-pylontech layout, prefixed
    with an InfoFlag byte (decoders parse info[1:])."""
    vals = [
        ("u16", s.cell_high_limit_mv), ("u16", s.cell_low_limit_mv),
        ("i16", s.cell_under_limit_mv),
        ("i16", c_to_k10(s.charge_high_temp_c)), ("i16", c_to_k10(s.charge_low_temp_c)),
        ("i16", s.charge_current_limit_da),
        ("u16", s.module_high_limit_mv), ("u16", s.module_low_limit_mv),
        ("u16", s.module_under_limit_mv),
        ("i16", c_to_k10(s.discharge_high_temp_c)), ("i16", c_to_k10(s.discharge_low_temp_c)),
        ("i16", -s.discharge_current_limit_da),
    ]
    out = ["00"]
    for kind, v in vals:
        out.append(f"{v & 0xFFFF:04X}")
    return "".join(out)


def decode_47(info: str) -> dict:
    r = HexReader(info)
    d = {"info_flag": f"{r.u8():02X}"}
    d.update({
        "cell_high_limit_v": r.u16() / 1000, "cell_low_limit_v": r.u16() / 1000,
        "cell_under_limit_v": r.i16() / 1000,
        "charge_high_temp_c": k10_to_c(r.i16()), "charge_low_temp_c": k10_to_c(r.i16()),
        "charge_current_limit_a": r.i16() / 10,
        "module_high_limit_v": r.u16() / 1000, "module_low_limit_v": r.u16() / 1000,
        "module_under_limit_v": r.u16() / 1000,
        "discharge_high_temp_c": k10_to_c(r.i16()), "discharge_low_temp_c": k10_to_c(r.i16()),
        "discharge_current_limit_a": r.i16() / 10,
    })
    return d


def build_51(s) -> str:
    name = s.device_name.encode("ascii")[:10].ljust(10, b"\x00")
    manu = s.manufacturer.encode("ascii")[:20]
    return name.hex().upper() + f"{s.software_version[0]:02X}{s.software_version[1]:02X}" + manu.hex().upper()


def decode_51(info: str) -> dict:
    r = HexReader(info)
    return {
        "device_name": r.ascii(10),
        "software_version": [r.u8(), r.u8()],
        "manufacturer": r.ascii(r.remaining),
    }


def build_92(s, module_number: int = 0) -> str:
    status = 0
    if s.charge_enable:
        status |= 1 << 7
    if s.discharge_enable:
        status |= 1 << 6
    if s.force_charge:
        status |= 1 << 4  # ChargeImmediately1
    return (f"{module_number:02X}{s.charge_voltage_limit_mv:04X}{s.discharge_voltage_min_mv:04X}"
            f"{s.charge_current_limit_da & 0xFFFF:04X}{-s.discharge_current_limit_da & 0xFFFF:04X}"
            f"{status:02X}")


def decode_92(info: str) -> dict:
    r = HexReader(info)
    d = {"command_value": f"{r.u8():02X}",
         "charge_voltage_limit_v": r.u16() / 1000,
         "discharge_voltage_limit_v": r.u16() / 1000,
         "charge_current_limit_a": r.i16() / 10,
         "discharge_current_limit_a": r.i16() / 10}
    status = r.u8()
    d["status_raw"] = f"{status:02X}"
    d["status"] = {
        "charge_enable": bool(status & 0x80), "discharge_enable": bool(status & 0x40),
        "charge_immediately2": bool(status & 0x20), "charge_immediately1": bool(status & 0x10),
        "full_charge_request": bool(status & 0x08),
    }
    return d


def build_93(s, module_number: int = 0) -> str:
    sn = s.serial_number.encode("ascii")[:16].ljust(16, b"\x00")
    return f"{module_number:02X}" + sn.hex().upper()


def decode_93(info: str) -> dict:
    r = HexReader(info)
    return {"command_value": f"{r.u8():02X}", "serial_number": r.ascii(16)}


RESPONSE_DECODERS = {
    0x42: decode_42, 0x47: decode_47, 0x51: decode_51,
    0x60: decode_60, 0x61: decode_61, 0x62: decode_62, 0x63: decode_63,
    0x92: decode_92, 0x93: decode_93,
}


def decode_response_info(request_cid2: int, info: str) -> dict | None:
    """Decode a response INFO given the CID2 of the request it answers."""
    dec = RESPONSE_DECODERS.get(request_cid2)
    if dec is None:
        return None
    try:
        return dec(info)
    except (ValueError, IndexError) as e:
        return {"decode_error": str(e), "info": info}
