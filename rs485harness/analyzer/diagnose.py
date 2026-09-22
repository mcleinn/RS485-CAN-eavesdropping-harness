"""Hypothesis-driven diagnosis over a captured session JSONL.

Walks the capture once, pairs requests with responses per channel, then
evaluates the failure-hypothesis matrix and emits findings with severity:
  info  - observation
  warn  - suspicious, worth checking
  fail  - identified problem
"""
from __future__ import annotations

from pathlib import Path

from ..core.session import read_session
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P
from .classify import classify_raw

PAIR_WINDOW_S = 1.5


def _is_request(dec: dict) -> bool:
    cid2 = dec.get("cid2")
    return (cid2 in F.CID2_NAMES and len(dec.get("info", "")) <= 4)


def analyze(path: Path | str) -> dict:
    chans: dict[str, dict] = {}
    exchanges: list[dict] = []
    pending: dict[str, dict] = {}  # chan -> open request

    for rec in read_session(path):
        chan = rec.get("chan", "-")
        c = chans.setdefault(chan, {
            "frames": 0, "raw_bytes": 0, "frame_errors": 0,
            "raw_verdicts": {}, "vers": set(), "req_adrs": set(),
            "resp_adrs": set(), "req_cid2": {}, "notes": 0})
        kind = rec.get("kind")
        if kind == "note":
            c["notes"] += 1
            continue
        if kind == "raw":
            n = len(rec.get("hex", "")) // 2
            c["raw_bytes"] += n
            cls = rec.get("decoded")
            if not cls or "verdict" not in cls:
                # tool logged raw bytes without classifying - do it here
                try:
                    cls = classify_raw(bytes.fromhex(rec.get("hex", "")))
                except ValueError:
                    cls = {"verdict": "unclassified"}
            v = cls.get("verdict", "unclassified")
            c["raw_verdicts"][v] = c["raw_verdicts"].get(v, 0) + 1
            if v == "modbus-rtu":
                c.setdefault("modbus_maps", set()).update(
                    cls.get("register_maps", []))
                if cls.get("modbus_summary"):
                    c["modbus_summary"] = cls["modbus_summary"]
            continue
        if kind != "frame":
            continue
        c["frames"] += 1
        dec = rec.get("decoded") or {}
        errs = rec.get("errors") or dec.get("errors") or []
        if errs:
            c["frame_errors"] += 1
        if dec.get("ver") is not None:
            c["vers"].add(dec["ver"])

        t = rec.get("t", 0.0)
        open_req = pending.get(chan)
        if open_req and t - open_req["t"] > PAIR_WINDOW_S:
            # previous poll on this channel expired unanswered
            exchanges.append({"chan": chan, "req": open_req["dec"],
                              "resp": None, "latency_ms": None})
            pending.pop(chan)

        if _is_request(dec):
            if pending.get(chan) is not None:
                # a new poll while one is open: previous went unanswered
                prev = pending.pop(chan)
                exchanges.append({"chan": chan, "req": prev["dec"],
                                  "resp": None, "latency_ms": None})
            pending[chan] = {"t": t, "dec": dec}
            c["req_adrs"].add(dec.get("adr"))
            key = f"{dec.get('cid2'):02X}" if dec.get("cid2") is not None else "?"
            c["req_cid2"][key] = c["req_cid2"].get(key, 0) + 1
        elif pending.get(chan) is not None:
            req = pending.pop(chan)
            c["resp_adrs"].add(dec.get("adr"))
            exchanges.append({"chan": chan, "req": req["dec"], "resp": dec,
                              "latency_ms": round((t - req["t"]) * 1000, 1),
                              "resp_errors": errs})
    for chan, req in pending.items():
        exchanges.append({"chan": chan, "req": req["dec"], "resp": None,
                          "latency_ms": None})

    findings: list[dict] = []

    def find(hyp: str, severity: str, detail: str) -> None:
        findings.append({"hypothesis": hyp, "severity": severity,
                         "detail": detail})

    # H1: silence per channel
    for chan, c in chans.items():
        if c["frames"] == 0 and c["raw_bytes"] == 0:
            find("H1-silence", "fail",
                 f"[{chan}] nothing on the wire at all - wrong socket, dead "
                 f"cable, A/B swap, or device not transmitting. On the "
                 f"Seplos side remember: the inverter port is the RJ45 "
                 f"labelled CAN (RS485 on pins 4/5).")

    # H2: garbage classifier (wrong baud etc.)
    for chan, c in chans.items():
        noise = c["raw_verdicts"].get("line-noise", 0)
        binary = c["raw_verdicts"].get("binary-unknown", 0)
        if (noise + binary) > 0 and c["frames"] == 0:
            find("H2-baud-or-polarity", "fail",
                 f"[{chan}] only unframed garbage ({dict(c['raw_verdicts'])}) "
                 f"- typical for baud mismatch or A/B polarity swap. Re-capture "
                 f"at other baud rates (rsh identify does this automatically).")
        if c["raw_verdicts"].get("modbus-rtu"):
            maps = sorted(c.get("modbus_maps", []))
            detail = (f"[{chan}] valid Modbus RTU frames present "
                      f"({c.get('modbus_summary', 'no request decode')}) - a "
                      f"device here speaks a Modbus register protocol, not "
                      f"Pylontech ASCII.")
            if "voltronic-lib" in maps:
                detail += (" Registers match the Voltronic 'LIB' map: if this "
                           "is the inverter set to 'PYL', that setting is "
                           "effectively LIB - switch the battery to its "
                           "Voltronic/LIB profile or change the inverter "
                           "protocol setting.")
            elif "seplos-v3-native" in maps:
                detail += (" Registers match the Seplos-native map: wrong "
                           "socket (RS485-1/2 monitor port) or the PYLON "
                           "setting is not active on the battery.")
            else:
                detail += (" If this is the battery in 'PYLON' mode, its "
                           "setting is not taking effect (or the PC/monitor "
                           "port is connected).")
            find("H9-modbus-detected", "warn", detail)

    done = [e for e in exchanges if e["resp"] is not None]
    unanswered = [e for e in exchanges if e["resp"] is None]

    # H3: address conventions
    for chan, c in chans.items():
        if c["req_adrs"] and c["resp_adrs"]:
            extra = c["resp_adrs"] - c["req_adrs"]
            if extra:
                find("H3-address", "info",
                     f"[{chan}] responses use ADR {sorted(f'{a:02X}' for a in extra if a is not None)} "
                     f"while requests were for ADR {sorted(f'{a:02X}' for a in c['req_adrs'] if a is not None)} "
                     f"- normal for the master-pack-answers-on-02 convention, "
                     f"but some inverters reject it.")
    # unanswered polls per cid2/adr
    if unanswered:
        by_key: dict[str, int] = {}
        for e in unanswered:
            cid2 = e["req"].get("cid2")
            adr = e["req"].get("adr")
            key = f"CID2 {cid2:02X} @ADR {adr:02X}" if cid2 is not None else "?"
            by_key[key] = by_key.get(key, 0) + 1
        find("H3/H4-unanswered", "fail" if not done else "warn",
             f"{len(unanswered)} unanswered poll(s): {by_key}. "
             f"If specific to one ADR -> address/DIP problem (try pack "
             f"address 1, power-cycle after DIP change). If specific to one "
             f"CID2 family -> command-family mismatch (H4).")

    # H4: command family gaps + RTN=04
    fam_stats: dict[str, dict] = {}
    for e in done:
        cid2 = e["req"].get("cid2")
        if cid2 is None:
            continue
        fam = F.cid2_family(cid2)
        st = fam_stats.setdefault(fam, {"ok": 0, "rtn04": 0, "other_rtn": 0})
        rtn = e["resp"].get("cid2")
        if rtn == 0x00 or rtn == cid2:
            st["ok"] += 1
        elif rtn == 0x04:
            st["rtn04"] += 1
        else:
            st["other_rtn"] += 1
    for fam, st in fam_stats.items():
        if st["rtn04"] and not st["ok"]:
            find("H4-command-family", "fail",
                 f"family '{fam}' is polled but every request returns RTN=04 "
                 f"(unsupported) - the battery does not implement the command "
                 f"family this inverter needs. Consider translate mode or a "
                 f"different protocol profile on the BMS.")
        if st["other_rtn"]:
            find("H4-rtn-errors", "warn",
                 f"family '{fam}': {st['other_rtn']} response(s) with an "
                 f"error RTN (not 00/04) - see per-frame records.")

    # H5: VER mismatches
    all_vers = set().union(*(c["vers"] for c in chans.values())) if chans else set()
    if len(all_vers) > 1:
        find("H5-version-byte", "warn",
             f"multiple protocol version bytes on the wire: "
             f"{sorted(f'{v:02X}' for v in all_vers)} - if the inverter sends "
             f"VER 25 and the battery answers VER 20 (or vice versa), response "
             f"lengths differ and the inverter may reject frames.")

    # H6: checksum / structural errors
    for chan, c in chans.items():
        if c["frame_errors"]:
            find("H6-frame-errors", "fail" if c["frame_errors"] > 2 else "warn",
                 f"[{chan}] {c['frame_errors']}/{c['frames']} frames with "
                 f"structural errors (checksum/length) - see 'errors' fields "
                 f"in the session for exact got/expected values.")

    # H7: cid2-echo responses
    echoing = [e for e in done
               if e["resp"].get("cid2") == e["req"].get("cid2")
               and len(e["resp"].get("info", "")) > 2]
    if echoing:
        find("H7-cid2-echo", "warn",
             f"{len(echoing)} response(s) echo the request CID2 in the RTN "
             f"position instead of 00 - non-spec behaviour (the "
             f"Battery-Emulator bug); strict inverters treat every reply as "
             f"an error. Translate mode can rewrite this (fix_cid2_echo).")

    # H8: timing
    lats = sorted(e["latency_ms"] for e in done if e["latency_ms"] is not None)
    if lats:
        med = lats[len(lats) // 2]
        stats = {"n": len(lats), "min": lats[0], "median": med, "max": lats[-1]}
        find("H8-timing", "warn" if lats[-1] > 400 else "info",
             f"request->response latency ms: {stats}"
             + (" - responses slower than ~400 ms can exceed inverter "
                "timeouts." if lats[-1] > 400 else ""))

    # H10: content snapshot for manual/LLM diff
    for e in reversed(done):
        cid2 = e["req"].get("cid2")
        if cid2 in (0x61, 0x63) and e["resp"].get("info"):
            payload = P.decode_response_info(cid2, e["resp"]["info"])
            if payload and "decode_error" not in payload:
                find(f"H10-content-{cid2:02X}", "info",
                     f"last decoded 0x{cid2:02X} payload: {payload}")
            break

    summary = {chan: {**c, "modbus_maps": sorted(c.get("modbus_maps", [])),
                      "vers": sorted(c["vers"]),
                      "req_adrs": sorted(a for a in c["req_adrs"] if a is not None),
                      "resp_adrs": sorted(a for a in c["resp_adrs"] if a is not None)}
               for chan, c in chans.items()}
    return {"session": str(path), "channels": summary,
            "exchanges": {"total": len(exchanges), "answered": len(done),
                          "unanswered": len(unanswered)},
            "findings": findings}


def format_report(report: dict) -> str:
    lines = [f"Diagnosis of {report['session']}"]
    ex = report["exchanges"]
    lines.append(f"  exchanges: {ex['answered']}/{ex['total']} answered")
    for chan, c in report["channels"].items():
        lines.append(f"  [{chan}] frames={c['frames']} errors={c['frame_errors']} "
                     f"raw_bytes={c['raw_bytes']} vers={['%02X' % v for v in c['vers']]} "
                     f"req_cid2={c['req_cid2']}")
    if not report["findings"]:
        lines.append("  no findings - communication looks healthy.")
    for f in report["findings"]:
        lines.append(f"  [{f['severity'].upper():4}] {f['hypothesis']}: {f['detail']}")
    return "\n".join(lines)
