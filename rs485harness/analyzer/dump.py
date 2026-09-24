"""Full per-unit battery-state dump for later analysis.

Polls every informative command at each pack address and merges the
decoded results into one record per unit: protocol version, identity
(device name / manufacturer / software=firmware version), serial number,
fixed system parameters (limits, temp thresholds), full analog data (every
cell voltage, temps, current, voltage, capacity, cycles, SOC), and the
charge/discharge management values.

Addresses are polled individually (get_values_single style) so each unit's
data is unambiguously keyed by address — no reliance on the order of a
multi-pack 0x42 'FF' array. `rsh soc` uses the quick FF path instead.
"""
from __future__ import annotations

from ..core.transport import Link
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P
from .identify import _probe_pylontech

# (cid2, info uses address?, output key)
PER_ADDRESS_CMDS = [
    (0x4F, False, "protocol_version"),
    (0x51, False, "identity"),          # device name, fw (softwareVersion), mfr
    (0x93, True, "serial"),
    (0x47, True, "system_parameters"),  # fixed limits / temp thresholds
    (0x42, True, "analog"),             # cells, temps, I, V, capacity, cycles, SOC
    (0x92, True, "management"),         # charge/discharge limits + flags
]


def dump_unit(link: Link, adr: int, ver: int, session: SessionWriter,
              chan: str, on_event) -> dict:
    unit: dict = {"address": adr, "address_hex": f"{adr:02X}"}
    for cid2, uses_adr, key in PER_ADDRESS_CMDS:
        info = f"{adr:02X}" if uses_adr else ""
        pf, lat = _probe_pylontech(link, ver, adr, cid2, info,
                                   session=session, chan=chan)
        if pf is None:
            unit[key] = None
            continue
        if not pf.ok:
            unit[key] = {"frame_errors": pf.errors, "info": pf.info}
            continue
        if cid2 == 0x4F:
            unit[key] = f"{pf.ver:02X}"
        else:
            unit[key] = P.decode_response_info(cid2, pf.info)
    # promote a few headline values for convenience
    an = unit.get("analog") or {}
    mods = an.get("modules") or []
    if mods:
        m = mods[0]
        unit["summary"] = {
            "soc_percent": m.get("soc_percent"),
            "voltage_v": m.get("voltage_v"),
            "current_a": m.get("current_a"),
            "n_cells": m.get("n_cells"),
            "cycles": m.get("cycles"),
            "remaining_ah": m.get("remaining_ah"),
            "total_ah": m.get("total_ah"),
        }
    ident = unit.get("identity") or {}
    if isinstance(ident, dict):
        unit.setdefault("summary", {})
        unit["summary"]["device_name"] = ident.get("device_name")
        unit["summary"]["firmware"] = ident.get("software_version")
        unit["summary"]["manufacturer"] = ident.get("manufacturer")
    on_event(f"  address {adr:02X}: "
             + (f"{unit['summary'].get('device_name')} "
                f"SOC {unit['summary'].get('soc_percent')}% "
                f"{unit['summary'].get('voltage_v')}V "
                f"{unit['summary'].get('n_cells')} cells"
                if unit.get("summary") else "no data"))
    return unit


def detect_addresses(link: Link, ver: int, session: SessionWriter, chan: str,
                     scan_range=range(0, 8)) -> list[int]:
    found = []
    for a in scan_range:
        pf, _ = _probe_pylontech(link, ver, a, 0x42, f"{max(a, 1):02X}",
                                 session=session, chan=chan, timeout=0.4)
        if pf is not None and pf.ok:
            found.append(a)
    return found


def dump_stack(link: Link, session: SessionWriter, adrs: list[int] | None = None,
               ver: int = 0x20, chan: str = "dump", on_event=None) -> dict:
    on_event = on_event or (lambda s: None)
    session.note(f"dump start on {link.name} ver={ver:02X}", chan)
    if adrs is None:
        on_event("scanning for pack addresses (0..7) ...")
        adrs = detect_addresses(link, ver, session, chan)
        on_event(f"responding addresses: {[f'{a:02X}' for a in adrs]}")
    units = [dump_unit(link, a, ver, session, chan, on_event) for a in adrs]
    report = {"link": link.name, "baud": getattr(link, "baud", None),
              "ver": f"{ver:02X}", "addresses": [f"{a:02X}" for a in adrs],
              "n_units": len(units), "units": units}
    session.note(f"dump done: {len(units)} unit(s)", chan)
    return report


def format_dump(report: dict) -> str:
    lines = [f"BATTERY DUMP - {report['n_units']} unit(s) @ {report['baud']} baud"]
    for u in report["units"]:
        s = u.get("summary") or {}
        lines.append(
            f"  [{u['address_hex']}] {s.get('device_name','?')} "
            f"fw={s.get('firmware')} SOC={s.get('soc_percent')}% "
            f"{s.get('voltage_v')}V {s.get('current_a')}A "
            f"{s.get('n_cells')}cells cyc={s.get('cycles')} "
            f"proto={u.get('protocol_version')}")
        an = u.get("analog") or {}
        mods = an.get("modules") or []
        if mods and mods[0].get("cell_v"):
            cv = mods[0]["cell_v"]
            lines.append(f"       cells(V): {', '.join(f'{v:.3f}' for v in cv)}")
    return "\n".join(lines)
