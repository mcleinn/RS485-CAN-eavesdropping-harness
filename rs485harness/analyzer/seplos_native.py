"""Seplos BMS 3.0 native service interface (read-only).

The pack's RS485-1/RS485-2 sockets speak Modbus RTU at 19200 8N1 (not the
inverter socket, which speaks the inverter protocol at 9600). Per the Seplos
protocol document the BMS supports only function 0x04 (read registers),
0x01 (read coils), 0x10 and 0x0F (writes). This module READS ONLY: it never
sends a write function code.

Blocks: PIA 0x1000 (pack status, 18 regs), PIB 0x1100 (cells + temps, 26),
SPA 0x1300 (settings, 106). Device id = pack number - 1 (single pack: 0).

Register names, units and scales follow the labels used by Seplos BMS
Studio as collected in https://github.com/marcelrv/seplosBMSv3 (CC BY-NC);
temperatures are sent in 0.1 K. NOT yet verified against real hardware by
this project - `sanity` in the report flags a read-out that does not look
like a LiFePO4 Seplos pack.
"""
from __future__ import annotations

import time

from ..core.transport import Link
from ..core.session import SessionWriter
from .classify import crc16_modbus, MODBUS_EXCEPTIONS

READ_REGISTERS = 0x04          # the only register-read function the BMS serves

PIA = (
    (0x1000, 'Battery voltage', 'V', 0.01, False),
    (0x1001, 'Current', 'A', 0.01, True),
    (0x1002, 'Remaining capacity', 'Ah', 0.01, False),
    (0x1003, 'Total capacity', 'Ah', 0.01, False),
    (0x1004, 'Total discharge capacity', 'Ah', 10.0, False),
    (0x1005, 'SOC', '%', 0.1, False),
    (0x1006, 'SOH', '%', 0.1, False),
    (0x1007, 'Cycle', 'times', 1.0, False),
    (0x1008, 'Averag of cell votage', 'V', 0.001, False),
    (0x1009, 'Averag of cell temperature', 'degC', 0.1, False),
    (0x100A, 'Max cell voltage', 'V', 0.001, False),
    (0x100B, 'Min cell voltage', 'V', 0.001, False),
    (0x100C, 'Max cell temperature', 'degC', 0.1, False),
    (0x100D, 'Min cell temperature', 'degC', 0.1, False),
    (0x100E, 'System events', '8', 1.0, False),
    (0x100F, 'Max discharge current', 'A', 1.0, False),
    (0x1010, 'Max charge current', 'A', 1.0, False),
    (0x1011, 'Extern voltage', 'V', 0.001, False),
)

PIB = (
    (0x1100, 'Cell01', 'V', 0.001, False),
    (0x1101, 'Cell02', 'V', 0.001, False),
    (0x1102, 'Cell03', 'V', 0.001, False),
    (0x1103, 'Cell04', 'V', 0.001, False),
    (0x1104, 'Cell05', 'V', 0.001, False),
    (0x1105, 'Cell06', 'V', 0.001, False),
    (0x1106, 'Cell07', 'V', 0.001, False),
    (0x1107, 'Cell08', 'V', 0.001, False),
    (0x1108, 'Cell09', 'V', 0.001, False),
    (0x1109, 'Cell10', 'V', 0.001, False),
    (0x110A, 'Cell11', 'V', 0.001, False),
    (0x110B, 'Cell12', 'V', 0.001, False),
    (0x110C, 'Cell13', 'V', 0.001, False),
    (0x110D, 'Cell14', 'V', 0.001, False),
    (0x110E, 'Cell15', 'V', 0.001, False),
    (0x110F, 'Cell16', 'V', 0.001, False),
    (0x1110, 'Cell temperature 1', 'degC', 0.1, False),
    (0x1111, 'Cell temperature 2', 'degC', 0.1, False),
    (0x1112, 'Cell temperature 3', 'degC', 0.1, False),
    (0x1113, 'Cell temperature 4', 'degC', 0.1, False),
    (0x1114, 'Cell temperature 5', 'degC', 0.1, False),
    (0x1115, 'Cell temperature 6', 'degC', 0.1, False),
    (0x1116, 'Cell temperature 7', 'degC', 0.1, False),
    (0x1117, 'Cell temperature 8', 'degC', 0.1, False),
    (0x1118, 'Ambient temperature', 'degC', 0.1, False),
    (0x1119, 'Power temperature', 'degC', 0.1, False),
)

SPA = (
    (0x1300, 'Ntc number', 'num', 1.0, False),
    (0x1301, 'Serial battery number', 'num', 1.0, False),
    (0x1302, 'Battery high voltage recovery', 'V', 0.01, False),
    (0x1303, 'Battery high voltage alarm', 'V', 0.01, False),
    (0x1304, 'Battery over voltage recovery', 'V', 0.01, False),
    (0x1305, 'Battery over voltage protection', 'V', 0.01, False),
    (0x1306, 'Battery low voltage recovery', 'V', 0.01, False),
    (0x1307, 'Battery low voltage alarm', 'V', 0.01, False),
    (0x1308, 'Battery under voltage recovery', 'V', 0.01, False),
    (0x1309, 'Battery under voltage protection', 'V', 0.01, False),
    (0x130A, 'Cell high voltage recovery', 'V', 0.001, False),
    (0x130B, 'Cell high voltage alarm', 'V', 0.001, False),
    (0x130C, 'Cell over voltage recovery', 'V', 0.001, False),
    (0x130D, 'Cell over voltage protection', 'V', 0.001, False),
    (0x130E, 'Cell low voltage recovery', 'V', 0.001, False),
    (0x130F, 'Cell low voltage alarm', 'V', 0.001, False),
    (0x1310, 'Cell under voltage recovery', 'V', 0.001, False),
    (0x1311, 'Cell under voltage protection', 'V', 0.001, False),
    (0x1312, 'Cell under voltage failure', 'V', 0.001, False),
    (0x1313, 'Cell diff pressure protection', 'V', 0.001, False),
    (0x1314, 'Diff pressure protection recovery', 'V', 0.001, False),
    (0x1315, 'Charge over current recovery', 'A', 1.0, True),
    (0x1316, 'Charge over current alarm', 'A', 1.0, True),
    (0x1317, 'Charge over current protection', 'A', 1.0, True),
    (0x1318, 'Charge over current delay', 's', 0.1, False),
    (0x1319, 'Secondary charge over current protection', 'A', 1.0, True),
    (0x131A, 'Secondary charge over current delay', 'ms', 1.0, False),
    (0x131B, 'Discharge over current recovery', 'A', 1.0, True),
    (0x131C, 'Discharge over current alarm', 'A', 1.0, True),
    (0x131D, 'Discharge over current protection', 'A', 1.0, True),
    (0x131E, 'Discharge over current delay', 's', 0.1, False),
    (0x131F, 'Secondary discharge over current protection', 'A', 1.0, True),
    (0x1320, 'Secondary discharge over current delay', 'ms', 1.0, False),
    (0x1321, 'Output short-circuit protection', 'A', 1.0, True),
    (0x1322, 'Output short-circuit delay', 'us', 1.0, False),
    (0x1323, 'Over current recovery delay', 's', 0.1, False),
    (0x1324, 'Number of over current lock times', 'times', 1.0, False),
    (0x1325, 'Charge current Limit duration', 's', 0.1, False),
    (0x1326, 'Pluse current limiting current', 'A', 1.0, False),
    (0x1327, 'Pluse current limiting time', 's', 0.1, False),
    (0x1328, 'Float charge locking voltage', 'V', 0.001, False),
    (0x1329, 'Float charge release voltage', 'V', 0.001, False),
    (0x132A, 'Float charge locking current', 'mA', 1.0, False),
    (0x132B, 'Short-circuit precharge completion rate', '%', 0.1, False),
    (0x132C, 'Normal precharge completion rate', '%', 0.1, False),
    (0x132D, 'Abnormal precharge completion rate', '%', 0.1, False),
    (0x132E, 'Precharge over time', 's', 0.1, False),
    (0x132F, 'Charge high temperature recovery', 'degC', 0.1, True),
    (0x1330, 'Charge high temperature alarm', 'degC', 0.1, True),
    (0x1331, 'Charge over temperature recovery', 'degC', 0.1, False),
    (0x1332, 'Charge over temperature protection', 'degC', 0.1, False),
    (0x1333, 'Charge low temperature recovery', 'degC', 0.1, True),
    (0x1334, 'Charge low temperature alarm', 'degC', 0.1, True),
    (0x1335, 'Charge under temperature recovery', 'degC', 0.1, True),
    (0x1336, 'Charge under temperature protection', 'degC', 0.1, False),
    (0x1337, 'Discharge high temperature recovery', 'degC', 0.1, False),
    (0x1338, 'Discharge high temperature alarm', 'degC', 0.1, True),
    (0x1339, 'Discharge over temperature recovery', 'degC', 0.1, False),
    (0x133A, 'Discharge over temperature protection', 'degC', 0.1, True),
    (0x133B, 'Discharge low temperature recovery', 'degC', 0.1, True),
    (0x133C, 'Discharge low temperature alarm', 'degC', 0.1, False),
    (0x133D, 'Discharge under temperature recovery', 'degC', 0.1, False),
    (0x133E, 'Discharge under temperature protection', 'degC', 0.1, False),
    (0x133F, 'High ambient temperature recovery', 'degC', 0.1, False),
    (0x1340, 'High ambient temperature alarm', 'degC', 0.1, False),
    (0x1341, 'Over ambient temperature recovery', 'degC', 0.1, False),
    (0x1342, 'Over ambient temperature protection', 'degC', 0.1, True),
    (0x1343, 'Low ambient temperature recovery', 'degC', 0.1, False),
    (0x1344, 'Low ambient temperature alarm', 'degC', 0.1, False),
    (0x1345, 'Under ambient temperature recovery', 'degC', 0.1, False),
    (0x1346, 'Under ambient temperature protection', 'degC', 0.1, False),
    (0x1347, 'Power high temperature recovery', 'degC', 0.1, False),
    (0x1348, 'power high temperature alarm', 'degC', 0.1, False),
    (0x1349, 'Power over temperature recovery', 'degC', 0.1, False),
    (0x134A, 'Power over temperature protection', 'degC', 0.1, False),
    (0x134B, 'Temperature regulate stop', 'degC', 0.1, False),
    (0x134C, 'Temperature regulate open', 'degC', 0.1, False),
    (0x134D, 'Equalization high temperature prohibition', 'degC', 0.1, False),
    (0x134E, 'Equalization low temperature prohibition', 'degC', 0.1, False),
    (0x134F, 'Static equalization timing', 'H', 1.0, False),
    (0x1350, 'Equalization open voltage', 'V', 0.001, False),
    (0x1351, 'Equalization open difference pressure', 'V', 0.001, False),
    (0x1352, 'Equalization stop difference pressure', 'V', 0.001, False),
    (0x1353, 'Power supply SOC', '%', 0.1, False),
    (0x1354, 'SOC low recovery', '%', 0.1, False),
    (0x1355, 'SOC low alarm', '%', 0.1, False),
    (0x1356, 'SOC protection recovery', '%', 0.1, False),
    (0x1357, 'SOC low protection', '%', 0.1, False),
    (0x1358, 'Rated battery capacity', 'Ah', 0.01, False),
    (0x1359, 'Total battery capacity', 'Ah', 0.01, False),
    (0x135A, 'Battery remaining capacity', 'Ah', 0.01, False),
    (0x135B, 'Stand-by time', 'H', 1.0, False),
    (0x135C, 'Forced output delay', 's', 0.1, False),
    (0x135D, 'Forced output splite', 'Min', 1.0, False),
    (0x135E, 'Number of forced output', 'times', 1.0, False),
    (0x135F, 'Compensation site 1', 'Cell', 1.0, False),
    (0x1360, 'Compensation site 1 resistance', 'mΩ', 1.0, False),
    (0x1361, 'Compensating site 2', 'Cell', 1.0, False),
    (0x1362, 'Compensation site 2 resistance', 'mΩ', 1.0, False),
    (0x1363, 'Cell diff pressure alarm', 'mV', 1.0, False),
    (0x1364, 'Diff pressure alarm recovery', 'mV', 1.0, False),
    (0x1365, 'Charging request voltage', 'V', 0.01, False),
    (0x1366, 'Charging request current', 'A', 1.0, False),
    (0x1367, 'Discharge request current', 'A', 1.0, False),
    (0x1368, 'Pcs protocol', 'PPT', 1.0, False),
    (0x1369, 'Current detector correction factor', '0.1', 1.0, False),
)

PCT = {
    0: 'Pylon_CAN',
    1: 'Growatt_CAN',
    2: 'Goodwe_CAN',
    3: 'Sofar_CAN',
    4: 'SMA_CAN',
    5: 'Victron_CAN',
    6: 'Studer_CAN',
    7: 'Ginlong_CAN',
    8: 'Voltronic_485',
    9: 'Srne_485',
    10: 'Growatt_485',
    11: 'Pylon_485',
    12: 'Deye(Pylon)_485',
    13: 'XZH(LCD02)_485',
    14: 'GudE_485',
    15: 'NPP-IMB_485',
}

BLOCKS = (("PIA", 0x1000, PIA), ("PIB", 0x1100, PIB), ("SPA", 0x1300, SPA))

# settings that matter for the charge-taper investigation, by SPA number
KEY_SETTINGS = (102, 103, 104, 105, 12, 11, 14, 13, 4, 3, 6, 5,
                41, 42, 43, 81, 82, 83, 100, 101)


def read_registers(link: Link, slave: int, start: int, count: int,
                   timeout: float = 1.0, session: SessionWriter | None = None,
                   chan: str = "disc", tag: str = ""):
    """One function-0x04 read. Returns (words | None, info dict).

    The reply length is known in advance (5 + 2*count, or 5 for an
    exception), so the frame is read to that length and CRC-checked instead
    of pattern-matching partial buffers."""
    pdu = bytes([slave, READ_REGISTERS, start >> 8, start & 0xFF,
                 count >> 8, count & 0xFF])
    crc = crc16_modbus(pdu)
    req = pdu + bytes([crc & 0xFF, crc >> 8])
    link.flush_input()
    t0 = time.perf_counter()
    link.write(req)
    if session:
        session.log(chan, "tx", req, "raw", note=f"seplos-native read {tag}")
    want = 5 + 2 * count
    buf = b""
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        chunk = link.read(timeout=min(0.1, max(0.01, deadline - time.perf_counter())))
        if not chunk:
            continue
        buf += chunk.data
        if buf.startswith(req):                 # local echo of our request
            buf = buf[len(req):]
        if len(buf) >= 5 and buf[1] == (READ_REGISTERS | 0x80):
            want = 5
        if len(buf) >= want:
            break
    latency = round((time.perf_counter() - t0) * 1000, 1)
    info = {"start": f"{start:04X}", "count": count, "function": READ_REGISTERS}
    if session and buf:
        session.log(chan, "rx", buf, "raw",
                    note=f"seplos-native answer {tag}, latency_ms={latency}")
    if not buf:
        info["status"] = "no answer"
        return None, info
    frame = buf[:want]
    if len(frame) < want or crc16_modbus(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
        info["status"] = f"bad frame ({len(buf)} bytes, wanted {want})"
        return None, info
    if frame[0] != slave:
        info["status"] = f"answer from device {frame[0]}, asked {slave}"
        return None, info
    if frame[1] & 0x80:
        name = MODBUS_EXCEPTIONS.get(frame[2], f"exception 0x{frame[2]:02X}")
        info["status"] = f"exception {frame[2]} ({name})"
        return None, info
    if frame[1] != READ_REGISTERS or frame[2] != 2 * count:
        info["status"] = "unexpected reply"
        return None, info
    words = [int.from_bytes(frame[3 + i:5 + i], "big") for i in range(0, 2 * count, 2)]
    info.update(status="ok", latency_ms=latency)
    return words, info


def _convert(raw: int, unit: str, scale: float, signed: bool):
    v = raw - 0x10000 if signed and raw >= 0x8000 else raw
    if unit == "degC":                        # sent as 0.1 K
        return round(v * scale - 273.15, 1)
    x = v * scale
    if scale < 1:
        return round(x, 3)
    return int(x) if float(x).is_integer() else x


def decode_block(name: str, words: list[int]) -> list[dict]:
    table = {n: t for n, _s, t in BLOCKS}[name]
    out = []
    for idx, (addr, label, unit, scale, signed) in enumerate(table):
        if idx >= len(words):
            break
        raw = words[idx]
        entry = {"no": idx + 1, "address": f"{addr:04X}", "name": label,
                 "raw": raw, "value": _convert(raw, unit, scale, signed),
                 "unit": unit}
        if name == "SPA" and label == "Pcs protocol":
            entry["text"] = PCT.get(raw, "?")
        if name == "SPA" and label == "Discharge request current":
            # the table marks it unsigned, the BMS sends it negative
            entry["value"] = raw - 0x10000 if raw >= 0x8000 else raw
        out.append(entry)
    return out


def _sanity(decoded: dict) -> list[str]:
    """Plausibility of the read-out - the register map is taken from
    documentation, so say so loudly when the values do not fit it."""
    notes = []
    spa = {e["no"]: e for e in decoded.get("SPA", [])}
    pia = {e["name"]: e for e in decoded.get("PIA", [])}

    def check(ok, text):
        if not ok:
            notes.append(text)

    if len(spa) >= 14:
        check(4 <= spa[2]["value"] <= 24, f"cell count reads {spa[2]['value']}")
        check(2.5 <= spa[14]["value"] <= 4.5,
              f"cell over-voltage protection reads {spa[14]['value']} V")
        check(spa[11]["value"] <= spa[12]["value"] <= spa[14]["value"],
              "cell high-voltage recovery/alarm/protection are not in "
              "ascending order")
    if len(spa) >= 102:
        check(40 <= spa[102]["value"] <= 62,
              f"charging request voltage reads {spa[102]['value']} V")
    if pia:
        check(30 <= pia["Battery voltage"]["value"] <= 65,
              f"pack voltage reads {pia['Battery voltage']['value']} V")
        check(0 <= pia["SOC"]["value"] <= 100, f"SOC reads {pia['SOC']['value']} %")
    return notes


def seplos_native_discovery(link: Link, session: SessionWriter, chan: str,
                            slave: int, on_event) -> dict:
    blocks: dict = {}
    decoded: dict = {}
    for name, start, table in BLOCKS:
        words, info = read_registers(link, slave, start, len(table),
                                     session=session, chan=chan, tag=name)
        blocks[name] = info
        if words is None:
            on_event(f"  block {name} @0x{start:04X}: {info['status']}")
            continue
        info["words"] = words
        decoded[name] = decode_block(name, words)
        on_event(f"  block {name} @0x{start:04X}: {len(words)} regs in "
                 f"{info['latency_ms']}ms")
    return {"slave": slave, "blocks": blocks, "seplos_native": decoded,
            "sanity": _sanity(decoded)}


def format_seplos_native(report: dict) -> list[str]:
    dec = report.get("seplos_native", {})
    lines = []
    for note in report.get("sanity", []):
        lines.append(f"  !! implausible value - register map may not match "
                     f"this firmware: {note}")
    pia = {e["name"]: e["value"] for e in dec.get("PIA", [])}
    if pia:
        lines.append(f"  state: {pia['Battery voltage']}V, {pia['Current']}A, "
                     f"SOC {pia['SOC']}%, SOH {pia['SOH']}%, "
                     f"{pia['Cycle']} cycles, cells "
                     f"{pia['Min cell voltage']}..{pia['Max cell voltage']}V, "
                     f"BMS allows charge {pia['Max charge current']}A / "
                     f"discharge {pia['Max discharge current']}A")
    cells = [e["value"] for e in dec.get("PIB", [])
             if e["name"].startswith("Cell") and
             "temperature" not in e["name"].lower() and e["raw"]]
    if cells:
        lines.append("  cells V: " + " ".join(f"{v:.3f}" for v in cells))
        lines.append(f"  cell spread: {round((max(cells) - min(cells)) * 1000)} mV")
    spa = dec.get("SPA", [])
    if spa:
        by_no = {e["no"]: e for e in spa}

        def fmt(e):
            return (f"  {e['no']:>3}  {e['name']:<42} {e['value']} "
                    f"{e.get('text') or e['unit']}")

        lines.append("  key settings (read-only):")
        lines += ["  " + fmt(by_no[n]) for n in KEY_SETTINGS if n in by_no]
        lines.append(f"  all {len(spa)} settings:")
        lines += ["  " + fmt(e) for e in spa]
    return lines


# ------------------------------------------------------------------ writing
#
# Deliberately narrow: one register at a time, only settings on the
# allow-list below, only inside hard limits, and by default only towards
# "more conservative" (a lower value). EXPERIMENTAL until proven on a real
# pack - the frame layout is from the Seplos protocol document.

WRITE_REGISTERS = 0x10

# {SPA number: (lowest, highest) value this tool will ever write}
WRITABLE = {102: (48.0, 57.6),      # Charging request voltage, V
            103: (10, 200)}         # Charging request current, A


def _write_frame(slave: int, addr: int, raw: int) -> bytes:
    pdu = bytes([slave, WRITE_REGISTERS, addr >> 8, addr & 0xFF, 0x00, 0x01,
                 0x02, raw >> 8, raw & 0xFF])
    crc = crc16_modbus(pdu)
    return pdu + bytes([crc & 0xFF, crc >> 8])


def write_setting(link: Link, session: SessionWriter, chan: str, slave: int,
                  number: int, value: float, apply: bool = False,
                  allow_increase: bool = False, on_event=None) -> dict:
    """Change ONE allow-listed setting: read all settings, check, write,
    read back. With apply=False nothing is sent beyond the reads (dry run).
    Returns a report; report["result"] is 'dry-run', 'written', 'unchanged'
    or 'refused: <why>' / 'failed: <why>'."""
    on_event = on_event or (lambda line: None)
    report: dict = {"setting": number, "requested": value, "slave": slave}

    def done(result: str) -> dict:
        report["result"] = result
        session.note(f"seplos write_setting {number}={value}: {result}", chan)
        on_event(f"  {result}")
        return report

    if number not in WRITABLE:
        return done(f"refused: setting {number} is not on the allow-list "
                    f"{sorted(WRITABLE)}")
    lo, hi = WRITABLE[number]
    if not lo <= value <= hi:
        return done(f"refused: {value} is outside the hard limits {lo}..{hi}")
    words, info = read_registers(link, slave, 0x1300, len(SPA),
                                 session=session, chan=chan, tag="SPA-before")
    if words is None:
        return done(f"failed: cannot read the settings first ({info['status']})")
    notes = _sanity({"SPA": decode_block("SPA", words)})
    if notes:
        return done("refused: settings read-out is implausible, the register "
                    "map may not match this firmware: " + "; ".join(notes))
    addr, label, unit, scale, _signed = SPA[number - 1]
    old_raw = words[number - 1]
    new_raw = int(round(value / scale))
    report.update(name=label, unit=unit, address=f"{addr:04X}",
                  before=_convert(old_raw, unit, scale, False),
                  frame_hex=_write_frame(slave, addr, new_raw).hex().upper())
    on_event(f"  {number} {label}: now {report['before']} {unit}, "
             f"requested {value} {unit}")
    if new_raw == old_raw:
        return done("unchanged")
    if new_raw > old_raw and not allow_increase:
        return done(f"refused: {value} is higher than the present "
                    f"{report['before']} {unit}; raising a charge request "
                    f"needs --allow-increase")
    report["restore"] = (f"seplos-set --setting {number} --value "
                         f"{report['before']} --allow-increase --apply")
    if not apply:
        return done("dry-run")

    req = _write_frame(slave, addr, new_raw)
    link.flush_input()
    link.write(req)
    session.log(chan, "tx", req, "raw", note=f"seplos-native WRITE {label}")
    buf = b""
    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline and len(buf) < 8:
        chunk = link.read(timeout=0.1)
        if chunk:
            buf += chunk.data
            if buf.startswith(req):             # local echo
                buf = buf[len(req):]
            if len(buf) >= 5 and buf[1] == (WRITE_REGISTERS | 0x80):
                break
    if buf:
        session.log(chan, "rx", buf, "raw", note="seplos-native WRITE answer")
    if len(buf) >= 5 and buf[1] == (WRITE_REGISTERS | 0x80):
        name = MODBUS_EXCEPTIONS.get(buf[2], f"exception 0x{buf[2]:02X}")
        report["write_ack"] = f"exception {buf[2]} ({name})"
    elif len(buf) >= 8 and buf[:6] == req[:6] and \
            crc16_modbus(buf[:6]) == (buf[6] | (buf[7] << 8)):
        report["write_ack"] = "ok"
    else:
        report["write_ack"] = f"no valid acknowledgement ({buf.hex().upper() or 'silence'})"
    # the read-back decides, whatever the acknowledgement looked like
    back, info = read_registers(link, slave, addr, 1, session=session,
                                chan=chan, tag="read-back")
    if back is None:
        return done(f"failed: write sent (ack: {report['write_ack']}) but the "
                    f"read-back failed ({info['status']}) - check with BMS Studio")
    report["after"] = _convert(back[0], unit, scale, False)
    if back[0] == new_raw:
        return done("written")
    return done(f"failed: read-back shows {report['after']} {unit} "
                f"(ack: {report['write_ack']}) - the BMS did not take the value")


# ------------------------------------------------------- status flags (PIC)
#
# 144 coils at 0x1200, read with function 0x01. Per-cell groups first, then
# named pack flags; names as shown in BMS Studio.

READ_COILS = 0x01
PIC_START, PIC_COUNT = 0x1200, 144

PIC_FLAGS = {
    0x40: "Discharge", 0x41: "Charge", 0x42: "Floating charge",
    0x43: "Full charge", 0x44: "Standby mode", 0x45: "Turn off",
    0x48: "Cell high voltage alarm", 0x49: "Cell over voltage protection",
    0x4A: "Cell low voltage alarm", 0x4B: "Cell under voltage protection",
    0x4C: "Battery high voltage alarm", 0x4D: "Battery over voltage protection",
    0x4E: "Battery low voltage alarm", 0x4F: "Battery under voltage protection",
    0x50: "Charge high temperature alarm",
    0x51: "Charge over temperature protection",
    0x52: "Charge low temperature alarm",
    0x53: "Charge under temperature protection",
    0x54: "Discharge high temperature alarm",
    0x55: "Discharge over temperature protection",
    0x56: "Discharge low temperature alarm",
    0x57: "Discharge under temperature protection",
    0x58: "High ambient temperature alarm",
    0x59: "Over ambient temperature protection",
    0x5A: "Low ambient temperature alarm",
    0x5B: "Under ambient temperature protection",
    0x5C: "High power temperature alarm",
    0x5D: "Over power temperature protection",
    0x5E: "Cell temperature low heating",
    0x60: "Charge current alarm", 0x61: "Charge over current protection",
    0x62: "Secondary charge over current protection",
    0x63: "Discharge current alarm", 0x64: "Discharge over current protection",
    0x65: "Secondary discharge over current protection",
    0x66: "Output short-circuit protection", 0x68: "Output short-circuit lock",
    0x69: "Inverse current protection",
    0x6A: "Secondary charge over current lock",
    0x6B: "Secondary discharge over current lock",
    0x6E: "Zero point uncalibrated",
    0x72: "SOC alarm", 0x73: "SOC protection", 0x74: "Cell diff alarm",
    0x78: "Discharge FET on", 0x79: "Charge FET on",
    0x7A: "Current limiting FET on", 0x7B: "Temp. regulate FET on",
    0x80: "Equilibrium module to open", 0x81: "Static equilibrium indicate",
    0x82: "Static equilibrium overtime", 0x83: "Equalization temperature limit",
    0x84: "Cell failure alarm",
    0x88: "NTC failure", 0x89: "AFE failure", 0x8A: "Charge mosfets failure",
    0x8B: "Discharge mosfets failure", 0x8C: "Celldiff failure",
    0x8D: "Break line failure", 0x8E: "Key failure", 0x8F: "Aerosol alarm",
}
MODE_FLAGS = ("Discharge", "Charge", "Floating charge", "Full charge",
              "Standby mode", "Turn off")
FET_FLAGS = ("Discharge FET on", "Charge FET on", "Current limiting FET on",
             "Temp. regulate FET on")


def read_coils(link: Link, slave: int, start: int, count: int,
               timeout: float = 1.0, session: SessionWriter | None = None,
               chan: str = "disc", tag: str = ""):
    """One function-0x01 read. Returns (list of bool | None, info dict)."""
    pdu = bytes([slave, READ_COILS, start >> 8, start & 0xFF,
                 count >> 8, count & 0xFF])
    crc = crc16_modbus(pdu)
    req = pdu + bytes([crc & 0xFF, crc >> 8])
    link.flush_input()
    t0 = time.perf_counter()
    link.write(req)
    if session:
        session.log(chan, "tx", req, "raw", note=f"seplos-native coils {tag}")
    nbytes = (count + 7) // 8
    want = 5 + nbytes
    buf = b""
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        chunk = link.read(timeout=min(0.1, max(0.01, deadline - time.perf_counter())))
        if not chunk:
            continue
        buf += chunk.data
        if buf.startswith(req):
            buf = buf[len(req):]
        if len(buf) >= 5 and buf[1] == (READ_COILS | 0x80):
            want = 5
        if len(buf) >= want:
            break
    latency = round((time.perf_counter() - t0) * 1000, 1)
    info = {"start": f"{start:04X}", "count": count, "function": READ_COILS}
    if session and buf:
        session.log(chan, "rx", buf, "raw",
                    note=f"seplos-native coils answer {tag}, latency_ms={latency}")
    if not buf:
        info["status"] = "no answer"
        return None, info
    frame = buf[:want]
    if len(frame) < want or crc16_modbus(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
        info["status"] = f"bad frame ({len(buf)} bytes, wanted {want})"
        return None, info
    if frame[0] != slave:
        info["status"] = f"answer from device {frame[0]}, asked {slave}"
        return None, info
    if frame[1] & 0x80:
        name = MODBUS_EXCEPTIONS.get(frame[2], f"exception 0x{frame[2]:02X}")
        info["status"] = f"exception {frame[2]} ({name})"
        return None, info
    if frame[1] != READ_COILS or frame[2] != nbytes:
        info["status"] = "unexpected reply"
        return None, info
    data = frame[3:3 + nbytes]
    bits = [bool(data[i // 8] & (1 << (i % 8))) for i in range(count)]
    info.update(status="ok", latency_ms=latency, hex=data.hex().upper())
    return bits, info


def decode_pic(bits: list[bool]) -> dict:
    """Split the 144 status coils into per-cell lists and named flags."""
    def cells(lo, n):
        return [i + 1 for i in range(n) if lo + i < len(bits) and bits[lo + i]]
    flags = [name for idx, name in sorted(PIC_FLAGS.items())
             if idx < len(bits) and bits[idx]]
    return {"cells_low_alarm": cells(0x00, 16), "cells_high_alarm": cells(0x10, 16),
            "temps_low_alarm": cells(0x20, 8), "temps_high_alarm": cells(0x28, 8),
            "balancing": cells(0x30, 16),
            "mode": [f for f in flags if f in MODE_FLAGS],
            "fets": {f: (f in flags) for f in FET_FLAGS},
            "flags": [f for f in flags if f not in MODE_FLAGS and f not in FET_FLAGS]}
