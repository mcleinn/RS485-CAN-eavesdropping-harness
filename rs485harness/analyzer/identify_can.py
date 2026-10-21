"""`rsh identify` for CAN links: listen-only per bitrate, inventory of the
identifiers seen, verdicts; one polite request only when nobody else is
talking to the device (same rule as RS485: never become a second master).

Recognised: Huawei R48xx rectifiers (29-bit proto 0x21/0x20), the 11-bit
Pylontech/Victron/Dyness/LuxPower/Seplos battery set (0x351/0x355/0x356/
0x359/0x35A/0x35C/0x35E ..., inverter keep-alive 0x305/0x307), Dyness
native 29-bit frames, LV-HUB string offsets. Error frames without valid
frames mean the wrong bitrate (a CAN controller never produces garbled
frames at the wrong rate - it produces stuff/form errors).
"""
from __future__ import annotations

from ..core.canbus import CanLink, BITRATE_SWEEP, CanError
from ..core.session import SessionWriter
from ..protocols.huawei_r48xx.ids import HuaweiId, CMD_UNSOL
from ..protocols.pylontech_can import frames as PF
from .can_battery import BusInventory, listen, send_unlock, snapshot


def _verdicts_for(label: str, inv: BusInventory) -> tuple[list[str], dict]:
    verdicts: list[str] = []
    facts: dict = {}
    ids = inv.summary()
    if inv.frames == 0:
        if inv.error_frames:
            verdicts.append(f"ERROR FRAMES ONLY @ {label}: {inv.error_frames} in the window "
                            f"({inv.first_error}) - wrong bitrate for this bus, or missing "
                            f"termination / only one node.")
        return verdicts, facts
    huawei = {k: v for k, v in ids.items() if v.get("proto") == "huawei-r48xx"}
    if huawei:
        hids = {k: HuaweiId.unpack(int(k, 16)) for k in huawei}
        psus = sorted({h.addr for h in hids.values()})
        ctl = [k for k, h in hids.items() if h.to_psu and h.cmd != CMD_UNSOL]
        unsol = [huawei[k] for k, h in hids.items() if h.cmd == CMD_UNSOL and not h.to_psu]
        period = unsol[0]["period_s"] if unsol else None
        facts.update({"huawei_psu_addrs": psus, "huawei_controller_present": bool(ctl),
                      "huawei_unsolicited_period_s": period})
        verdicts.append(f"HUAWEI R48xx PSU @ {label}: address(es) {psus}, unsolicited "
                        f"frame every ~{period} s" + (f"; {len(ctl)} controller id(s) seen "
                                                      f"- another controller is talking, do "
                                                      f"NOT send" if ctl else
                                                      "; no controller on the bus"))
    pylon_ids = inv.pylontech_ids()
    if pylon_ids or inv.native_seen:
        bat = sorted(pylon_ids & PF.BATTERY_IDS)
        keep = sorted(pylon_ids & PF.INVERTER_IDS)
        ranking = inv.guess()
        facts.update({"pylontech_ids": [f"0x{i:03X}" for i in sorted(pylon_ids)],
                      "brand": inv.brand, "inverter_keepalive": bool(keep),
                      "dialects": ranking[:3], "dialect": ranking[0]["name"] if ranking else None,
                      "dyness_native": inv.native_seen})
        if bat or inv.native_seen:
            first = next((e for e in ids.values() if e.get("flow") == "BMS->INV"), {})
            per = first.get("period_s")
            verdicts.append(f"PYLONTECH-CAN BATTERY @ {label}: ids "
                            f"{[f'0x{i:03X}' for i in bat]} every ~{per} s, brand {inv.brand!r}"
                            + (", Dyness native 0x18BB/0x18AA frames" if inv.native_seen else "")
                            + (f"; inverter/GX keep-alive {[f'0x{i:03X}' for i in keep]} "
                               f"present" if keep else "; no inverter keep-alive seen")
                            + (f"; dialect guess: {ranking[0]['name']} "
                               f"({'; '.join(ranking[0]['reasons']) or 'all mandatory ids'})"
                               if ranking else ""))
            if inv.strings:
                verdicts.append(f"LV-HUB strings @ {label}: {sorted(inv.strings)}")
        elif keep:
            verdicts.append(f"INVERTER/GX KEEP-ALIVE ONLY @ {label}: "
                            f"{[f'0x{i:03X}' for i in keep]} - an inverter polls, no battery "
                            f"answers.")
    other = [k for k, v in ids.items() if not v.get("proto")]
    if other:
        verdicts.append(f"UNKNOWN IDS @ {label}: {other[:12]}"
                        + (" ..." if len(other) > 12 else ""))
    if inv.error_frames:
        verdicts.append(f"note: {inv.error_frames} error frames beside {inv.frames} "
                        f"valid frames @ {label} ({inv.first_error}).")
    quirks = inv.quirks(facts.get("dialect"))
    if quirks:
        facts["quirks"] = quirks
    return verdicts, facts


def identify_can(link: CanLink, session: SessionWriter, chan: str = "id",
                 listen_s: float = 5.0, active: bool = True,
                 bitrates=BITRATE_SWEEP, on_event=None, hcfg: dict | None = None,
                 unlock_s: float = 3.0) -> dict:
    on_event = on_event or (lambda line: None)
    report: dict = {"channel": chan, "link": link.name, "bus": "can", "passive": {},
                    "active": [], "verdicts": [], "facts": {}}
    session.note(f"identify (CAN) start on {link.name}", chan)
    found_rate = None
    inv: BusInventory | None = None
    for rate in bitrates:
        try:
            link.set_bitrate(rate, listen_only=True)
        except CanError as e:
            report["verdicts"].append(f"cannot open {rate} bit/s listen-only: {e}")
            continue
        label = f"{rate // 1000}k"
        session.note(f"identify phase 1: listen-only {listen_s:.1f}s @ {label}", chan)
        on_event(f"listening {listen_s:.0f}s @ {label} (listen-only) ...")
        inv = listen(link, session, listen_s, chan, on_event=on_event)
        inv.passive_ids = inv.pylontech_ids()
        res = {"frames": inv.frames, "error_frames": inv.error_frames, "ids": inv.summary(),
               "brand": inv.brand}
        if inv.first_error:
            res["first_error"] = inv.first_error
        report["passive"][label] = res
        verdicts, facts = _verdicts_for(label, inv)
        report["verdicts"] += verdicts
        report["facts"].update(facts)
        if inv.frames and found_rate is None:
            found_rate = rate
            report["facts"]["bitrate"] = rate
            break        # a talking bus found: no need to try other rates

    if active and found_rate and report["facts"].get("huawei_psu_addrs"):
        if report["facts"].get("huawei_controller_present"):
            msg = ("a controller is already commanding this PSU - skipping the status "
                   "request (use --passive-only, or stop the other controller).")
            report["verdicts"].append("ACTIVE PROBING SKIPPED: " + msg)
            on_event("!! " + msg)
        else:
            from .huawei import HuaweiClient, huawei_config
            addr = report["facts"]["huawei_psu_addrs"][0] or 1
            link.set_bitrate(found_rate, listen_only=False)
            cl = HuaweiClient(link, session, chan, addr, hcfg or huawei_config({}), on_event)
            snap = cl.status()
            info = cl.info()
            report["active"].append({"protocol": "huawei", "addr": addr, "status": snap,
                                     "info": info})
            if snap.get("complete"):
                report["verdicts"].append(
                    f"RESPONDER: Huawei PSU addr {addr} answers 0x40: out "
                    f"{snap.get('out_voltage_v')} V / {snap.get('out_current_a')} A, "
                    f"AC {snap.get('ac_in_voltage_v')} V, limit ratio "
                    f"{snap.get('current_limit_ratio')}, flags {snap.get('status_flags')}"
                    + (f"; nominal {info.get('nominal_current_a')} A, slot "
                       f"{info.get('slot_id')}, {info.get('elabel', {}).get('BoardType')}"
                       if info.get("nominal_current_a") else ""))
            else:
                report["verdicts"].append(f"Huawei PSU addr {addr}: no complete status burst "
                                          f"({snap.get('frames', 0)} frames) - ACK/termination?")
            link.set_bitrate(found_rate, listen_only=True)

    bat_present = inv is not None and inv.battery_present()
    if active and found_rate and bat_present:
        if inv.inverter_present():
            msg = ("an inverter/GX keeps this battery bus alive - staying passive (no 0x305/"
                   "0x307 sent).")
            report["verdicts"].append("ACTIVE PROBING SKIPPED: " + msg)
            on_event("!! " + msg)
        else:
            on_event("no inverter keep-alive heard - sending 0x305 + 0x307 for 3 s to unlock "
                     "the extension frames ...")
            link.set_bitrate(found_rate, listen_only=False)
            before = set(inv.ids)
            reps = max(1, int(round(unlock_s)))
            res = send_unlock(link, session, chan, repeats=reps, gap_s=unlock_s / reps,
                              inv=inv, on_event=on_event)
            link.set_bitrate(found_rate, listen_only=True)
            res["new_ids"] = sorted(set(inv.ids) - before)
            report["active"].append({"protocol": "pylontech-can", "unlock": res,
                                     "snapshot": snapshot(inv)})
            if res["acked"] == 0:
                report["verdicts"].append("UNLOCK: our 0x305/0x307 got no ACK - nobody "
                                          "acknowledges, check termination / listen-only")
            elif res["new_ids"]:
                report["verdicts"].append(f"UNLOCK: extension frames appeared after 0x305+0x307: "
                                          f"{res['new_ids']}")
            else:
                report["verdicts"].append("UNLOCK: 0x305+0x307 acknowledged, no extension frames "
                                          "within 3 s (pack firmware without 2.4 extensions, or "
                                          "needs the 'VIC' ident)")
            report["facts"]["extension_ids"] = res["new_ids"]
    if not report["verdicts"]:
        report["verdicts"].append(
            "SILENCE: no frames and no error frames at any bitrate. A CAN node that is "
            "powered sends something within seconds (Huawei: unsolicited every ~0.4 s, "
            "Pylontech: every 1 s). Check CAN-H/CAN-L (Huawei: top/white = CAN-L, "
            "bottom/black = CAN-H), the socket (battery CAN pins 4/5), power, and whether "
            "the device needs a keep-alive before it talks.")
    session.note("identify (CAN) done: " + " | ".join(report["verdicts"]), chan)
    return report
