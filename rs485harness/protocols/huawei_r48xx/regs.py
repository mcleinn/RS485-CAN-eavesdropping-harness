"""Huawei R48xx registers: status burst (0x40), setpoints (0x80), info (0x50),
e-label (0xD2), unsolicited (0x11) - decode and encode.

Sources and disagreements: docs/refs/huawei/NOTES.md sections 4-7. Where
sources disagree the code carries every reading as data:

- voltage scale: V x 1024 (majority, verified by error-ack probing);
- current scale: a named policy (CURRENT_SCALES), never a constant -
  `ratio1024` (A / nominal x 1024, nominal from info 0x0001; verified on
  three models by one source), `x20` and `x30` (other sources). The tool
  establishes which one a unit obeys before it sends any current setpoint.
- no-command timeout ~60 s (one source ~30 s): ONLINE_TIMEOUT_S.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...core.canbus import CanMsg
from .ids import (HuaweiId, CMD_DATA, CMD_INFO, CMD_SET, CMD_GET, CMD_ELABEL,
                  CMD_UNSOL, CMD_ADDR, CMD_CFG, request_id, reply_id)

ONLINE_TIMEOUT_S = 60.0
SCALE_1024 = 1024

# status registers (0x40 burst): reg -> (name, unit, divisor)
STATUS_REGS = {
    0x0E: ("operating_hours", "h", 1),
    0x70: ("ac_in_power_w", "W", SCALE_1024),
    0x71: ("ac_in_freq_hz", "Hz", SCALE_1024),
    0x72: ("ac_in_current_a", "A", SCALE_1024),
    0x73: ("dc_out_power_w", "W", SCALE_1024),
    0x74: ("efficiency", "", SCALE_1024),
    0x75: ("out_voltage_v", "V", SCALE_1024),
    0x76: ("current_limit_ratio", "", SCALE_1024),   # ratio of nominal (see NOTES 5.2)
    0x77: ("power_available_ratio", "", SCALE_1024),
    0x78: ("ac_in_voltage_v", "V", SCALE_1024),
    0x7C: ("dc_bus_voltage_v", "V", SCALE_1024),
    0x7E: ("pfc_temp_c", "degC", SCALE_1024),
    0x7F: ("out_temp_c", "degC", SCALE_1024),
    0x80: ("in_temp_c", "degC", SCALE_1024),
    0x81: ("out_current_a", "A", SCALE_1024),          # fast / unfiltered
    0x82: ("out_current_filtered_a", "A", SCALE_1024),
    0x83: ("status_bits", "", 1),
    0x87: ("fan_status", "", 1),
}
STATUS_BITS = {        # of the 32-bit value in bytes 4-7 (sources name few bits)
    1 << 9: "standby-via-pin",          # BL comment (Peter): 0x00000200
    1 << 20: "fan-error?",              # ES page 10: 00 00 10 00 seen, poster's guess
}
STATUS_BITS_HI = {     # of the 32-bit value in bytes 2-5 (esphome)
    1 << 29: "input-power-failure",
}

# set registers (0x80): name -> (reg, layout, unit)
#   layout "u32": value big-endian in bytes 4-7 (low 16 bits = bytes 6-7)
#   layout "flag3": byte 3 = 0/1
SET_REGS = {
    "online_voltage": (0x00, "u32", "V"),
    "default_voltage": (0x01, "u32", "V"),
    "ovp_voltage": (0x02, "u32", "V"),
    "online_current": (0x03, "u32", "A"),
    "default_current": (0x04, "u32", "A"),
    "ac_current_limit": (0x09, "u32", "A"),      # not on R4850G2
    "fan_duty": (0x14, "u16@2", "%"),            # bytes 2-3 = fraction x 25600; not on R4850G2
    "standby": (0x32, "flag3", ""),
    "fan_max": (0x34, "flag3", ""),
    # 0x108081FE 01 13 00 01 .. : broadcast on cmd 0x81; in a power-up candump
    # (CP) and, measured 2026-10-24, every 3 s from a touchscreen front-end
    # controller. Purpose not documented anywhere; decoded, never sent.
    "config_13": (0x13, "flag3", ""),
}
SET_REG_BY_NUM = {v[0]: k for k, v in SET_REGS.items()}
# accepted ranges found by error-ack probing (NOTES 5.1): (min, max) volts
VOLTAGE_RANGES = {"online_voltage": (41.0, 58.6), "default_voltage": (48.0, 58.4),
                  "ovp_voltage": (56.5, 60.5)}
CURRENT_RATIO_MAX = 1250 / 1024          # "this field goes to 1250" (PG)
ACK_ERROR_BYTE = 0x21


# ------------------------------------------------------------ current scale
@dataclass(frozen=True)
class CurrentScale:
    name: str
    needs_nominal: bool
    source: str

    def encode(self, amps: float, nominal_a: float | None) -> int:
        if self.name == "ratio1024":
            if not nominal_a:
                raise ValueError("ratio1024 scale needs the nominal current (info reg 0x0001)")
            return int(round(amps / nominal_a * 1024))
        if self.name == "x20":
            return int(round(amps * 20))
        if self.name == "x30":
            return int(round(amps * 30))
        raise ValueError(self.name)

    def decode(self, raw: int, nominal_a: float | None) -> float:
        if self.name == "ratio1024":
            if not nominal_a:
                raise ValueError("ratio1024 scale needs the nominal current")
            return raw / 1024 * nominal_a
        return raw / (20 if self.name == "x20" else 30)


CURRENT_SCALES = {
    "ratio1024": CurrentScale("ratio1024", True,
                              "patagonaa (R4830S1/R4850G2/R4850G6), esphome, Huawei R100030G1 guide"),
    "x20": CurrentScale("x20", False, "haklein, ele-dev, mjpalmowski R4850G2.YAML, beyondlogic comment"),
    "x30": CurrentScale("x30", False, "BotoX, craigpeacock Protocol.xlsx"),
}


# ----------------------------------------------------------------- encode
def encode_set_data(name: str, value: float, nominal_a: float | None = None,
                    scale: str = "ratio1024") -> tuple[bytes, int]:
    """Data bytes for a 0x80 set frame and the raw value sent. Voltages in
    V, currents in A (converted by the named scale), flags 0/1, fan duty %."""
    reg, layout, unit = SET_REGS[name]
    if unit == "V":
        raw = int(round(value * SCALE_1024))
    elif unit == "A":
        raw = CURRENT_SCALES[scale].encode(value, nominal_a)
    elif layout == "u16@2":
        raw = int(round(value / 100 * 25600))
    else:
        raw = 1 if value else 0
    if layout == "u32":
        data = bytes([0x01, reg, 0, 0]) + raw.to_bytes(4, "big")
    elif layout == "flag3":
        data = bytes([0x01, reg, 0, raw, 0, 0, 0, 0])
    else:
        data = bytes([0x01, reg]) + raw.to_bytes(2, "big") + bytes(4)
    return data, raw


def set_frame(name: str, value: float, addr: int = 1, nominal_a: float | None = None,
              scale: str = "ratio1024") -> tuple[CanMsg, int]:
    data, raw = encode_set_data(name, value, nominal_a, scale)
    return CanMsg(0.0, request_id(CMD_SET, addr), data, ext=True), raw


def request_frame(cmd: int, addr: int = 0) -> CanMsg:
    return CanMsg(0.0, request_id(cmd, addr), bytes(8), ext=True)


def get_frame(reg: int, addr: int = 1) -> CanMsg:
    return CanMsg(0.0, request_id(CMD_GET, addr), bytes([0x01, reg, 0, 0, 0, 0, 0, 0]), ext=True)


def voltage_in_range(name: str, volts: float) -> bool:
    lo, hi = VOLTAGE_RANGES.get(name, (0.0, 1e9))
    return lo <= volts <= hi


# ----------------------------------------------------------------- decode
def _u32(data: bytes, off: int = 4) -> int:
    d = bytes(data).ljust(8, b"\0")
    return int.from_bytes(d[off:off + 4], "big")


def decode_status_data(data: bytes) -> dict:
    """One frame of the 0x40 burst: bytes 0-1 = 0x01 nn register, value =
    big-endian u32 in bytes 4-7."""
    d = bytes(data).ljust(8, b"\0")
    reg = d[1]
    raw = _u32(d)
    out = {"reg": f"0x{reg:02X}", "raw": raw}
    if d[0] != 0x01:
        out["note"] = f"unexpected byte0 0x{d[0]:02X}"
    spec = STATUS_REGS.get(reg)
    if spec is None:
        out["name"] = f"unknown_{reg:02X}"
        return out
    name, unit, div = spec
    out["name"] = name
    if reg == 0x83:
        hi = int.from_bytes(d[2:6], "big")
        flags = [n for bit, n in STATUS_BITS.items() if raw & bit]
        flags += [n for bit, n in STATUS_BITS_HI.items() if hi & bit]
        out.update({"value": raw, "flags": flags, "bits_2_5": hi})
    else:
        out["value"] = round(raw / div, 3) if div != 1 else raw
    if unit:
        out["unit"] = unit
    return out


def decode_ack_data(data: bytes) -> dict:
    """0x80 ack: echo of the set frame, byte 0 = 0x21 (or low nibble 2) on
    error ("value out of range" / "parameter error")."""
    d = bytes(data).ljust(8, b"\0")
    reg = d[1]
    err = d[0] == ACK_ERROR_BYTE or bool(d[0] & 0x20) or (d[0] & 0x0F) == 2
    name = SET_REG_BY_NUM.get(reg, f"unknown_{reg:02X}")
    layout = SET_REGS.get(name, (None, "u32", ""))[1]
    raw = d[3] if layout == "flag3" else _u32(d)
    out = {"reg": f"0x{reg:02X}", "name": name, "ok": not err, "raw": raw,
           "status_byte": d[0]}
    if err:
        out["error"] = "parameter error / value out of range (0x21)"
    unit = SET_REGS.get(name, (None, None, ""))[2]
    if unit == "V":
        out["value_v"] = round(raw / SCALE_1024, 3)
    return out


def decode_info_data(data: bytes) -> dict:
    """0x50 reply frames: bytes 0-1 = 00 nn sub-id."""
    d = bytes(data).ljust(8, b"\0")
    sub = d[1]
    out = {"sub": sub, "raw_hex": d.hex().upper()}
    if sub == 0x01:
        val = _u32(d)
        out.update({"name": "characteristics",
                    "nominal_current_a": ((val >> 16) & 0x3FF) / 2})
    elif sub == 0x02:
        out.update({"name": "serial", "serial_hex": d[2:8].hex().upper()})
    elif sub in (0x03, 0x04):
        out.update({"name": "barcode", "ascii": d[2:8].decode("ascii", "replace")})
    elif sub == 0x05:
        out.update({"name": "versions", "hw_version": int.from_bytes(d[2:4], "big"),
                    "dcdc_sw": int.from_bytes(d[4:6], "big"),
                    "pfc_sw": int.from_bytes(d[6:8], "big")})
    elif sub == 0x06:
        out.update({"name": "slot_id", "slot_top": d[2], "slot_bottom": d[3],
                    "slot_id": f"0x{d[2]:02X}{d[3]:02X}"})
    else:
        out["name"] = f"info_{sub:02X}"
    return out


def decode_unsolicited_data(data: bytes, nominal_a: float | None = None) -> dict:
    """0x11 frames: 00 01 00 0s 00 00 xx xx (~377 ms), 00 02 .. (~3 s),
    00 03 00 00 00 0s xx xx. xx xx = output load ratio x 1024 (patagonaa;
    other sources read it /20 or /30 as amps)."""
    d = bytes(data).ljust(8, b"\0")
    sub = d[1]
    load_raw = int.from_bytes(d[6:8], "big")
    out = {"sub": sub, "load_raw": load_raw, "load_ratio": round(load_raw / 1024, 4)}
    if nominal_a:
        out["load_a_ratio1024"] = round(load_raw / 1024 * nominal_a, 2)
    if sub == 0x01:
        out["output_disabled"] = bool(d[3])
    elif sub == 0x03:
        out["output_active"] = bool(d[5])
    return out


def decode_addr_data(data: bytes) -> dict:
    d = bytes(data).ljust(8, b"\0")
    return {"phase": f"{d[0]:02X}{d[1]:02X}", "serial_hex": d[2:8].hex().upper()}


def decode(msg: CanMsg, nominal_a: float | None = None) -> dict | None:
    """Decode any Huawei frame; None if the identifier is not Huawei."""
    if not msg.ext or msg.err:
        return None
    hid = HuaweiId.unpack(msg.can_id)
    if not hid.is_huawei:
        return None
    out: dict = {"proto": "huawei-r48xx", "addr": hid.addr, "cmd": hid.cmd_name,
                 "cont": hid.cont}
    cmd = hid.cmd
    from_psu = not hid.to_psu or cmd in (CMD_UNSOL, CMD_ADDR)
    if cmd == CMD_DATA and from_psu:
        out["kind"] = "status"
        out.update(decode_status_data(msg.data))
    elif cmd == CMD_DATA:
        out["kind"] = "status_request"
    elif cmd == CMD_SET and from_psu:
        out["kind"] = "ack"
        out.update(decode_ack_data(msg.data))
    elif cmd in (CMD_SET, CMD_CFG):
        out["kind"] = "set"
        d = bytes(msg.data).ljust(8, b"\0")
        name = SET_REG_BY_NUM.get(d[1], f"unknown_{d[1]:02X}")
        layout = SET_REGS.get(name, (None, "u32", ""))[1]
        raw = d[3] if layout == "flag3" else _u32(d)
        out.update({"name": name, "raw": raw})
        if SET_REGS.get(name, (0, 0, ""))[2] == "V":
            out["value_v"] = round(raw / SCALE_1024, 3)
    elif cmd == CMD_INFO and from_psu:
        out["kind"] = "info"
        out.update(decode_info_data(msg.data))
    elif cmd == CMD_INFO:
        out["kind"] = "info_request"
    elif cmd == CMD_ELABEL and from_psu:
        out["kind"] = "elabel"
        out["ascii"] = bytes(msg.data)[2:8].decode("ascii", "replace")
    elif cmd == CMD_ELABEL:
        out["kind"] = "elabel_request"
    elif cmd == CMD_GET and from_psu:
        out["kind"] = "get_reply"
        out.update(decode_status_data(msg.data))
    elif cmd == CMD_GET:
        out["kind"] = "get"
        out["reg"] = f"0x{bytes(msg.data)[1]:02X}" if len(msg.data) > 1 else None
    elif cmd == CMD_UNSOL:
        out["kind"] = "unsolicited"
        out.update(decode_unsolicited_data(msg.data, nominal_a))
    elif cmd == CMD_ADDR:
        out["kind"] = "addr_negotiation"
        out.update(decode_addr_data(msg.data))
    else:
        out["kind"] = "other"
    out["flow"] = "PSU->CTL" if from_psu else "CTL->PSU"
    return out


def flow_of(msg: CanMsg) -> str | None:
    d = decode(msg)
    return d["flow"] if d else None


# ------------------------------------------------------------- aggregation
class StatusBurst:
    """Collects the 0x40 reply frames of one PSU until the last one
    (cont=0, normally register 0x83) and yields a snapshot dict."""

    def __init__(self):
        self.frames: list[dict] = []
        self.complete = False

    def feed(self, msg: CanMsg) -> bool:
        dec = decode(msg)
        if not dec or dec.get("kind") != "status":
            return False
        self.frames.append(dec)
        if not dec["cont"]:
            self.complete = True
        return True

    def snapshot(self, nominal_a: float | None = None, scale: str | None = None) -> dict:
        snap: dict = {"registers": {}, "unknown": []}
        for f in self.frames:
            if f["name"].startswith("unknown"):
                snap["unknown"].append({"reg": f["reg"], "raw": f["raw"]})
                continue
            snap[f["name"]] = f.get("value")
            snap["registers"][f["reg"]] = f["raw"]
            if f["name"] == "status_bits":
                snap["status_flags"] = f["flags"]
        if "current_limit_ratio" in snap and nominal_a:
            snap["current_limit_a_ratio1024"] = round(snap["current_limit_ratio"] * nominal_a, 2)
        if "current_limit_ratio" in snap and scale and scale != "ratio1024":
            raw = snap["registers"]["0x76"]
            snap[f"current_limit_a_{scale}"] = round(CURRENT_SCALES[scale].decode(raw, nominal_a), 2)
        snap["frames"] = len(self.frames)
        snap["complete"] = self.complete
        return snap


class ELabel:
    def __init__(self):
        self.text = ""
        self.complete = False

    def feed(self, msg: CanMsg) -> bool:
        dec = decode(msg)
        if not dec or dec.get("kind") != "elabel":
            return False
        self.text += bytes(msg.data)[2:8].decode("ascii", "replace")
        if not dec["cont"]:
            self.complete = True
        return True

    def fields(self) -> dict:
        out = {}
        for line in self.text.replace("\r", "\n").split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().rstrip("\x00")
        return out
