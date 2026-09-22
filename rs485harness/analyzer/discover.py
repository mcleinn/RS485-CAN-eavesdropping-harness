"""Discovery run: full inventory of what a responder provides.

Where identify answers "who is there", discover answers "what do they
offer": it sweeps every known command / register block and produces a
capability + state report you can archive and diff (against the inverter's
demands, against the ideal simulator, or against the same battery after a
firmware or protocol-setting change).

- Pylontech-ASCII responder: probes ALL known CID2 commands (both
  families), classifies each as ok / unsupported (RTN=04) / error RTN /
  silent, decodes every payload, and records dialect quirks (response
  address policy, CID2 echo, VER changes).
- Modbus responder (Voltronic-LIB / Seplos-native): reads the documented
  register blocks and decodes the Voltronic map into named values.

Run identify first to learn baud/protocol; discover assumes the link's
current baud (or --baud).
"""
from __future__ import annotations

from ..core.transport import Link
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P
from .identify import _probe_pylontech, _probe_modbus, _modbus_response_words

# (cid2, info-style): "pack" = pack-number byte expected in INFO
PYLONTECH_SWEEP = [
    (0x42, "pack"), (0x44, "pack"), (0x47, ""), (0x4F, ""), (0x51, ""),
    (0x60, ""), (0x61, ""), (0x62, ""), (0x63, ""),
    (0x92, "pack"), (0x93, "pack"),
]

# Voltronic 'Inverter and BMS 485' register blocks (docs/refs/...20201202.txt)
VOLTRONIC_BLOCKS = [
    (0x0001, 8, "version-info"),
    (0x0010, 37, "status"),        # 0x0010..0x0034: cells, temps, I, V, SOC
    (0x0036, 10, "alarms"),        # parallel count + alarm/protect words
    (0x0070, 8, "limits"),         # limits + charge/discharge status
]

STATUS_BITS_0074 = {7: "charge_enable", 6: "discharge_enable",
                    5: "charge_immediately", 4: "charge_immediately2",
                    3: "full_charge_request", 2: "small_current_charge_request"}


def _classify_pylontech_result(cid2: int, pf, req_adr: int, req_ver: int,
                               quirks: set) -> dict:
    entry: dict = {"cid2": f"{cid2:02X}",
                   "name": F.CID2_NAMES.get(cid2, "?")}
    if pf is None:
        entry["status"] = "silent"
        return entry
    entry["response_adr"] = pf.adr
    if pf.adr != req_adr:
        quirks.add(f"responds from ADR {pf.adr:02X} when polled at "
                   f"{req_adr:02X}")
    if pf.ver != req_ver:
        quirks.add(f"response VER {pf.ver:02X} differs from request VER "
                   f"{req_ver:02X}")
    if pf.errors:
        entry["frame_errors"] = pf.errors
    rtn = pf.cid2
    if rtn == cid2 and len(pf.info) > 2:
        quirks.add("responses echo CID2 instead of RTN=00 "
                   "(non-spec, strict inverters reject this)")
        rtn_eff = 0x00
    else:
        rtn_eff = rtn
    if rtn_eff == 0x00:
        entry["status"] = "ok"
        decoded = P.decode_response_info(cid2, pf.info)
        if cid2 == 0x4F:
            decoded = {"protocol_version": f"{pf.ver:02X}"}
        if decoded is not None and "decode_error" not in (decoded or {}):
            entry["decoded"] = decoded
        else:
            entry["payload_hex"] = pf.info
            if decoded:
                entry["decode_error"] = decoded.get("decode_error")
    elif rtn_eff == 0x04:
        entry["status"] = "unsupported"
    else:
        entry["status"] = f"error RTN={rtn_eff:02X} " \
                          f"({F.RTN_CODES.get(rtn_eff, '?')})"
    return entry


def _pylontech_discovery(link: Link, session: SessionWriter, chan: str,
                         adr: int, ver: int, on_event) -> dict:
    quirks: set = set()
    commands: dict = {}
    for cid2, info_style in PYLONTECH_SWEEP:
        info = f"{max(adr, 1):02X}" if info_style == "pack" else ""
        pf, lat = _probe_pylontech(link, ver, adr, cid2, info,
                                   session=session, chan=chan)
        entry = _classify_pylontech_result(cid2, pf, adr, ver, quirks)
        if lat is not None:
            entry["latency_ms"] = lat
        commands[f"{cid2:02X}"] = entry
        on_event(f"  {cid2:02X} {entry['name']:32} -> {entry['status']}")

    supported = [k for k, v in commands.items() if v["status"] == "ok"]
    state = _merge_state(commands)
    return {"commands": commands, "supported": supported,
            "unsupported": [k for k, v in commands.items()
                            if v["status"] == "unsupported"],
            "silent": [k for k, v in commands.items()
                       if v["status"] == "silent"],
            "errors": [k for k, v in commands.items()
                       if v["status"].startswith("error")],
            "quirks": sorted(quirks), "state": state}


def _merge_state(commands: dict) -> dict:
    """Flatten the most useful decoded values into one snapshot."""
    state: dict = {}
    d61 = commands.get("61", {}).get("decoded")
    if d61:
        state.update({k: v for k, v in d61.items() if not k.startswith("_")})
    d63 = commands.get("63", {}).get("decoded")
    if d63:
        state["limits"] = d63
    d62 = commands.get("62", {}).get("decoded")
    if d62:
        state["active_alarms"] = d62.get("active", [])
    d42 = commands.get("42", {}).get("decoded")
    if d42 and d42.get("modules"):
        m = d42["modules"][0]
        state["cells"] = m.get("n_cells")
        state["cell_v"] = m.get("cell_v")
        state["capacity_ah"] = {"remaining": m.get("remaining_ah"),
                                "total": m.get("total_ah")}
    d47 = commands.get("47", {}).get("decoded")
    if d47:
        state["system_parameters"] = d47
    d51 = commands.get("51", {}).get("decoded")
    if d51:
        state["identity"] = d51
    d93 = commands.get("93", {}).get("decoded")
    if d93:
        state["serial_number"] = d93.get("serial_number")
    d4f = commands.get("4F", {}).get("decoded")
    if d4f:
        state["protocol_version"] = d4f.get("protocol_version")
    return state


def _decode_voltronic_registers(blocks: dict) -> dict:
    """Named values from the Voltronic register map (best effort)."""
    regs: dict[int, int] = {}
    for start, words in blocks.items():
        for i, w in enumerate(words):
            regs[start + i] = w
    out: dict = {}
    if 0x0010 in regs:
        out["cell_count"] = regs[0x0010]
    cells = [regs[a] for a in range(0x0011, 0x0025) if a in regs and regs[a]]
    if cells:
        # doc says unit 0.1V but real firmwares often use mV - auto-scale
        scale = 0.001 if max(cells) > 1000 else 0.1
        out["cell_voltages_v"] = [round(c * scale, 3) for c in cells]
    if 0x0025 in regs:
        out["temp_sensor_count"] = regs[0x0025]
    temps = [regs[a] for a in range(0x0026, 0x0030) if a in regs and regs[a]]
    if temps:
        out["temperatures_c"] = [round(t / 10 - 273.15, 1) for t in temps]
    if 0x0030 in regs:
        out["charge_current_a"] = regs[0x0030] / 10
    if 0x0031 in regs:
        out["discharge_current_a"] = regs[0x0031] / 10
    if 0x0032 in regs:
        out["module_voltage_v"] = regs[0x0032] / 10
    if 0x0033 in regs:
        out["soc_percent"] = regs[0x0033]
    if 0x0036 in regs:
        out["parallel_packs"] = regs[0x0036]
    alarm_words = {f"{a:04X}": f"{regs[a]:04X}"
                   for a in range(0x0037, 0x003D) if a in regs}
    if alarm_words:
        out["alarm_protect_words"] = alarm_words
        out["any_alarm_or_protect"] = any(int(v, 16) for v in
                                          alarm_words.values())
    if 0x0070 in regs:
        out["charge_voltage_limit_v"] = regs[0x0070] / 10
    if 0x0071 in regs:
        out["discharge_voltage_limit_v"] = regs[0x0071] / 10
    if 0x0072 in regs:
        out["charge_current_limit_a"] = regs[0x0072] / 10
    if 0x0073 in regs:
        out["discharge_current_limit_a"] = regs[0x0073] / 10
    if 0x0074 in regs:
        s = regs[0x0074]
        out["charge_discharge_status"] = {name: bool(s & (1 << bit))
                                          for bit, name in
                                          STATUS_BITS_0074.items()}
    if 0x0075 in regs:
        out["run_time_to_empty_min"] = regs[0x0075]
    return out


def _modbus_discovery(link: Link, session: SessionWriter, chan: str,
                      slave: int, on_event) -> dict:
    blocks: dict[int, list[int]] = {}
    read: dict = {}
    for start, count, tag in VOLTRONIC_BLOCKS:
        hit, lat, _req = _probe_modbus(link, slave, start, count,
                                       timeout=0.8, session=session,
                                       chan=chan, tag=f"discover-{tag}")
        if hit:
            words = _modbus_response_words(hit)
            if words:
                blocks[start] = words
                read[tag] = {"start": f"{start:04X}", "count": count,
                             "latency_ms": lat, "words": words}
                on_event(f"  block {tag} @0x{start:04X}: {len(words)} regs "
                         f"in {lat}ms")
                continue
        read[tag] = {"start": f"{start:04X}", "count": count,
                     "status": "no answer"}
        on_event(f"  block {tag} @0x{start:04X}: no answer")
    return {"slave": slave, "blocks": read,
            "decoded": _decode_voltronic_registers(blocks)}


def _find_responder(link: Link, session: SessionWriter, chan: str,
                    on_event) -> dict | None:
    """Quick scan (current baud only) for something to discover."""
    for adr in (2, 0x12, 1, 0):
        for ver in (0x20, 0x25):
            pf, _lat = _probe_pylontech(link, ver, adr, 0x61, "",
                                        session=session, chan=chan)
            if pf is None:
                pf, _lat = _probe_pylontech(link, ver, adr, 0x42,
                                            f"{max(adr, 1):02X}",
                                            session=session, chan=chan)
            if pf is not None:
                return {"protocol": "pylontech", "adr": adr, "ver": ver}
    for slave in (1, 0x0F, 0):
        start = 0x1000 if slave == 0 else 0x0010
        hit, _lat, _req = _probe_modbus(link, slave, start, 4,
                                        session=session, chan=chan,
                                        tag="discover-scan")
        if hit:
            return {"protocol": "modbus", "slave": slave}
    return None


def discover(link: Link, session: SessionWriter, chan: str = "disc",
             adr: int | None = None, ver: int | None = None,
             on_event=None) -> dict:
    on_event = on_event or (lambda line: None)
    session.note(f"discover start on {link.name} "
                 f"baud={getattr(link, 'baud', '?')}", chan)
    report: dict = {"link": link.name, "baud": getattr(link, "baud", None)}

    if adr is not None:
        responder = {"protocol": "pylontech", "adr": adr, "ver": ver or 0x20}
    else:
        on_event("scanning for a responder (current baud) ...")
        responder = _find_responder(link, session, chan, on_event)
    if responder is None:
        report["result"] = ("no responder found at the current baud - run "
                            "`rsh identify` first to find baud/protocol, "
                            "then re-run discover")
        session.note("discover: no responder", chan)
        return report
    report["responder"] = {**responder,
                           **({"ver": f"{responder['ver']:02X}"}
                              if "ver" in responder else {})}
    on_event(f"responder: {report['responder']}")

    if responder["protocol"] == "pylontech":
        result = _pylontech_discovery(link, session, chan,
                                      responder["adr"], responder["ver"],
                                      on_event)
    else:
        result = _modbus_discovery(link, session, chan, responder["slave"],
                                   on_event)
    report.update(result)
    session.note(f"discover done: supported={report.get('supported')} "
                 f"quirks={report.get('quirks')}", chan)
    return report


def format_discovery(report: dict) -> str:
    lines = ["DISCOVERY REPORT"]
    if "result" in report:
        return "\n".join(lines + [f"  {report['result']}"])
    lines.append(f"  responder: {report['responder']}")
    if "commands" in report:
        lines.append(f"  supported:   {', '.join(report['supported']) or '-'}")
        lines.append(f"  unsupported: {', '.join(report['unsupported']) or '-'}")
        if report["silent"]:
            lines.append(f"  silent:      {', '.join(report['silent'])}")
        if report["errors"]:
            lines.append(f"  errors:      {', '.join(report['errors'])}")
        for q in report.get("quirks", []):
            lines.append(f"  quirk: {q}")
        st = report.get("state", {})
        if st:
            hl = []
            if "voltage_v" in st:
                hl.append(f"{st['voltage_v']}V")
            if "current_a" in st:
                hl.append(f"{st['current_a']}A")
            if "soc_percent" in st:
                hl.append(f"SOC {st['soc_percent']}%")
            if "soh_percent" in st:
                hl.append(f"SOH {st['soh_percent']}%")
            if "cycles" in st:
                hl.append(f"{st['cycles']} cycles")
            if "cells" in st:
                hl.append(f"{st['cells']} cells")
            if hl:
                lines.append("  state: " + ", ".join(hl))
            lim = st.get("limits")
            if lim:
                lines.append(f"  limits: charge {lim.get('charge_voltage_limit_v')}V/"
                             f"{lim.get('charge_current_limit_a')}A, discharge min "
                             f"{lim.get('discharge_voltage_min_v')}V/"
                             f"{lim.get('discharge_current_limit_a')}A, status "
                             f"{lim.get('status')}")
            if st.get("active_alarms") is not None:
                lines.append(f"  alarms: {st['active_alarms'] or 'none'}")
            ident = st.get("identity")
            if ident:
                lines.append(f"  identity: {ident.get('device_name')} / "
                             f"{ident.get('manufacturer')} sw "
                             f"{ident.get('software_version')} serial "
                             f"{st.get('serial_number', '?')}")
    if "decoded" in report:  # modbus
        for k, v in report["decoded"].items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)
