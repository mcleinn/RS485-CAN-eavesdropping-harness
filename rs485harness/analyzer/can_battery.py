"""CAN battery side of identify/discover/dump/soc: passive inventory of
the bus, dialect guess, optional extension unlock, snapshots.

Rule carried over from RS485: the harness never becomes a second master.
On a battery bus the "master" is the inverter's keep-alive (0x305); the
unlock (0x305 + 0x307) is sent only when no inverter keep-alive was heard.
"""
from __future__ import annotations

import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..core.session import SessionWriter
from ..protocols.pylontech_can import frames as PF
from ..protocols.pylontech_can.dialects import CAN_BATTERY_PROFILES, guess_dialect
from ..protocols.huawei_r48xx import regs as HR


def decode_any(msg: CanMsg) -> dict | None:
    """One decoder entry point for every CAN command: Huawei R48xx first
    (29-bit proto 0x20/0x21), then the Pylontech family / Dyness native."""
    if msg.err:
        return None
    dec = HR.decode(msg) if msg.ext else None
    if dec is None:
        dec = PF.decode(msg)
    return dec


class BusInventory:
    """Per-identifier statistics plus the latest decoded content."""

    def __init__(self):
        self.ids: dict[str, dict] = {}
        self.frames = 0
        self.error_frames = 0
        self.first_error: str | None = None
        self.brand: str | None = None
        self.native_seen = False
        self.ext_35x_seen = False
        self.inverter_ids: set[str] = set()
        self.strings: set[int] = set()
        self.latest: dict[str, dict] = {}     # name -> decoded (BMS->INV)
        self.bitmap_seen = False
        self.passive_ids: set[int] | None = None

    def feed(self, msg: CanMsg, dec: dict | None) -> None:
        if msg.err:
            self.error_frames += 1
            self.first_error = self.first_error or msg.describe_error()
            return
        self.frames += 1
        e = self.ids.setdefault(msg.id_str, {"count": 0, "ext": msg.ext, "dlcs": set(),
                                             "times": [], "last_hex": "", "name": None,
                                             "flow": None})
        e["count"] += 1
        e["dlcs"].add(msg.dlc)
        e["times"].append(msg.ts)
        e["last_hex"] = msg.data.hex(" ").upper()
        if dec:
            e["name"] = dec.get("name") or dec.get("kind")
            e["flow"] = dec.get("flow")
            e["proto"] = dec.get("proto")
            if dec.get("proto") == "dyness-native":
                self.native_seen = True
            if dec.get("ext") and dec.get("proto") == "pylontech-can":
                self.ext_35x_seen = True
            if dec.get("string"):
                self.strings.add(dec["string"])
            if dec.get("flow") == "INV->BMS":
                self.inverter_ids.add(msg.id_str)
            elif dec.get("flow") == "BMS->INV":
                key = dec["name"] + (f"@{dec['string']}" if dec.get("string") else "")
                self.latest[key] = dec
            if dec.get("name") == "brand":
                self.brand = dec.get("brand")
            if dec.get("online_bitmap(seplos)"):
                self.bitmap_seen = True

    def summary(self) -> dict:
        out = {}
        for k, e in self.ids.items():
            t = e["times"]
            period = None
            if len(t) >= 3:
                gaps = sorted(t[i + 1] - t[i] for i in range(len(t) - 1))
                period = round(gaps[len(gaps) // 2], 3)
            out[k] = {"count": e["count"], "ext": e["ext"], "dlcs": sorted(e["dlcs"]),
                      "period_s": period, "last": e["last_hex"], "name": e["name"],
                      "flow": e["flow"], "proto": e.get("proto")}
        return out

    def pylontech_ids(self) -> set[int]:
        return {int(k, 16) & 0xFFF if e["ext"] and e.get("proto") == "pylontech-can" else int(k, 16)
                for k, e in self.ids.items() if e.get("proto") == "pylontech-can"}

    def battery_present(self) -> bool:
        return bool(self.pylontech_ids() & PF.BATTERY_IDS) or self.native_seen

    def inverter_present(self) -> bool:
        return bool(self.inverter_ids)

    def guess(self) -> list[dict]:
        return guess_dialect(self.pylontech_ids(), self.brand, self.ext_35x_seen,
                             self.native_seen, self.passive_ids, self.bitmap_seen)

    def quirks(self, profile_name: str | None = None) -> list[str]:
        q = []
        p = CAN_BATTERY_PROFILES.get(profile_name or "pylontech-lv")
        for k, e in self.ids.items():
            if e.get("proto") != "pylontech-can":
                continue
            base = int(k, 16) & 0xFFF if e["ext"] else int(k, 16)
            exp = p.dlcs.get(base)
            if exp and sorted(e["dlcs"]) != [exp]:
                q.append(f"0x{base:03X} DLC {sorted(e['dlcs'])} (profile {p.name} expects {exp})")
        if self.ext_35x_seen:
            q.append("0x35x frames carry 29-bit identifiers (Dyness Junior Box style)")
        br = self.latest.get("brand")
        if br and br.get("brand_padded", "").endswith("202020"):
            q.append("brand padded with spaces ('PYLON   ')")
        if br and br.get("brand") and br["brand"] not in ("PYLON", "DYNESS-L"):
            q.append(f"brand string {br['brand']!r}")
        if self.strings:
            q.append(f"LV-HUB string addresses seen: {sorted(self.strings)}")
        if self.error_frames:
            q.append(f"{self.error_frames} error frames ({self.first_error})")
        return q


def listen(link: CanLink, session: SessionWriter, seconds: float, chan: str,
           inv: BusInventory | None = None, on_event=None, mode: str = "tap") -> BusInventory:
    inv = inv or BusInventory()
    on_event = on_event or (lambda line: None)
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        msg = link.recv(timeout=0.1)
        if msg is None or msg.echo:
            continue
        dec = decode_any(msg)
        log_can(session, chan, "rx", msg, decoded=dec, flow=dec["flow"] if dec else None, mode=mode)
        first = msg.id_str not in inv.ids
        inv.feed(msg, dec)
        if first and not msg.err:
            on_event(f"  heard: {msg}" + (f"  = {dec.get('name') or dec.get('kind')}" if dec else ""))
    return inv


def send_unlock(link: CanLink, session: SessionWriter, chan: str, victron: bool = False,
                repeats: int = 3, gap_s: float = 1.0, inv: BusInventory | None = None,
                on_event=None) -> dict:
    """Act as the inverter for a few seconds: 0x305 + 0x307 every second,
    listening in between (Pylontech sends the extension frames every 2 s
    once unlocked). Returns what arrived while we were sending."""
    inv = inv or BusInventory()
    res = {"sent": 0, "acked": 0, "victron_ident": victron}
    for _ in range(repeats):
        for msg in (PF.encode_keepalive(), PF.encode_inverter_ident(victron=victron)):
            msg.ts = time.perf_counter()
            acked = True
            try:
                link.send(msg, timeout=0.3)
            except CanTxTimeout:
                acked = False
            except CanError as e:
                res["error"] = str(e)
                return res
            res["sent"] += 1
            res["acked"] += int(acked)
            log_can(session, chan, "tx", msg, decoded=PF.decode(msg), flow="INV->BMS",
                    acked=acked, mode="probe", note="unlock extension frames")
        listen(link, session, gap_s, chan, inv, on_event, mode="probe")
    return res


def snapshot(inv: BusInventory) -> dict:
    """Battery state merged from the latest frame of each kind."""
    snap: dict = {}
    for key, dec in inv.latest.items():
        name = key.split("@")[0]
        string = dec.get("string", 0)
        target = snap if not string else snap.setdefault("strings", {}).setdefault(str(string), {})
        for k, v in dec.items():
            if k in ("proto", "id", "name", "dlc", "flow", "string", "ext", "raw_hex", "brand_padded"):
                continue
            target[k] = v
        target.setdefault("frames_seen", []).append(dec["id"])
    if "max_cell_mv" in snap:
        snap["max_cell_v"] = round(snap["max_cell_mv"] / 1000, 3)
    if "min_cell_mv" in snap:
        snap["min_cell_v"] = round(snap["min_cell_mv"] / 1000, 3)
    if "cells_mv" in snap:
        cells = list(snap["cells_mv"])
        if snap.get("cell5_mv"):
            cells.append(snap["cell5_mv"])
        snap["cells_v"] = [round(c / 1000, 3) for c in cells]
    return snap


def discover_can(link: CanLink, session: SessionWriter, chan: str = "disc",
                 listen_s: float = 6.0, active: bool = True, on_event=None,
                 unlock_s: float = 3.0) -> dict:
    """CAN variant of `rsh discover`: inventory of every identifier with
    cadence/DLC/decoded content, dialect ranking, quirks, extension unlock
    when allowed, and the merged state snapshot."""
    on_event = on_event or (lambda line: None)
    report: dict = {"link": link.name, "bus": "can", "bitrate": link.bitrate}
    session.note(f"discover (CAN) start on {link.name} @ {link.bitrate}", chan)
    link.set_bitrate(link.bitrate, listen_only=True)
    on_event(f"listening {listen_s:.0f}s @ {link.bitrate // 1000}k (listen-only) ...")
    inv = listen(link, session, listen_s, chan, on_event=on_event)
    inv.passive_ids = inv.pylontech_ids()
    huawei = [k for k, e in inv.ids.items() if e.get("proto") == "huawei-r48xx"]
    if huawei and not inv.battery_present():
        report["result"] = ("Huawei R48xx PSU on this bus - use `rsh huawei status/info` "
                            "for its inventory")
        report["ids"] = inv.summary()
        return report
    if not inv.frames:
        report["result"] = ("nothing heard" + (f" ({inv.error_frames} error frames: wrong "
                                              f"bitrate?)" if inv.error_frames else "")
                            + " - run `rsh identify` to find the bitrate")
        report["ids"] = inv.summary()
        session.note("discover (CAN): nothing heard", chan)
        return report
    report["inverter_present"] = inv.inverter_present()
    report["inverter_ids"] = sorted(inv.inverter_ids)
    if active and inv.battery_present() and not inv.inverter_present():
        on_event("no inverter keep-alive heard - sending 0x305 + 0x307 to unlock the extension frames ...")
        link.set_bitrate(link.bitrate, listen_only=False)
        before = set(inv.ids)
        reps = max(1, int(round(unlock_s)))
        report["unlock"] = send_unlock(link, session, chan, repeats=reps,
                                       gap_s=unlock_s / reps, inv=inv, on_event=on_event)
        link.set_bitrate(link.bitrate, listen_only=True)
        report["unlock"]["new_ids"] = sorted(set(inv.ids) - before)
    elif active and inv.inverter_present():
        on_event("an inverter is keeping this bus alive - staying passive (no unlock sent)")
        report["unlock"] = {"skipped": "inverter present"}
    report["ids"] = inv.summary()
    report["brand"] = inv.brand
    ranking = inv.guess()
    report["dialects"] = ranking
    best = ranking[0]["name"] if ranking else None
    report["dialect"] = best
    report["quirks"] = inv.quirks(best)
    report["snapshot"] = snapshot(inv)
    report["mandatory_missing"] = {
        name: [f"0x{i:03X}" for i in p.mandatory_ids if i not in inv.pylontech_ids()]
        for name, p in CAN_BATTERY_PROFILES.items()}
    report["result"] = "ok"
    session.note(f"discover (CAN) done: dialect {best}, {len(inv.ids)} ids", chan)
    return report


def format_discovery_can(report: dict) -> str:
    lines = [f"CAN DISCOVERY on {report.get('link')} @ {report.get('bitrate', '?')} bit/s: "
             f"{report.get('result')}"]
    if report.get("result") != "ok":
        return "\n".join(lines)
    lines.append(f"  inverter keep-alive present: {report['inverter_present']} "
                 f"{report.get('inverter_ids')}")
    if "unlock" in report:
        lines.append(f"  extension unlock: {report['unlock']}")
    lines.append(f"  brand: {report.get('brand')!r}; dialect ranking:")
    for d in report["dialects"][:3]:
        lines.append(f"    {d['name']:14} score {d['score']:3}  {'; '.join(d['reasons'])}")
    lines.append("  identifiers:")
    for k, e in sorted(report["ids"].items()):
        lines.append(f"    {k:12} x{e['count']:<4} every {e['period_s']!s:>6} s  DLC {e['dlcs']}  "
                     f"{e['name'] or '?':22} {e['last']}")
    if report.get("quirks"):
        lines.append("  quirks: " + "; ".join(report["quirks"]))
    snap = report.get("snapshot") or {}
    keys = ("brand", "soc_percent", "soh_percent", "voltage_v", "current_a", "temperature_c",
            "cvl_v", "ccl_a", "dcl_a", "dvl_v", "charge_enable", "discharge_enable",
            "force_charge_1", "full_charge_request", "protections", "alarms", "module_count",
            "min_cell_v", "max_cell_v", "installed_ah_u16", "installed_ah_u32")
    lines.append("  state: " + ", ".join(f"{k}={snap[k]}" for k in keys if k in snap))
    return "\n".join(lines)


def dump_can(link: CanLink, session: SessionWriter, chan: str = "dump",
             listen_s: float = 4.0, active: bool = True, on_event=None,
             unlock_s: float = 3.0) -> dict:
    """CAN variant of `rsh dump`: the battery snapshot from its broadcasts
    (+ extension / Dyness native frames when available)."""
    rep = discover_can(link, session, chan, listen_s, active, on_event, unlock_s)
    out = {"link": rep.get("link"), "bitrate": rep.get("bitrate"), "result": rep.get("result"),
           "dialect": rep.get("dialect"), "brand": rep.get("brand"),
           "snapshot": rep.get("snapshot", {}), "ids_seen": sorted((rep.get("ids") or {}).keys()),
           "quirks": rep.get("quirks", [])}
    return out


def format_dump_can(report: dict) -> str:
    snap = report.get("snapshot") or {}
    lines = [f"CAN BATTERY DUMP ({report.get('dialect')}, brand {report.get('brand')!r}): "
             f"{report.get('result')}"]
    for k in sorted(snap):
        if k in ("frames_seen", "strings"):
            continue
        lines.append(f"  {k:28} {snap[k]}")
    for s, sub in (snap.get("strings") or {}).items():
        lines.append(f"  string {s}: " + ", ".join(f"{k}={v}" for k, v in sub.items()
                                                   if k != "frames_seen"))
    if report.get("quirks"):
        lines.append("  quirks: " + "; ".join(report["quirks"]))
    return "\n".join(lines)


def soc_can(link: CanLink, session: SessionWriter, chan: str = "soc",
            listen_s: float = 3.0, on_event=None) -> dict:
    """Quick SOC/V/I table from 0x355/0x356 (one row per LV-HUB string)."""
    link.set_bitrate(link.bitrate, listen_only=True)
    inv = listen(link, session, listen_s, chan, on_event=on_event)
    snap = snapshot(inv)
    rows = []
    if "soc_percent" in snap or "voltage_v" in snap:
        rows.append({"string": 0, "soc_percent": snap.get("soc_percent"),
                     "soh_percent": snap.get("soh_percent"), "voltage_v": snap.get("voltage_v"),
                     "current_a": snap.get("current_a"), "temperature_c": snap.get("temperature_c"),
                     "module_count": snap.get("module_count")})
    for s, sub in sorted((snap.get("strings") or {}).items()):
        rows.append({"string": int(s), **{k: sub.get(k) for k in ("soc_percent", "soh_percent",
                                                                   "voltage_v", "current_a",
                                                                   "temperature_c", "module_count")}})
    return {"rows": rows, "brand": inv.brand, "frames": inv.frames,
            "error_frames": inv.error_frames}
