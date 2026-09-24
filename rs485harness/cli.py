"""rsh - RS485 harness CLI.

Every subcommand supports --json for machine-readable results; all wire
traffic goes to a JSONL session file in captures/ (path printed on exit).

Typical first commands:
  rsh ports                       list COM ports
  rsh selftest COM5 COM6          verify the adapter (ch1/ch2 cross-wired)
  rsh identify --port COM5       'who is there?' on a channel
  rsh demo                        no-hardware loopback demo (sim vs sim)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .core import session as S
from .core.transport import SerialLink, loopback_pair
from .protocols.pylontech import frames as F
from .protocols.pylontech import payloads as P
from .protocols.pylontech.dialects import (BATTERY_PROFILES, INVERTER_PROFILES,
                                           DialectProfile, InverterPollProfile)
from .simulators.battery_state import preset, PRESETS, apply_overrides, parse_overrides
from .simulators.battery_sim import BatterySimulator
from .simulators.inverter_sim import InverterSimulator


def _arm_deadline(seconds, what: str) -> None:
    """Hard process deadline: a one-shot tool must never run open-ended.
    Prints a diagnostic and exits(3) if the work outlives the budget (e.g. a
    serial call wedged in a driver)."""
    if not seconds:
        return
    import os, threading

    def _fire():
        print(f"\n[deadline] {what} exceeded --max-seconds={seconds:g}; "
              f"aborting. If this recurs the adapter may be wedged - "
              f"unplug/replug it.", flush=True)
        os._exit(3)
    timer = threading.Timer(seconds, _fire)
    timer.daemon = True   # never keep the process alive after the work is done
    timer.start()


def _print(args, human: str, data: dict) -> None:
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(human)


def _echo(line: str) -> None:
    print(line, flush=True)


def _quiet(line: str) -> None:
    pass


def cmd_ports(args) -> int:
    from serial.tools import list_ports
    ports = [{"device": p.device, "description": p.description,
              "hwid": p.hwid} for p in list_ports.comports()]
    human = "\n".join(f"{p['device']:8} {p['description']} [{p['hwid']}]"
                      for p in ports) or "no serial ports found"
    _print(args, human, {"ports": ports})
    return 0


def cmd_decode(args) -> int:
    raw_in = "".join(args.hex).replace(" ", "").replace("-", "")
    if raw_in.startswith("~"):
        data = raw_in.encode("ascii")
        if not data.endswith(b"\r"):
            data += b"\r"
    else:
        data = bytes.fromhex(raw_in)
    pf = F.parse_frame(data)
    role = args.role
    lines = [pf.describe(role)]
    payload = None
    looks_like_response = len(pf.info) > 4 or args.request_cid2
    if pf.info and role in ("response", "auto") and looks_like_response:
        req_cid2 = int(args.request_cid2, 16) if args.request_cid2 else None
        if req_cid2 is None and pf.cid2 in P.RESPONSE_DECODERS:
            req_cid2 = pf.cid2  # cid2-echo style response
        if req_cid2:
            payload = P.decode_response_info(req_cid2, pf.info)
            lines.append(f"payload (as answer to {req_cid2:02X}): {payload}")
    _print(args, "\n".join(lines),
           {"frame": pf.to_dict(), "ok": pf.ok, "payload": payload})
    return 0 if pf.ok else 1


def _open_serial(port: str, baud: int, name: str) -> SerialLink:
    link = SerialLink(port, baud, name=name)
    return link


def cmd_battery_sim(args) -> int:
    state = preset(args.preset)
    apply_overrides(state, parse_overrides(args.set))
    state.drift = args.drift
    profile = BATTERY_PROFILES[args.profile]
    if args.adr:
        profile = DialectProfile(**{**profile.to_dict(),
                                    "response_adr": args.adr})
    sess = S.SessionWriter(args.session or S.new_session_path("battery-sim"))
    link = _open_serial(args.port, args.baud or profile.baud, "battery")
    if args.detect_echo:
        link.detect_echo()
    sim = BatterySimulator(link, state, profile, sess,
                           on_event=_quiet if args.json else _echo)
    print(f"battery-sim '{args.profile}' preset '{args.preset}' on "
          f"{args.port} @ {link.baud} baud; session: {sess.path}", flush=True)
    try:
        stats = sim.run(duration=args.duration)
    except KeyboardInterrupt:
        sim.stop_event.set()
        stats = sim.stats
    _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
    sess.close()
    link.close()
    return 0


def cmd_inverter_sim(args) -> int:
    profile = INVERTER_PROFILES[args.profile]
    over: dict = {}
    if args.adr is not None:
        over["adr"] = args.adr
    if args.ver is not None:
        over["ver"] = int(args.ver, 16)
    if over:
        profile = InverterPollProfile(**{**profile.to_dict(), **over})
    sess = S.SessionWriter(args.session or S.new_session_path("inverter-sim"))
    link = _open_serial(args.port, args.baud or profile.baud, "inverter")
    if args.detect_echo:
        link.detect_echo()
    sim = InverterSimulator(link, profile, sess,
                            on_event=_quiet if args.json else _echo)
    print(f"inverter-sim '{args.profile}' on {args.port} @ {link.baud} baud "
          f"ADR {profile.adr:02X}; session: {sess.path}", flush=True)
    try:
        stats = sim.run(duration=args.duration)
    except KeyboardInterrupt:
        sim.stop_event.set()
        stats = sim._stats_summary()
    _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
    sess.close()
    link.close()
    return 0


def cmd_identify(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'identify')
    from .analyzer.identify import identify
    sess = S.SessionWriter(args.session or S.new_session_path("identify"))
    link = _open_serial(args.port, 9600, "id")
    bauds = tuple(int(b) for b in args.bauds.split(","))
    adrs = tuple(int(a, 0) for a in args.adrs.split(","))
    report = identify(link, sess, listen_s=args.listen,
                      active=not args.passive_only, bauds=bauds, adrs=adrs,
                      on_event=_quiet if args.json else _echo)
    report["session"] = str(sess.path)
    report_path = S.save_report(sess.path, report, "identify")
    report["report_file"] = str(report_path)
    human = "\n".join(["IDENTIFY RESULT:"] +
                      [f"  * {v}" for v in report["verdicts"]] +
                      [f"session: {sess.path}", f"report:  {report_path}"])
    _print(args, human, report)
    sess.close()
    link.close()
    return 0


def cmd_send(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'send')
    """Send one frame (fields or raw hex), wait for and decode the reply."""
    from .scripting import build_frame
    from .core.transport import FrameAccumulator
    import time as _t
    sess = S.SessionWriter(args.session or S.new_session_path("send"))
    link = _open_serial(args.port, args.baud, "send")
    if args.hex:
        spec = {"hex": args.hex}
    else:
        spec = {"ver": args.ver or "20", "adr": args.adr or "02",
                "cid1": args.cid1 or "46", "cid2": args.cid2 or "00",
                "info": args.info or ""}
    frame = build_frame(spec, allow_shutdown=args.allow_shutdown)
    link.flush_input()
    t0 = _t.perf_counter()
    link.write(frame)
    sess.log("send", "tx", frame, "frame", decoded=F.parse_frame(frame).to_dict())
    acc = FrameAccumulator()
    reply = None
    deadline = t0 + args.timeout
    while _t.perf_counter() < deadline and reply is None:
        chunk = link.read(timeout=min(0.1, max(0.01, deadline - _t.perf_counter())))
        if not chunk:
            continue
        for ev in acc.feed(chunk):
            kind = ev.kind
            if kind == "frame":
                reply = F.parse_frame(ev.data)
                lat = round((ev.ts - t0) * 1000, 1)
                sess.log("send", "rx", ev.data, "frame", decoded=reply.to_dict(),
                         errors=reply.errors, note=f"latency_ms={lat}")
            else:
                sess.log("send", "rx", ev.data, "raw")
    payload = None
    if reply and reply.info:
        cid2 = int(args.expect_cid2, 16) if args.expect_cid2 else \
            (int(args.cid2, 16) if args.cid2 else None)
        if cid2 is not None:
            payload = P.decode_response_info(cid2, reply.info)
    out = {"sent": frame.hex().upper(),
           "reply": reply.to_dict() if reply else None,
           "payload": payload, "session": str(sess.path)}
    human = (f"TX {frame.hex().upper()}\n"
             + (f"RX {reply.describe('response')}" if reply else "RX (timeout)")
             + (f"\npayload: {payload}" if payload else ""))
    _print(args, human, out)
    sess.close()
    link.close()
    return 0 if reply else 2


def cmd_script(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'script')
    from .scripting import load_scenario, run_scenario, ScenarioError
    try:
        scenario = load_scenario(args.scenario)
    except ScenarioError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    sess = S.SessionWriter(args.session or S.new_session_path("script"))
    link = _open_serial(args.port, args.baud or scenario.get("baud", 9600),
                        "script")
    mode = scenario.get("mode")
    print(f"scenario '{scenario.get('name','?')}' mode={mode} on {args.port} "
          f"@ {link.baud} baud; session: {sess.path}", flush=True)
    try:
        stats = run_scenario(link, scenario, sess, duration=args.duration,
                             on_event=_quiet if args.json else _echo)
    except (ScenarioError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sess.close(); link.close()
        return 1
    stats["session"] = str(sess.path)
    report_path = S.save_report(sess.path, stats, "script")
    _print(args, f"stats: {stats}\nsession: {sess.path}\nreport: {report_path}",
           stats)
    sess.close()
    link.close()
    return 0


def cmd_soc(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'soc')
    """Quick per-pack SOC/V/I table via one 0x42 'FF' (all packs) poll."""
    from .analyzer.identify import _probe_pylontech
    sess = S.SessionWriter(args.session or S.new_session_path("soc"))
    link = _open_serial(args.port, args.baud, "soc")
    ver = int(args.ver, 16) if args.ver else 0x20
    adrs = [int(args.adr, 0)] if args.adr is not None else [2, 0, 1, 0x12]
    data = None
    used = None
    for adr in adrs:
        pf, lat = _probe_pylontech(link, ver, adr, 0x42, "FF",
                                   session=sess, chan="soc", timeout=args.timeout)
        if pf is not None and pf.ok and pf.info:
            d = P.decode_42_multi(pf.info)
            if d.get("modules"):
                data, used = d, adr
                break
    if data is None:
        _print(args, "no multi-pack response (try --baud 115200, or run "
                     "`rsh identify` to find the address/baud)",
               {"error": "no response", "session": str(sess.path)})
        sess.close(); link.close(); return 2
    rows = []
    for i, m in enumerate(data["modules"], 1):
        rows.append({"pack": i, "soc_percent": m.get("soc_percent"),
                     "voltage_v": m.get("voltage_v"), "current_a": m.get("current_a"),
                     "remaining_ah": m.get("remaining_ah"),
                     "total_ah": m.get("total_ah"), "cycles": m.get("cycles")})
    human = [f"SOC via ADR {used:02X}  ({data['n_modules']} packs, stack "
             f"{data['stack_soc_percent']}%)",
             f"{'pack':>4} {'SOC%':>5} {'V':>7} {'A':>7} {'remAh':>7} {'totAh':>7} {'cyc':>5}"]
    for r in rows:
        human.append(f"{r['pack']:>4} {r['soc_percent']!s:>5} "
                     f"{r['voltage_v']!s:>7} {r['current_a']!s:>7} "
                     f"{r['remaining_ah']!s:>7} {r['total_ah']!s:>7} "
                     f"{r['cycles']!s:>5}")
    out = {"address": used, "n_packs": data["n_modules"],
           "stack_soc_percent": data["stack_soc_percent"], "packs": rows,
           "session": str(sess.path)}
    _print(args, "\n".join(human), out)
    sess.close(); link.close()
    return 0


def cmd_dump(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'dump')
    from .analyzer.dump import dump_stack, format_dump
    sess = S.SessionWriter(args.session or S.new_session_path("dump"))
    link = _open_serial(args.port, args.baud, "dump")
    ver = int(args.ver, 16) if args.ver else 0x20
    adrs = [int(a, 0) for a in args.adrs.split(",")] if args.adrs else None
    report = dump_stack(link, sess, adrs=adrs, ver=ver,
                        on_event=_quiet if args.json else _echo)
    report["session"] = str(sess.path)
    report_path = S.save_report(sess.path, report, "dump")
    report["report_file"] = str(report_path)
    _print(args, format_dump(report) + f"\nsession: {sess.path}\n"
           f"full JSON: {report_path}", report)
    sess.close(); link.close()
    return 0


def cmd_discover(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'discover')
    from .analyzer.discover import discover, format_discovery
    sess = S.SessionWriter(args.session or S.new_session_path("discover"))
    link = _open_serial(args.port, args.baud, "disc")
    report = discover(link, sess,
                      adr=args.adr, ver=int(args.ver, 16) if args.ver else None,
                      on_event=_quiet if args.json else _echo)
    report["session"] = str(sess.path)
    report_path = S.save_report(sess.path, report, "discovery")
    report["report_file"] = str(report_path)
    _print(args, format_discovery(report) +
           f"\nsession: {sess.path}\nreport:  {report_path}", report)
    sess.close()
    link.close()
    return 0


def cmd_sniff(args) -> int:
    from .analyzer.sniff import Sniffer
    sess = S.SessionWriter(args.session or S.new_session_path("sniff"))
    links = {}
    links["ch1"] = _open_serial(args.ch1, args.baud, "ch1")
    if args.ch2:
        links["ch2"] = _open_serial(args.ch2, args.baud, "ch2")
    sn = Sniffer(links, sess, on_event=_quiet if args.json else _echo)
    print(f"sniffing {', '.join(l.name for l in links.values())} @ "
          f"{args.baud} baud; Ctrl+C to stop; session: {sess.path}", flush=True)
    stats = sn.run(duration=args.duration)
    _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
    sess.close()
    for link in links.values():
        link.close()
    return 0


def cmd_bridge(args) -> int:
    from .analyzer.bridge import Bridge
    from .analyzer.translate import Translator
    sess = S.SessionWriter(args.session or S.new_session_path("bridge"))
    a = _open_serial(args.ch1, args.baud, "ch1")
    b = _open_serial(args.ch2, args.baud, "ch2")
    if args.detect_echo:
        a.detect_echo()
        b.detect_echo()
    tr = None
    if args.fix_cid2_echo or args.force_ver or args.force_adr:
        tr = Translator()
        if args.fix_cid2_echo:
            tr.fix_cid2_echo = {"ch1->ch2", "ch2->ch1"}
        for spec in args.force_ver or []:
            d, v = spec.split("=")
            tr.force_ver[{"a2b": "ch1->ch2", "b2a": "ch2->ch1"}[d]] = int(v, 16)
        for spec in args.force_adr or []:
            d, v = spec.split("=")
            tr.force_adr[{"a2b": "ch1->ch2", "b2a": "ch2->ch1"}[d]] = int(v, 16)
    br = Bridge(a, b, sess, translator=tr,
                on_event=_quiet if args.json else _echo)
    print(f"bridging {args.ch1} <-> {args.ch2} @ {args.baud} baud "
          f"(translate={'on' if tr else 'off'}); Ctrl+C to stop; "
          f"session: {sess.path}", flush=True)
    stats = br.run(duration=args.duration)
    _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
    sess.close()
    a.close()
    b.close()
    return 0


def cmd_selftest(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'selftest')
    from .analyzer.selftest import run_selftest
    print("Self-test requires ch1 and ch2 joined on one RS485 bus: "
          "A-A, B-B (straight, not crossed) plus GND.", flush=True)
    sess = S.SessionWriter(args.session or S.new_session_path("selftest"))
    report = run_selftest(args.port1, args.port2, session=sess,
                          on_event=_quiet if args.json else _echo)
    report["session"] = str(sess.path)
    report_path = S.save_report(sess.path, report, "selftest")
    report["report_file"] = str(report_path)
    sess.close()
    _print(args, f"RESULT: {'PASSED' if report['passed'] else 'FAILED'}\n"
                 f"session: {sess.path}\nreport:  {report_path}", report)
    return 0 if report["passed"] else 1


def cmd_diagnose(args) -> int:
    from .analyzer.diagnose import analyze, format_report
    path = args.session
    if not path:
        caps = sorted(S.CAPTURE_DIR.glob("*.jsonl"))
        if not caps:
            print("no captures found", file=sys.stderr)
            return 1
        path = caps[-1]
    report = analyze(path)
    report_path = S.save_report(path, report, "diagnosis")
    report["report_file"] = str(report_path)
    _print(args, format_report(report) + f"\n  report saved: {report_path}",
           report)
    return 0


def cmd_demo(args) -> int:
    """No-hardware loopback demo: battery-sim vs inverter-sim (or identify)."""
    from .analyzer.identify import identify
    sess = S.SessionWriter(args.session or S.new_session_path("demo"))
    end_a, end_b = loopback_pair("inverter", "battery")
    state = preset(args.preset)
    apply_overrides(state, parse_overrides(args.set))
    bat = BatterySimulator(end_b, state, BATTERY_PROFILES[args.battery_profile],
                           sess, chan="bat", on_event=_quiet if args.json else _echo)
    bat.start()
    if args.identify:
        report = identify(end_a, sess, chan="id", listen_s=1.0,
                          on_event=_quiet if args.json else _echo)
        human = "\n".join(["IDENTIFY vs simulated battery:"] +
                          [f"  * {v}" for v in report["verdicts"]])
    else:
        inv = InverterSimulator(end_a, INVERTER_PROFILES[args.inverter_profile],
                                sess, on_event=_quiet if args.json else _echo)
        report = {"inverter_stats": None}
        try:
            report["inverter_stats"] = inv.run(duration=args.duration or 5)
        except KeyboardInterrupt:
            pass
        human = f"inverter stats: {report['inverter_stats']}"
    bat.stop()
    report["battery_stats"] = bat.stats
    report["session"] = str(sess.path)
    report_path = S.save_report(sess.path, report, "demo")
    report["report_file"] = str(report_path)
    sess.close()
    _print(args, human + f"\nbattery stats: {bat.stats}\nsession: {sess.path}"
                         f"\nreport:  {report_path}", report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rsh", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, session=True):
        p.add_argument("--json", action="store_true",
                       help="machine-readable JSON output")
        p.add_argument("--max-seconds", type=float, default=240,
                       help="hard time budget for one-shot tools (default 240; "
                            "0 = none)")
        if session:
            p.add_argument("--session", help="session JSONL path (default: captures/<ts>-<cmd>.jsonl)")

    p = sub.add_parser("ports", help="list serial ports")
    common(p, session=False)
    p.set_defaults(fn=cmd_ports)

    p = sub.add_parser("decode", help="decode a pasted frame (hex or ~ASCII)")
    p.add_argument("hex", nargs="+", help="frame as hex bytes or literal ~...\\r ASCII")
    p.add_argument("--role", choices=["request", "response", "auto"],
                   default="auto")
    p.add_argument("--request-cid2", help="CID2 (hex) of the request this "
                                          "response answers, e.g. 61")
    common(p, session=False)
    p.set_defaults(fn=cmd_decode)

    p = sub.add_parser("battery-sim", help="simulate a Pylontech battery (slave)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int)
    p.add_argument("--preset", default="ideal", choices=PRESETS)
    p.add_argument("--profile", default="ideal-pylontech",
                   choices=sorted(BATTERY_PROFILES))
    p.add_argument("--adr", choices=["fixed02", "fixed01", "echo"],
                   help="override response address policy")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="override battery state fields, e.g. --set soc=20 "
                        "--set current_a=-15 --set charge_enable=false "
                        "--set voltage_v=51.2")
    p.add_argument("--drift", action="store_true", help="time-varying values")
    p.add_argument("--duration", type=float, help="seconds (default: forever)")
    p.add_argument("--detect-echo", action="store_true",
                   help="probe for TX echo before starting (bus must be quiet)")
    common(p)
    p.set_defaults(fn=cmd_battery_sim)

    p = sub.add_parser("inverter-sim", help="poll a battery like an inverter (master)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int)
    p.add_argument("--profile", default="voltronic-6x",
                   choices=sorted(INVERTER_PROFILES))
    p.add_argument("--adr", type=lambda s: int(s, 0), help="poll address")
    p.add_argument("--ver", help="protocol version byte, hex (20/25)")
    p.add_argument("--duration", type=float)
    p.add_argument("--detect-echo", action="store_true")
    common(p)
    p.set_defaults(fn=cmd_inverter_sim)

    p = sub.add_parser("identify", help="'who is there?' scan on one channel")
    p.add_argument("--port", required=True)
    p.add_argument("--listen", type=float, default=5.0,
                   help="passive listen seconds per baud (default 5)")
    p.add_argument("--passive-only", action="store_true",
                   help="do not transmit anything")
    p.add_argument("--bauds", default="9600,19200,115200")
    p.add_argument("--adrs", default="0,1,2")
    common(p)
    p.set_defaults(fn=cmd_identify)

    p = sub.add_parser("send", help="send one frame (fields or --hex), decode reply")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--hex", help="raw frame bytes hex or ~ASCII (verbatim, "
                                 "may be malformed)")
    p.add_argument("--ver", help="hex, default 20")
    p.add_argument("--adr", help="hex, default 02")
    p.add_argument("--cid1", help="hex, default 46")
    p.add_argument("--cid2", help="hex, default 00")
    p.add_argument("--info", help="INFO ascii-hex payload, default empty")
    p.add_argument("--expect-cid2", help="decode reply payload as answer to "
                                         "this request CID2 (hex)")
    p.add_argument("--timeout", type=float, default=1.0)
    p.add_argument("--allow-shutdown", action="store_true",
                   help="permit building a 0x64 shutdown frame")
    common(p)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("script", help="run a JSON scenario: master send/branch "
                                      "sequence OR reactive responder rules")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, help="override scenario baud")
    p.add_argument("scenario", help="scenario .json file (or inline JSON)")
    p.add_argument("--duration", type=float,
                   help="responder mode: run seconds then stop")
    common(p)
    p.set_defaults(fn=cmd_script)

    p = sub.add_parser("soc", help="quick per-pack SOC/V/I table (0x42 FF, "
                                   "all packs in one poll)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200,
                   help="Pylontech monitoring baud (default 115200; use 9600 "
                        "for the inverter-link protocol)")
    p.add_argument("--adr", help="poll address (default: try 2,0,1,12)")
    p.add_argument("--ver", help="protocol version byte hex (default 20)")
    p.add_argument("--timeout", type=float, default=1.0)
    common(p)
    p.set_defaults(fn=cmd_soc)

    p = sub.add_parser("dump", help="complete per-unit battery state (cells, "
                                    "temps, limits, firmware, serial) to JSON")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--adrs", help="comma-separated addresses (default: scan 0..7)")
    p.add_argument("--ver", help="protocol version byte hex (default 20)")
    common(p)
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("discover", help="full capability + state inventory "
                                        "of a responder (run identify first)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--adr", type=lambda s: int(s, 0),
                   help="skip the scan, discover at this address")
    p.add_argument("--ver", help="protocol version byte, hex (default 20)")
    common(p)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("sniff", help="passive capture on one or two channels")
    p.add_argument("--ch1", required=True)
    p.add_argument("--ch2")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--duration", type=float)
    common(p)
    p.set_defaults(fn=cmd_sniff)

    p = sub.add_parser("bridge", help="forward ch1<->ch2, optionally translating")
    p.add_argument("--ch1", required=True)
    p.add_argument("--ch2", required=True)
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--duration", type=float)
    p.add_argument("--detect-echo", action="store_true",
                   help="probe both adapters for TX echo first (quiet bus!)")
    p.add_argument("--fix-cid2-echo", action="store_true",
                   help="rewrite responses that echo CID2 to proper RTN=00")
    p.add_argument("--force-ver", action="append", metavar="DIR=HEX",
                   help="e.g. b2a=20 (a=ch1, b=ch2)")
    p.add_argument("--force-adr", action="append", metavar="DIR=HEX",
                   help="e.g. b2a=02")
    common(p)
    p.set_defaults(fn=cmd_bridge)

    p = sub.add_parser("selftest", help="adapter self-test (ch1+ch2 on one bus)")
    p.add_argument("port1")
    p.add_argument("port2")
    common(p)
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("diagnose", help="run hypothesis checks over a session")
    p.add_argument("session", nargs="?", help="session file (default: newest capture)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_diagnose)

    p = sub.add_parser("demo", help="no-hardware loopback demo (sims in-process)")
    p.add_argument("--preset", default="ideal", choices=PRESETS)
    p.add_argument("--battery-profile", default="ideal-pylontech",
                   choices=sorted(BATTERY_PROFILES))
    p.add_argument("--inverter-profile", default="voltronic-6x",
                   choices=sorted(INVERTER_PROFILES))
    p.add_argument("--identify", action="store_true",
                   help="run the identify scan against the simulated battery")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="override simulated battery state (see battery-sim)")
    p.add_argument("--duration", type=float)
    common(p)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("menu", help="interactive human test runner")
    p.set_defaults(fn=None)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "menu":
        from .runner import run_menu
        return run_menu()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
