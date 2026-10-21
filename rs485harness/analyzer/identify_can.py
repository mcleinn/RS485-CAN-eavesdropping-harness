"""`rsh identify` for CAN links: listen-only per bitrate, inventory of the
identifiers seen, verdicts; one polite request only when nobody else is
talking to the device (same rule as RS485: never become a second master).

Recognised so far: Huawei R48xx rectifiers (29-bit proto 0x21/0x20) and the
11-bit Pylontech/Victron/Dyness/LuxPower battery set (0x351/0x355/0x356/
0x359/0x35C/0x35E, inverter keep-alive 0x305/0x307). Error frames without
valid frames mean the wrong bitrate (a CAN controller never produces
garbled frames at the wrong rate - it produces stuff/form errors).
"""
from __future__ import annotations

import time

from ..core.canbus import CanLink, CanMsg, BITRATE_SWEEP, CanError, log_can
from ..core.session import SessionWriter
from ..protocols.huawei_r48xx import regs as HR
from ..protocols.huawei_r48xx.ids import HuaweiId, looks_huawei, CMD_UNSOL

PYLON_IDS = {0x351: "limits", 0x355: "soc/soh", 0x356: "V/I/T", 0x359: "alarms",
             0x35A: "victron-alarms", 0x35C: "requests", 0x35E: "brand", 0x35F: "info",
             0x305: "inverter-keepalive", 0x307: "victron-handshake",
             0x370: "name1", 0x371: "name2", 0x372: "modules", 0x373: "cell-extremes",
             0x374: "min-cell-id", 0x375: "max-cell-id", 0x376: "min-temp-id",
             0x377: "max-temp-id", 0x378: "energy", 0x379: "installed-ah"}


def _listen_can(link: CanLink, seconds: float, session: SessionWriter, chan: str,
                on_event) -> dict:
    ids: dict[str, dict] = {}
    errors = 0
    overflow = 0
    first_err: str | None = None
    brand = None
    t_end = time.monotonic() + seconds
    n = 0
    while time.monotonic() < t_end:
        msg = link.recv(timeout=0.1)
        if msg is None or msg.echo:
            continue
        dec = HR.decode(msg)
        log_can(session, chan, "rx", msg, decoded=dec, flow=dec["flow"] if dec else None,
                mode="tap")
        if msg.err:
            errors += 1
            first_err = first_err or msg.describe_error()
            continue
        if msg.extra.get("overflow"):
            overflow += 1
        n += 1
        e = ids.setdefault(msg.id_str, {"count": 0, "ext": msg.ext, "dlcs": set(),
                                        "times": [], "last_hex": ""})
        e["count"] += 1
        e["dlcs"].add(msg.dlc)
        e["times"].append(msg.ts)
        e["last_hex"] = msg.data.hex(" ").upper()
        if not msg.ext and msg.can_id == 0x35E:
            brand = bytes(msg.data).rstrip(b"\0 ").decode("ascii", "replace")
        if e["count"] == 1:
            on_event(f"  heard: {msg}" + (f"  = {dec['cmd']} from psu{dec['addr']}"
                                          if dec else ""))
    out: dict = {"frames": n, "error_frames": errors, "overflow": overflow,
                 "ids": {}, "brand": brand}
    if first_err:
        out["first_error"] = first_err
    for k, e in ids.items():
        t = e["times"]
        period = None
        if len(t) >= 3:
            gaps = sorted(t[i + 1] - t[i] for i in range(len(t) - 1))
            period = round(gaps[len(gaps) // 2], 3)
        can_id = int(k, 16)
        label = None
        if e["ext"] and looks_huawei(can_id, True):
            label = "huawei " + str(HuaweiId.unpack(can_id))
        elif not e["ext"] and can_id in PYLON_IDS:
            label = "pylontech-can " + PYLON_IDS[can_id]
        out["ids"][k] = {"count": e["count"], "ext": e["ext"], "dlcs": sorted(e["dlcs"]),
                         "period_s": period, "last": e["last_hex"], "label": label}
    return out


def _verdicts_for(label: str, res: dict) -> tuple[list[str], dict]:
    verdicts: list[str] = []
    facts: dict = {}
    ids = res["ids"]
    if res["frames"] == 0:
        if res["error_frames"]:
            verdicts.append(f"ERROR FRAMES ONLY @ {label}: {res['error_frames']} in the window "
                            f"({res.get('first_error')}) - wrong bitrate for this bus, or "
                            f"missing termination / only one node.")
        return verdicts, facts
    huawei = {k: v for k, v in ids.items() if v["label"] and v["label"].startswith("huawei")}
    if huawei:
        psus = sorted({HuaweiId.unpack(int(k, 16)).addr for k in huawei})
        ctl = [k for k in huawei if HuaweiId.unpack(int(k, 16)).to_psu
               and HuaweiId.unpack(int(k, 16)).cmd != CMD_UNSOL]
        unsol = [v for k, v in huawei.items() if HuaweiId.unpack(int(k, 16)).cmd == CMD_UNSOL
                 and not HuaweiId.unpack(int(k, 16)).to_psu]
        period = unsol[0]["period_s"] if unsol else None
        facts.update({"huawei_psu_addrs": psus, "huawei_controller_present": bool(ctl),
                      "huawei_unsolicited_period_s": period})
        verdicts.append(f"HUAWEI R48xx PSU @ {label}: address(es) {psus}, unsolicited "
                        f"frame every ~{period} s" + (f"; {len(ctl)} controller id(s) seen "
                                                      f"- another controller is talking, do "
                                                      f"NOT send" if ctl else
                                                      "; no controller on the bus"))
    pylon = {k: v for k, v in ids.items() if v["label"] and v["label"].startswith("pylontech")}
    if pylon:
        keep = [k for k in pylon if int(k, 16) in (0x305, 0x307)]
        bat = [k for k in pylon if int(k, 16) not in (0x305, 0x307)]
        facts.update({"pylontech_ids": sorted(pylon), "brand": res.get("brand"),
                      "inverter_keepalive": bool(keep)})
        if bat:
            per = pylon[bat[0]]["period_s"]
            verdicts.append(f"PYLONTECH-CAN BATTERY @ {label}: ids {sorted(bat)} every "
                            f"~{per} s, brand {res.get('brand')!r}"
                            + (f"; inverter/GX keep-alive {sorted(keep)} present"
                               if keep else "; no inverter keep-alive seen"))
        elif keep:
            verdicts.append(f"INVERTER/GX KEEP-ALIVE ONLY @ {label}: {sorted(keep)} - an "
                            f"inverter polls, no battery answers.")
    other = [k for k, v in ids.items() if not v["label"]]
    if other:
        verdicts.append(f"UNKNOWN IDS @ {label}: {other[:12]}"
                        + (" ..." if len(other) > 12 else ""))
    if res["error_frames"]:
        verdicts.append(f"note: {res['error_frames']} error frames beside {res['frames']} "
                        f"valid frames @ {label} ({res.get('first_error')}).")
    return verdicts, facts


def identify_can(link: CanLink, session: SessionWriter, chan: str = "id",
                 listen_s: float = 5.0, active: bool = True,
                 bitrates=BITRATE_SWEEP, on_event=None, hcfg: dict | None = None) -> dict:
    on_event = on_event or (lambda line: None)
    report: dict = {"channel": chan, "link": link.name, "bus": "can", "passive": {},
                    "active": [], "verdicts": [], "facts": {}}
    session.note(f"identify (CAN) start on {link.name}", chan)
    found_rate = None
    for rate in bitrates:
        try:
            link.set_bitrate(rate, listen_only=True)
        except CanError as e:
            report["verdicts"].append(f"cannot open {rate} bit/s listen-only: {e}")
            continue
        label = f"{rate // 1000}k"
        session.note(f"identify phase 1: listen-only {listen_s:.1f}s @ {label}", chan)
        on_event(f"listening {listen_s:.0f}s @ {label} (listen-only) ...")
        res = _listen_can(link, listen_s, session, chan, on_event)
        report["passive"][label] = res
        verdicts, facts = _verdicts_for(label, res)
        report["verdicts"] += verdicts
        report["facts"].update(facts)
        if res["frames"] and found_rate is None:
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
    if not report["verdicts"]:
        report["verdicts"].append(
            "SILENCE: no frames and no error frames at any bitrate. A CAN node that is "
            "powered sends something within seconds (Huawei: unsolicited every ~0.4 s, "
            "Pylontech: every 1 s). Check CAN-H/CAN-L (Huawei: top/white = CAN-L, "
            "bottom/black = CAN-H), the socket (battery CAN pins 4/5), power, and whether "
            "the device needs a keep-alive before it talks.")
    session.note("identify (CAN) done: " + " | ".join(report["verdicts"]), chan)
    return report
