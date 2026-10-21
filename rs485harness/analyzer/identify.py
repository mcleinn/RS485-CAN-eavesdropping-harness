"""'Who is there?' - first thing to run after plugging in a channel.

Phase 1 (passive): listen at each candidate baud rate; report whether
anything talks spontaneously (an inverter polling), in which protocol, at
what cadence.

Phase 2 (active, optional): politely interrogate for a battery/BMS:
- Pylontech ASCII requests across ADR x VER x {0x42, 0x61} (+identity reads
  0x51/0x93 on success)
- Modbus RTU reads for the Voltronic 'LIB' register map (slave 1 @ 9600,
  registers 0x0000+) and Seplos BMS 3.0 native (slave 0 @ 19200, PIA
  0x1000+)

Result: a structured report saying who answered, at which baud/address,
which dialect, and how fast.
"""
from __future__ import annotations

import time

from ..core.transport import Link, SerialLink, FrameAccumulator
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P
from .classify import classify_raw, crc16_modbus, find_modbus_frames

DEFAULT_BAUDS = (9600, 19200, 115200)
# 0x12 = master-group address for the system 6x commands per the V3.5/Deye
# doc ("only addresses 12,22,32,... are valid" for 0x60-0x64)
DEFAULT_ADRS = (0, 1, 2, 0x12)
DEFAULT_VERS = (0x20, 0x25)


def _listen(link: Link, seconds: float, session: SessionWriter, chan: str,
            on_event) -> dict:
    acc = FrameAccumulator()
    frames: list[F.ParsedFrame] = []
    frame_times: list[float] = []
    raw_bytes = b""
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        chunk = link.read(timeout=0.1)
        events = acc.feed(chunk) if chunk else []
        stale = acc.flush_if_stale(time.perf_counter())
        if stale:
            events.append(stale)
        for ev in events:
            if ev.kind == "frame":
                pf = F.parse_frame(ev.data)
                frames.append(pf)
                frame_times.append(ev.ts)
                session.log(chan, "rx", ev.data, "frame", decoded=pf.to_dict(),
                            errors=pf.errors)
                on_event(f"  heard: {pf.describe('request')}")
            else:
                raw_bytes += ev.data
                session.log(chan, "rx", ev.data, "raw")
    out: dict = {"frames": len(frames), "raw_bytes": len(raw_bytes)}
    if frames:
        good = [f for f in frames if f.ok]
        out["pylontech"] = {
            "clean_frames": len(good),
            "vers": sorted({f.ver for f in frames if f.ver is not None}),
            "adrs": sorted({f.adr for f in frames if f.adr is not None}),
            "cid1s": sorted({f.cid1 for f in frames if f.cid1 is not None}),
            "cid2s": sorted({f"{f.cid2:02X}" for f in frames if f.cid2 is not None}),
            "families": sorted({F.cid2_family(f.cid2) for f in frames
                                if f.cid2 is not None}),
        }
        if len(frame_times) >= 3:
            gaps = [frame_times[i + 1] - frame_times[i]
                    for i in range(len(frame_times) - 1)]
            gaps.sort()
            out["pylontech"]["median_gap_s"] = round(gaps[len(gaps) // 2], 3)
    if raw_bytes:
        out["raw_classification"] = classify_raw(raw_bytes)
    return out


def _probe_pylontech(link: Link, ver: int, adr: int, cid2: int,
                     info: str, timeout: float = 0.5,
                     session: SessionWriter | None = None, chan: str = "id",
                     retries: int = 1):
    """Send one request, wait for one framed reply. Silence is retried
    (`retries` extra attempts) - real BMSes occasionally drop a request."""
    req = F.encode_frame(ver, adr, 0x46, cid2, info)
    for attempt in range(1 + retries):
        link.flush_input()
        t0 = time.perf_counter()
        link.write(req)
        if session:
            session.log(chan, "tx", req, "frame",
                        decoded=F.parse_frame(req).to_dict(),
                        note="probe" if attempt == 0 else f"probe retry {attempt}")
        acc = FrameAccumulator()
        deadline = t0 + timeout
        while time.perf_counter() < deadline:
            chunk = link.read(timeout=min(0.1, max(0.01, deadline - time.perf_counter())))
            if not chunk:
                continue
            for ev in acc.feed(chunk):
                if ev.kind == "frame":
                    pf = F.parse_frame(ev.data)
                    latency = round((ev.ts - t0) * 1000, 1)
                    if session:
                        session.log(chan, "rx", ev.data, "frame",
                                    decoded=pf.to_dict(), errors=pf.errors,
                                    note=f"probe answer, latency_ms={latency}")
                    return pf, latency
                elif session:
                    session.log(chan, "rx", ev.data, "raw", note="during probe")
    return None, None


def _probe_modbus(link: Link, slave: int, start: int, count: int,
                  timeout: float = 0.5, session: SessionWriter | None = None,
                  chan: str = "id", tag: str = "", function: int = 0x03):
    pdu = bytes([slave, function, start >> 8, start & 0xFF, count >> 8, count & 0xFF])
    crc = crc16_modbus(pdu)
    req = pdu + bytes([crc & 0xFF, crc >> 8])
    link.flush_input()
    t0 = time.perf_counter()
    link.write(req)
    if session:
        session.log(chan, "tx", req, "raw", note=f"modbus probe {tag}")
    buf = b""
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        chunk = link.read(timeout=min(0.1, max(0.01, deadline - time.perf_counter())))
        if chunk:
            buf += chunk.data
            hits = find_modbus_frames(buf)
            if hits:
                latency = round((time.perf_counter() - t0) * 1000, 1)
                if session:
                    session.log(chan, "rx", buf, "raw",
                                decoded={"verdict": "modbus-rtu",
                                         "modbus_frames": hits},
                                note=f"modbus probe answer {tag}, "
                                     f"latency_ms={latency}")
                return hits[0], latency, req
    if session and buf:
        session.log(chan, "rx", buf, "raw", note=f"during modbus probe {tag}")
    return None, None, req


def _modbus_response_words(hit: dict) -> list[int] | None:
    """Register values from a function-0x03/0x04 response frame, if any."""
    frame = bytes.fromhex(hit["hex"])
    if len(frame) >= 7 and frame[1] in (0x03, 0x04):
        n = frame[2]
        data = frame[3:3 + n]
        if len(data) == n and n and n % 2 == 0:
            return [int.from_bytes(data[i:i + 2], "big")
                    for i in range(0, n, 2)]
    return None


def identify(link: Link, session: SessionWriter, chan: str = "id",
             listen_s: float = 5.0, active: bool = True,
             bauds=DEFAULT_BAUDS, adrs=DEFAULT_ADRS, vers=DEFAULT_VERS,
             on_event=None) -> dict:
    on_event = on_event or (lambda line: None)
    if getattr(link, "is_can", False):
        from .identify_can import identify_can
        return identify_can(link, session, chan, listen_s=listen_s, active=active,
                            on_event=on_event)
    can_baud = isinstance(link, SerialLink)
    baud_list = list(bauds) if can_baud else [None]
    report: dict = {"channel": chan, "link": link.name, "passive": {},
                    "active": [], "verdicts": []}
    session.note(f"identify start on {link.name}", chan)

    # ---- Phase 1: passive listen
    for baud in baud_list:
        if baud:
            link.set_baud(baud)
            link.flush_input()
        label = str(baud or "current")
        session.note(f"identify phase 1: passive listen {listen_s:.1f}s "
                     f"@ {label} baud", chan)
        on_event(f"listening {listen_s:.0f}s @ {label} baud ...")
        res = _listen(link, listen_s, session, chan, on_event)
        report["passive"][label] = res
        if res.get("pylontech"):
            p = res["pylontech"]
            fam = "/".join(p["families"])
            report["verdicts"].append(
                f"ACTIVE TALKER @ {label} baud: Pylontech-ASCII frames "
                f"(VER {p['vers']}, ADR {p['adrs']}, CID2 {p['cid2s']}, "
                f"family {fam}, cadence ~{p.get('median_gap_s', '?')}s) - "
                f"this side is polling like an INVERTER/master.")
        elif res.get("raw_classification", {}).get("verdict") == "modbus-rtu":
            rc = res["raw_classification"]
            maps = rc.get("register_maps", [])
            advice = ""
            if "voltronic-lib" in maps:
                advice = (" -> This side speaks the Voltronic 'LIB' register "
                          "protocol, NOT Pylontech ASCII. If this is the "
                          "inverter, its selected battery type maps to LIB, "
                          "not PYL: either set the inverter battery type "
                          "(program 05) to 'PYL' so it polls Pylontech-ASCII, "
                          "or switch the battery to its Voltronic/LIB "
                          "profile, or bridge-translate.")
            elif "seplos-v3-native" in maps:
                advice = (" -> Seplos-native Modbus (PC/monitor protocol). "
                          "If this is the battery's port, you are on the "
                          "RS485-1/2 monitor socket or the PYLON setting is "
                          "not active - use the CAN-labelled socket / "
                          "re-check the RS485 protocol setting in the app.")
            report["verdicts"].append(
                f"ACTIVE TALKER @ {label} baud: Modbus RTU polling "
                f"({rc.get('modbus_summary', '')}) - a Modbus register "
                f"protocol, not Pylontech ASCII.{advice}")
        elif res["raw_bytes"]:
            rc = res["raw_classification"]
            report["verdicts"].append(
                f"NOISE @ {label} baud: {rc['verdict']} ({rc.get('hint', '')})")

    # ---- Phase 2: active interrogation
    if active:
        heard_master = any(v.get("pylontech") for v in report["passive"].values())
        if heard_master:
            # an inverter is polling on this bus: we must not become a second
            # master (collisions; the battery would answer OUR probes)
            msg = ("another master is polling this bus - skipping active "
                   "probes. Use --passive-only here, or probe the battery with "
                   "the inverter switched off / its RS485 unplugged.")
            report["verdicts"].append("ACTIVE PROBING SKIPPED: " + msg)
            session.note("identify: active phase skipped, master present", chan)
            on_event("!! " + msg)
            active = False
    if active:
        pylontech_probes = []
        for baud in baud_list:
            if baud:
                link.set_baud(baud)
                link.flush_input()
            label = str(baud or "current")
            session.note(f"identify phase 2: active probing @ {label} baud",
                         chan)
            for ver in vers:
                for adr in adrs:
                    for cid2, info in ((0x61, ""), (0x42, f"{max(adr, 1):02X}")):
                        pf, lat = _probe_pylontech(link, ver, adr, cid2, info,
                                                   session=session, chan=chan)
                        entry = {"protocol": "pylontech", "baud": label,
                                 "ver": f"{ver:02X}", "adr": adr,
                                 "cid2": f"{cid2:02X}",
                                 "answered": pf is not None}
                        if pf is not None:
                            entry.update({"latency_ms": lat,
                                          "response": pf.to_dict(),
                                          "rtn": pf.cid2,
                                          "clean": pf.ok})
                            on_event(f"  ANSWER pylontech @{label} VER {ver:02X} "
                                     f"ADR {adr:02X} CID2 {cid2:02X}: "
                                     f"RTN={pf.cid2:02X} ADR-in-resp={pf.adr:02X} "
                                     f"{lat}ms")
                            pylontech_probes.append((label, ver, adr, cid2, pf, lat))
                        report["active"].append(entry)

            # Modbus probes at this baud
            # Voltronic spec: common node id 0x01 (every BMS must answer it);
            # unique id is 0x0F when the ID switch is at 0. 0x0010 = cell
            # count, always implemented - the most reliable battery probe.
            for slave, start, count, tag, fn in (
                    (1, 0x0010, 4, "voltronic-LIB-cells", 0x03),
                    (1, 0x0010, 4, "voltronic-LIB-cells-fn04", 0x04),
                    (1, 0x0000, 4, "voltronic-LIB", 0x03),
                    (0x0F, 0x0010, 4, "voltronic-LIB-id0F", 0x03),
                    (0, 0x1000, 4, "seplos-v3-PIA-fn04", 0x04),
                    (1, 0x1000, 4, "seplos-v3-PIA-fn04", 0x04),
                    (0, 0x1000, 4, "seplos-v3-PIA", 0x03),
                    (1, 0x1000, 4, "seplos-v3-PIA", 0x03)):
                hit, lat, req = _probe_modbus(link, slave, start, count,
                                              session=session, chan=chan,
                                              tag=tag, function=fn)
                entry = {"protocol": "modbus", "baud": label, "slave": slave,
                         "start": f"{start:04X}", "probe": tag, "function": fn,
                         "answered": hit is not None}
                if hit and hit.get("exception") is not None:
                    entry["exception"] = hit["exception"]
                    entry["exception_name"] = hit["exception_name"]
                if hit:
                    entry.update({"latency_ms": lat, "response": hit})
                    words = _modbus_response_words(hit)
                    if words is not None:
                        entry["registers"] = words
                    on_event(f"  ANSWER modbus @{label} slave {slave} "
                             f"({tag}): {lat}ms regs={words}")
                report["active"].append(entry)

        # Verdicts from active probing
        if pylontech_probes:
            label, ver, adr, cid2, pf, lat = pylontech_probes[0]
            answered = {(l, a) for l, v, a, c, p_, lt in pylontech_probes}
            resp_adrs = sorted({p_.adr for *_x, p_, _lt in pylontech_probes})
            cid2s_ok = sorted({f"{c:02X}" for l, v, a, c, p_, lt in pylontech_probes
                               if p_.cid2 in (0x00, c)})
            report["verdicts"].append(
                f"RESPONDER: Pylontech-ASCII battery-like device @ {label} "
                f"baud; answers on request-ADR(s) "
                f"{sorted({a for _l, a in answered})}, responds with ADR(s) "
                f"{resp_adrs}, commands OK: {cid2s_ok}, latency ~{lat}ms.")
            # identity read
            pf51, _ = _probe_pylontech(link, ver, adr, 0x51, "",
                                       session=session, chan=chan)
            if pf51 is not None and pf51.info:
                try:
                    ident = P.decode_51(pf51.info)
                    report["identity"] = ident
                    report["verdicts"].append(
                        f"IDENTITY: device_name={ident['device_name']!r} "
                        f"manufacturer={ident['manufacturer']!r} "
                        f"sw={ident['software_version']}")
                except (ValueError, IndexError):
                    pass
        modbus_hits = [e for e in report["active"]
                       if e["protocol"] == "modbus" and e["answered"]]
        exc_only = [e for e in modbus_hits if e.get("exception") is not None]
        if exc_only and len(exc_only) == len(modbus_hits):
            codes = sorted({f"fn 0x{e['function']:02X} -> {e['exception_name']}"
                            for e in exc_only})
            report["verdicts"].append(
                f"RESPONDER: Modbus RTU device @ {exc_only[0]['baud']} baud "
                f"slave {exc_only[0]['slave']} answers only with EXCEPTIONS "
                f"({'; '.join(codes)}) - it is a live Modbus slave but does "
                f"not serve these functions/registers; the protocol it "
                f"implements is not the probed map.")
        modbus_hits = [e for e in modbus_hits if e.get("exception") is None]
        for e in modbus_hits:
            regs = e.get("registers")
            extra = ""
            if (regs and e["probe"].startswith("voltronic-LIB")
                    and e["start"] == "0010" and 1 <= regs[0] <= 32):
                extra = (f" Register 0x0010 reads back {regs[0]} cells - a "
                         f"battery in Voltronic/LIB mode confirmed. If the "
                         f"inverter side polls this same map, set the "
                         f"inverter to LIB; if the inverter polls "
                         f"Pylontech-ASCII instead, switch the battery back "
                         f"to PYLON.")
            if regs and e["probe"].startswith("seplos-v3"):
                report["verdicts"].append(
                    f"RESPONDER: Seplos BMS 3.0 SERVICE socket (native "
                    f"Modbus) @ {e['baud']} baud, device {e['slave']} - "
                    f"settings and cell data are readable here: "
                    f"`rsh discover --port <COM> --baud {e['baud']}`. This "
                    f"is not the inverter socket (that one answers "
                    f"Pylontech-ASCII at 9600).")
                continue
            report["verdicts"].append(
                f"RESPONDER: Modbus RTU device @ {e['baud']} baud slave "
                f"{e['slave']} ({e['probe']}"
                + (f", registers={regs}" if regs else "")
                + ") - Voltronic-LIB or Seplos-native style BMS." + extra)

    if not report["verdicts"]:
        report["verdicts"].append(
            "SILENCE: nothing heard and nothing answered at any baud. "
            "Check physical layer first: correct inverter socket for THIS "
            "battery (pinout varies by generation - on the Mason 280L 3.0 "
            "RS485 is on the RS485 sockets, A=2/7 B=1/8, while pins 4/5 are "
            "CAN), A/B polarity, crossover pinout, device powered/enabled. "
            "Try each socket.")
    session.note("identify done: " + " | ".join(report["verdicts"]), chan)
    return report
