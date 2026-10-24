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
import os
import json
import sys
import time

from .core import session as S
from .core.transport import SerialLink, loopback_pair, open_link
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
    ports = []
    if sys.platform != "android":   # pyserial cannot enumerate ttys there
        from serial.tools import list_ports
        ports = [{"device": p.device, "description": p.description,
                  "hwid": p.hwid} for p in list_ports.comports()]
    fd = os.environ.get("RSH_USB_FD")
    can_ports: list[dict] = []
    if fd:   # Android/Termux: the one device termux-usb handed us
        from .core.gs_usb import describe_fd_device
        ident = describe_fd_device(int(fd))
        if ident["kind"] == "ftdi":
            from .core.ftdi_usb import describe_device
            d = describe_device(int(fd))
            for ch in d["channels"]:
                ports.append({"device": f"usb:{ch}", "hwid": d["hwid"],
                              "description": f"{d['product']} channel {ch} "
                                             f"(fd {fd}, via termux-usb)"})
        elif ident["kind"] == "gs_usb":
            from .core.canbus import CanSpec
            from .core.gs_usb import GsUsbDevice
            dev = GsUsbDevice.get(CanSpec("gs", f"fd{fd}", 0))
            info = dev.describe()
            dev.close()
            for ch in range(info["channels"]):
                can_ports.append({"device": f"can:gs:.{ch}", "hwid": info["hwid"],
                                  "kind": "can",
                                  "description": f"{info['product']} channel {ch} "
                                                 f"(fd {fd}, via termux-usb)"})
        else:
            ports.append({"device": f"(fd {fd})", "hwid": ident["hwid"],
                          "description": f"unsupported USB device ({ident['kind']})"})
    else:
        from .core.gs_usb import list_can_adapters
        can_ports = list_can_adapters()
    lines = [f"{p['device']:8} {p['description']} [{p['hwid']}]" for p in ports]
    if not lines:
        lines = ["no serial ports found"]
    real_can = [p for p in can_ports if p.get("kind") != "note"]
    if real_can or args.json:
        lines.append("CAN:")
        lines += [f"  {p['device']:14} {p['description']} [{p['hwid']}]" for p in real_can]
    for p in can_ports:
        if p.get("kind") == "note":
            lines.append(f"  note: {p['description']}")
    _print(args, "\n".join(lines), {"ports": ports, "can": can_ports})
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


def _open_serial(port: str, baud: int, name: str):
    return open_link(port, baud, name=name)


def is_can_link_spec(port: str) -> bool:
    return bool(port) and port.lower().startswith("can:")


def _labelled(prefix: str, label: str | None) -> str:
    """captures/<ts>-<prefix>[-<label>].jsonl - the Phase B naming
    (B3.1-<serial>) without renaming files afterwards."""
    if not label:
        return prefix
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in label.strip())
    return f"{prefix}-{safe}" if safe else prefix


def _save_huawei(cfg: dict, hcfg: dict) -> None:
    from .runner import save_config
    cfg["huawei"] = {k: v for k, v in hcfg.items() if not k.startswith("_")}
    save_config(cfg)


def cmd_huawei(args) -> int:
    """Huawei R48xx rectifier control over CAN (docs/CAN-PLAN.md 3.6)."""
    _arm_deadline(getattr(args, 'max_seconds', None), 'huawei')
    from .analyzer.huawei import HuaweiClient, huawei_config
    from .runner import load_config, save_config
    from .protocols.huawei_r48xx import regs as HR
    from .protocols.huawei_r48xx.ids import BITRATE
    from .analyzer.huawei import bind_unit, unit_on_addr, PERSIST_REMINDER
    cfg = load_config()
    hcfg = huawei_config(cfg)
    if hcfg.get("_migrated"):
        print(f"config note: {hcfg['_migrated']}", file=sys.stderr, flush=True)
        _save_huawei(cfg, hcfg)
    on_event = _quiet if args.json else _echo
    sub = args.sub
    label = getattr(args, "label", None)

    if sub == "caps":          # no bus access: edit the hard caps / per-serial scale
        changed = {}
        if args.voltage_max is not None:
            hcfg["caps"]["voltage_max"] = changed["voltage_max"] = args.voltage_max
        if args.current_max is not None:
            hcfg["caps"]["current_max"] = changed["current_max"] = args.current_max
        if args.current_scale is not None or args.nominal_a is not None:
            if args.current_scale is not None and args.current_scale not in HR.CURRENT_SCALES \
                    and args.current_scale != "none":
                print(f"unknown scale {args.current_scale}; one of "
                      f"{sorted(HR.CURRENT_SCALES)} or none", file=sys.stderr)
                return 2
            serial = args.serial
            if not serial:
                serial, _ = unit_on_addr(hcfg, args.addr if args.addr is not None
                                         else hcfg.get("addr", 1))
            if not serial:
                print("a current scale / nominal current belongs to a SERIAL: give --serial "
                      "<12 hex> (from `info`), or run `info` first so the address is bound",
                      file=sys.stderr)
                return 2
            scale = None if args.current_scale == "none" else args.current_scale
            unit = hcfg["units"].setdefault(serial.upper(), {})
            if args.current_scale is not None:
                unit["current_scale"] = changed["current_scale"] = scale
            if args.nominal_a is not None:
                unit["nominal_a"] = changed["nominal_a"] = args.nominal_a
            unit.setdefault("addr", args.addr if args.addr is not None else hcfg.get("addr", 1))
            changed["serial"] = serial.upper()
        if changed:
            _save_huawei(cfg, hcfg)
        human = (f"huawei config: caps {hcfg['caps']}, units {hcfg['units']}, addr_map "
                 f"{hcfg['addr_map']}" + (f", legacy {hcfg['legacy']}" if hcfg.get("legacy") else "")
                 + (f" (changed: {changed})" if changed else ""))
        _print(args, human, {"huawei": {k: v for k, v in hcfg.items() if not k.startswith("_")},
                             "changed": changed})
        return 0

    if sub == "sim":
        from .simulators.huawei_psu_sim import HuaweiPsuSimulator
        sess = S.SessionWriter(args.session or S.new_session_path(_labelled("huawei-sim", label)))
        link = open_link(args.port, args.bitrate, name="psu-sim")
        sim_addr = args.addr if args.addr is not None else 1
        sim = HuaweiPsuSimulator(link, sess, addr=sim_addr, nominal_a=args.nominal_a,
                                 scale=args.scale, load_ohm=args.load_ohm, load_w=args.load_w,
                                 battery_v=args.battery_v, timeout_s=args.online_timeout,
                                 default_current_a=args.default_current_a,
                                 serial_hex=args.serial,
                                 polls_refresh_timeout=args.polls_refresh_timeout == "yes",
                                 reject_zero_default_current=args.reject_zero_default_current,
                                 on_ac_loss=args.on_ac_loss, negotiate_on_start=args.negotiate,
                                 on_event=on_event)
        print(f"huawei-psu-sim addr {sim_addr} serial {args.serial} ({args.nominal_a} A nominal, "
              f"scale {args.scale}, polls_refresh_timeout={args.polls_refresh_timeout}, "
              f"on_ac_loss={args.on_ac_loss}) on {args.port} @ {args.bitrate}; "
              f"session: {sess.path}", flush=True)
        import time as _t
        sim.start()
        t0 = _t.monotonic()
        pending = {"ac_off": args.ac_off_after, "ac_on": args.ac_on_after}
        try:
            while sim._thread.is_alive():
                el = _t.monotonic() - t0
                if args.duration and el >= args.duration:
                    break
                if pending["ac_off"] is not None and el >= pending["ac_off"]:
                    sim.set_ac(False); pending["ac_off"] = None
                if pending["ac_on"] is not None and el >= pending["ac_on"]:
                    sim.set_ac(True); pending["ac_on"] = None
                _t.sleep(0.1)
        except KeyboardInterrupt:
            pass
        sim.stop()
        stats = sim.stats
        _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
        sess.close(); link.close()
        return 0

    if sub == "frontend-sim":
        from .simulators.huawei_frontend_sim import HuaweiFrontEndSimulator
        sess = S.SessionWriter(args.session or S.new_session_path(_labelled("huawei-frontend-sim", label)))
        link = open_link(args.port, args.bitrate, name="frontend-sim")
        fe = HuaweiFrontEndSimulator(link, sess, addr=args.addr if args.addr is not None else 1,
                                     period_s=args.period, online_v=args.online_v,
                                     online_i_raw=args.online_i_raw, default_v=args.default_v,
                                     default_i_raw=args.default_i_raw, on_event=on_event)
        print(f"huawei-frontend-sim (the add-on control board) on {args.port} @ {args.bitrate}: "
              f"status request + 5 writes every {args.period} s; session: {sess.path}", flush=True)
        try:
            stats = fe.run(duration=args.duration)
        except KeyboardInterrupt:
            stats = fe.stats
        _print(args, f"stats: {stats}\nlast status seen: {fe.last_status}",
               {"stats": stats, "last_status": fe.last_status, "session": str(sess.path)})
        sess.close(); link.close()
        return 0

    if sub == "gateway":
        from .analyzer.huawei_gateway import HuaweiGateway
        sess = S.SessionWriter(args.session or S.new_session_path(_labelled("huawei-gateway", label)))
        psu_link = open_link(args.port, args.bitrate, name="module")
        fe_link = open_link(args.front_end, args.bitrate, name="front-end")
        addr = args.addr if args.addr is not None else hcfg.get("addr", 1)
        gw = HuaweiGateway(psu_link, fe_link, sess, hcfg, addr=addr, voltage=args.voltage,
                           current=args.current, resend_period=args.resend,
                           status_period=args.status_period, on_event=on_event,
                           fe_addr=args.front_end_addr)
        refusals = gw.prepare()
        report = {"sub": sub, "addr": addr, "module": args.port, "front_end": args.front_end,
                  "voltage": args.voltage, "current": args.current, "label": label}
        if refusals:
            report["refusals"] = refusals
            human = "\n".join(["gateway REFUSED:"] + [f"  {r}" for r in refusals])
            rc = 1
        else:
            print(f"gateway: module on {args.port}, front-end on {args.front_end}, addr {addr}, "
                  f"V={args.voltage} A={args.current} re-sent every {args.resend} s; front-end "
                  f"requests forwarded, its writes blocked + acked; Ctrl+C stops", flush=True)
            stats = gw.run(duration=args.duration)
            report["stats"] = stats
            lines = [f"gateway stats: {stats}"]
            if stats.get("fe_setpoints"):
                lines.append(f"  front-end wanted (blocked): {stats['fe_setpoints']}")
            if stats.get("stop_reason"):
                lines.append(f"  stopped: {stats['stop_reason']}")
            human = "\n".join(lines)
            rc = 0 if not stats.get("stop_reason") else 1
        if hcfg.get("addr_map"):
            _save_huawei(cfg, hcfg)
        report["session"] = str(sess.path)
        sess.close(); psu_link.close(); fe_link.close()
        _print(args, human + f"\nsession: {sess.path}", report)
        return rc

    sess = S.SessionWriter(args.session or S.new_session_path(_labelled(f"huawei-{sub}", label)))
    link = open_link(args.port, args.bitrate, name="ctl")
    addr = args.addr if args.addr is not None else hcfg.get("addr", 1)
    broadcast = bool(getattr(args, "broadcast", False))
    if broadcast and args.addr is not None:
        print("--broadcast and --addr are mutually exclusive (broadcast = address 0 = all "
              "units; --expect-acks lists who must answer)", file=sys.stderr)
        sess.close(); link.close()
        return 2
    expect = None
    if getattr(args, "expect_acks", None):
        expect = [int(x) for x in args.expect_acks.split(",") if x.strip()]
    tap_link = None
    if getattr(args, "tap", None):
        tap_link = open_link(args.tap, args.bitrate, name="tap", listen_only=True)
    cl = HuaweiClient(link, sess, "can", addr, hcfg, on_event,
                      target_addr=0 if broadcast else None, expect_acks=expect)
    report: dict = {"sub": sub, "addr": addr, "target": cl.target_label(), "port": args.port,
                    "bitrate": args.bitrate, "label": label}
    if label:
        sess.note(f"label {label}", "can", label=label)
    rc = 0
    try:
        if sub == "status":
            snap = cl.status(timeout=args.timeout)
            report["status"] = snap
            lines = [f"Huawei PSU addr {addr} status ({snap.get('frames', 0)} frames, "
                     f"{'complete' if snap.get('complete') else 'INCOMPLETE'}):"]
            for k, v in snap.items():
                if k in ("registers", "frames", "complete", "unknown"):
                    continue
                lines.append(f"  {k:28} {v}")
            if snap.get("unknown"):
                lines.append(f"  unknown registers: {snap['unknown']}")
            if cl.other_controller_frames:
                lines.append(f"  !! {cl.other_controller_frames} frames from ANOTHER controller "
                             f"seen - do not command this PSU from two places")
            human = "\n".join(lines)
            rc = 0 if snap.get("complete") else 1
        elif sub == "info":
            info = cl.info(timeout=args.timeout)
            report["info"] = info
            if info.get("serial_hex"):
                _save_huawei(cfg, hcfg)
            lines = [f"Huawei PSU addr {addr} info:"]
            for k, v in info.items():
                lines.append(f"  {k:20} {v}")
            if info.get("addr_change"):
                lines.append(f"  !! ADDR-CHANGE: {info['addr_change']} - the scale stored for "
                             f"the previous serial is NOT applied to this one")
            if info.get("serial_hex"):
                _, unit = unit_on_addr(hcfg, addr)
                lines.append(f"  bound: addr {addr} = serial {info['serial_hex']} "
                             f"(scale {unit.get('current_scale')}, nominal {unit.get('nominal_a')})")
            human = "\n".join(lines)
            rc = 0 if info.get("nominal_current_a") else 1
        elif sub == "set":
            if args.voltage is None and args.current is None:
                print("nothing to set: give --voltage and/or --current", file=sys.stderr)
                return 2
            rep = cl.set_points(args.voltage, args.current, apply=args.apply,
                                persist=args.persist, verify=not args.no_verify)
            report.update(rep)
            lines = [f"result: {rep['result']} target {rep['target']}"
                     + (f" expecting acks from {rep['expect_acks']}" if rep.get("expect_acks") else "")
                     + ("" if args.apply else " (dry run - add --apply to send)")]
            for r in rep.get("refusals", []):
                lines.append(f"  REFUSED: {r}")
            for p in rep.get("plan", []):
                lines.append(f"  frame {p['id']}  {p['hex']}   = {p['name']} "
                             f"{p['value']} {p['unit']} (raw {p['raw']}"
                             + (f", scale {p['scale']}" if p.get("scale") else "") + ")")
            for s in rep.get("sent", []):
                lines.append(f"  sent {s['id']} {s['hex']} -> {s['target']}: acked={s['acked']} "
                             f"ok={s.get('ok')} acks={s.get('acks')}"
                             + (f" MISSING {s['missing_acks']}" if s.get("missing_acks") else ""))
            if "verify" in rep:
                v = rep["verify"]
                lines.append(f"  verify: target {v['target_v']} V, measured "
                             f"{v['measured_v']} V -> {'OK' if v['verified'] else 'NOT within '
                             + str(v['tolerance_v']) + ' V'} {v.get('note', '')}")
            human = "\n".join(lines)
            rc = 0 if rep["result"] in ("dry-run", "applied") else 1
        elif sub == "calibrate-current":
            rep = cl.calibrate_current(test_a=args.test_a)
            report.update(rep)
            if rep.get("serial_hex"):
                _save_huawei(cfg, hcfg)
            lines = [f"current scale for serial {rep.get('serial_hex')} (addr {addr}): "
                     f"{rep.get('result')} "
                     f"({rep.get('source', rep.get('error', 'no candidate matched'))})"]
            for c in rep.get("candidates", []):
                lines.append(f"  {c}")
            human = "\n".join(lines)
            rc = 0 if rep.get("result") else 1
        elif sub == "hold":
            if args.poll_only and (args.voltage is not None or args.current is not None):
                print("--poll-only takes no --voltage/--current (status requests only)",
                      file=sys.stderr)
                return 2
            if args.silence_after is not None:
                print(PERSIST_REMINDER, flush=True)
            print(f"hold V={args.voltage} A={args.current} -> {cl.target_label()} "
                  f"(answers from addr {addr}); "
                  + ("status 1 s, NO set frames" if args.poll_only else "status 1 s, re-send 5 s")
                  + (f"; silence after {args.silence_after} s, then listen {args.observe} s on "
                     f"{args.tap or args.port}" if args.silence_after is not None else "")
                  + "; Ctrl+C stops (PSU reverts to defaults in ~60 s)", flush=True)
            stats = cl.hold(args.voltage, args.current, args.duration, poll_only=args.poll_only,
                            silence_after=args.silence_after, observe_s=args.observe,
                            tap_link=tap_link)
            report["stats"] = stats
            lines = [f"hold stats: {stats}"]
            for r in stats.get("refusals", []):
                lines.append(f"  REFUSED: {r}")
            if stats.get("first_offline_s") is not None:
                lines.append(f"  OFFLINE-TRANSITION at t={stats['first_offline_s']} s "
                             f"({stats.get('offline_evidence')})")
            elif args.poll_only:
                lines.append("  no offline transition seen while polling")
            if stats.get("silence"):
                sl = stats["silence"]
                lines.append(f"  SILENCE on {sl['measured_on']}: output disabled at "
                             f"{sl['t_first_output_disabled']} s, zero current at "
                             f"{sl['t_zero_current']} s, residual {sl['residual_current_a']} A, "
                             f"unsolicited period {sl['unsolicited_period_s']} s "
                             f"(relative to the last transmitted frame)")
            human = "\n".join(lines)
            rc = 0 if not stats.get("refusals") and not stats.get("stop_reason") else 1
        else:
            human = f"unknown sub-command {sub}"
            rc = 2
    finally:
        report["session"] = str(sess.path)
        if sub != "hold":                     # hold writes its own summary
            sess.summary(report, "can")
        sess.close()
        link.close()
        if tap_link is not None:
            tap_link.close()
    _print(args, human + f"\nsession: {sess.path}", report)
    return rc


def cmd_battery_sim(args) -> int:
    state = preset(args.preset)
    apply_overrides(state, parse_overrides(args.set))
    state.drift = args.drift
    if is_can_link_spec(args.port):
        from .simulators.can_battery_sim import CanBatterySimulator
        from .protocols.pylontech_can.dialects import CAN_BATTERY_PROFILES
        cprof = CAN_BATTERY_PROFILES[args.can_profile]
        sess = S.SessionWriter(args.session or S.new_session_path("battery-sim"))
        link = open_link(args.port, args.baud or cprof.bitrate, name="battery")
        sim = CanBatterySimulator(link, state, cprof, sess, chan="bat",
                                  on_event=_quiet if args.json else _echo, string=args.string)
        print(f"CAN battery-sim '{cprof.name}' (brand {cprof.brand!r}) on {args.port} @ "
              f"{link.bitrate}; session: {sess.path}", flush=True)
        try:
            stats = sim.run(duration=args.duration)
        except KeyboardInterrupt:
            stats = sim.stats
        _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
        sess.close(); link.close()
        return 0
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
    if is_can_link_spec(args.port):
        from .simulators.can_battery_sim import CanInverterSimulator
        from .protocols.pylontech_can.dialects import CAN_INVERTER_PROFILES
        cprof = CAN_INVERTER_PROFILES[args.can_profile]
        sess = S.SessionWriter(args.session or S.new_session_path("inverter-sim"))
        link = open_link(args.port, args.baud or cprof.bitrate, name="inverter")
        sim = CanInverterSimulator(link, cprof, sess, chan="inv",
                                   on_event=_quiet if args.json else _echo)
        print(f"CAN inverter-sim '{cprof.name}' on {args.port} @ {link.bitrate}; "
              f"session: {sess.path}", flush=True)
        try:
            stats = sim.run(duration=args.duration)
        except KeyboardInterrupt:
            stats = sim.stats
        _print(args, f"stats: {stats}", {"stats": stats, "snapshot": sim.snapshot,
                                         "ids_seen": sim.ids_seen, "session": str(sess.path)})
        sess.close(); link.close()
        return 0
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
    if is_can_link_spec(args.port):
        from .analyzer.identify_can import identify_can
        from .analyzer.huawei import huawei_config
        from .runner import load_config
        bitrates = tuple(int(b) for b in args.bitrates.split(","))
        link = open_link(args.port, bitrates[0], name="id", listen_only=True)
        report = identify_can(link, sess, listen_s=args.listen,
                              active=not args.passive_only, bitrates=bitrates,
                              on_event=_quiet if args.json else _echo,
                              hcfg=huawei_config(load_config()))
    else:
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
    if is_can_link_spec(args.port):
        _arm_deadline(getattr(args, 'max_seconds', None), 'soc')
        from .analyzer.can_battery import soc_can
        sess = S.SessionWriter(args.session or S.new_session_path("soc"))
        link = open_link(args.port, args.baud, name="soc",
                         listen_only=True)
        rep = soc_can(link, sess, listen_s=max(args.timeout, 3.0),
                      on_event=_quiet if args.json else _echo)
        rep["session"] = str(sess.path)
        human = [f"SOC via CAN broadcasts (brand {rep['brand']!r}, {rep['frames']} frames)",
                 f"{'string':>6} {'SOC%':>5} {'SOH%':>5} {'V':>7} {'A':>7} {'T':>6} {'mods':>5}"]
        for r in rep["rows"]:
            human.append(f"{r['string']:>6} {r['soc_percent']!s:>5} {r['soh_percent']!s:>5} "
                         f"{r['voltage_v']!s:>7} {r['current_a']!s:>7} "
                         f"{r['temperature_c']!s:>6} {r['module_count']!s:>5}")
        if not rep["rows"]:
            human.append("  no 0x355/0x356 heard" + (f" ({rep['error_frames']} error frames: "
                                                      f"wrong bitrate?)" if rep["error_frames"] else ""))
        _print(args, "\n".join(human), rep)
        sess.close(); link.close()
        return 0 if rep["rows"] else 2
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
    mode = f"0x42 FF @ ADR {used:02X}" if data is not None else None
    if data is None:
        # Firmware without the FF all-packs query (e.g. US5000 fw 2.3):
        # poll each pack address individually and assemble the stack.
        mods = []
        for adr in range(2, 10):
            pf, lat = _probe_pylontech(link, ver, adr, 0x42, f"{adr:02X}",
                                       session=sess, chan="soc",
                                       timeout=args.timeout)
            if pf is None or not pf.ok or not pf.info:
                if mods:
                    break        # contiguous range ended
                continue
            d = P.decode_42(pf.info)
            if d.get("modules"):
                m = d["modules"][0]; m["address"] = adr; mods.append(m)
        if mods:
            rem = sum(m["remaining_ah"] for m in mods)
            tot = sum(m["total_ah"] for m in mods)
            data = {"n_modules": len(mods), "modules": mods,
                    "stack_soc_percent": round(rem / tot * 100, 1) if tot else None}
            mode = f"per-pack 0x42 @ ADR {mods[0]['address']:02X}..{mods[-1]['address']:02X}"
    if data is None:
        _print(args, "no response (try --baud 115200, or run `rsh identify` "
                     "to find the address/baud)",
               {"error": "no response", "session": str(sess.path)})
        sess.close(); link.close(); return 2
    rows = []
    for i, m in enumerate(data["modules"], 1):
        rows.append({"pack": i, "soc_percent": m.get("soc_percent"),
                     "voltage_v": m.get("voltage_v"), "current_a": m.get("current_a"),
                     "remaining_ah": m.get("remaining_ah"),
                     "total_ah": m.get("total_ah"), "cycles": m.get("cycles")})
    human = [f"SOC via {mode}  ({data['n_modules']} packs, stack "
             f"{data['stack_soc_percent']}%)",
             f"{'pack':>4} {'SOC%':>5} {'V':>7} {'A':>7} {'remAh':>7} {'totAh':>7} {'cyc':>5}"]
    for r in rows:
        human.append(f"{r['pack']:>4} {r['soc_percent']!s:>5} "
                     f"{r['voltage_v']!s:>7} {r['current_a']!s:>7} "
                     f"{r['remaining_ah']!s:>7} {r['total_ah']!s:>7} "
                     f"{r['cycles']!s:>5}")
    out = {"mode": mode, "n_packs": data["n_modules"],
           "stack_soc_percent": data["stack_soc_percent"], "packs": rows,
           "session": str(sess.path)}
    _print(args, "\n".join(human), out)
    sess.close(); link.close()
    return 0


def cmd_dump(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'dump')
    from .analyzer.dump import dump_stack, format_dump
    sess = S.SessionWriter(args.session or S.new_session_path("dump"))
    if is_can_link_spec(args.port):
        from .analyzer.can_battery import dump_can, format_dump_can
        link = open_link(args.port, args.baud, name="dump",
                         listen_only=True)
        report = dump_can(link, sess, listen_s=args.listen, active=not args.passive_only,
                          on_event=_quiet if args.json else _echo)
        report["session"] = str(sess.path)
        report_path = S.save_report(sess.path, report, "dump")
        report["report_file"] = str(report_path)
        _print(args, format_dump_can(report) + f"\nsession: {sess.path}\n"
               f"full JSON: {report_path}", report)
        sess.close(); link.close()
        return 0 if report.get("result") == "ok" else 2
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


def cmd_timeline(args) -> int:
    from .analyzer.timeline import TimelineConfig, build_timeline, export, format_timeline
    path = args.session
    if not path:
        caps = sorted(S.CAPTURE_DIR.glob("*.jsonl"))
        if not caps:
            print("no captures found", file=sys.stderr)
            return 1
        path = caps[-1]
    cfg = TimelineConfig(large_ccl_increase_a=args.large_ccl_increase,
                         current_tolerance_a=args.current_tolerance,
                         response_delay_s=args.response_delay,
                         ccl_factor=args.ccl_factor, current_factor=args.current_factor)
    result = build_timeline(path, cfg, service_path=args.service)
    paths = export(result, path)
    result["exports"] = paths
    human = format_timeline(result) + "\nexports: " + ", ".join(
        f"{k}={v}" for k, v in paths.items())
    slim = {k: v for k, v in result.items()
            if k not in ("series", "service_series")}
    _print(args, human, slim)
    return 0


def cmd_mark(args) -> int:
    """Append a timestamped marker to a session (from another terminal, or
    after the fact with --at ISO/epoch)."""
    import datetime as _dt
    path = args.session
    if not path:
        caps = sorted(S.CAPTURE_DIR.glob("*.jsonl"))
        if not caps:
            print("no captures found", file=sys.stderr)
            return 1
        path = caps[-1]
    ts = None
    if args.at:
        try:
            ts = float(args.at)
        except ValueError:
            ts = _dt.datetime.fromisoformat(args.at).timestamp()
    sess = S.SessionWriter(path)
    text = " ".join(args.text)
    if not text.upper().startswith("MARK"):
        text = "MARK " + text
    sess.log("-", "-", b"", kind="note", note=text, marker=True, ts=ts)
    sess.close()
    print(f"marked {path}: {text}")
    return 0


def cmd_discover(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'discover')
    from .analyzer.discover import discover, format_discovery
    sess = S.SessionWriter(args.session or S.new_session_path("discover"))
    if is_can_link_spec(args.port):
        from .analyzer.can_battery import discover_can, format_discovery_can
        link = open_link(args.port, args.baud, name="disc",
                         listen_only=True)
        report = discover_can(link, sess, listen_s=args.listen, active=not args.passive_only,
                              on_event=_quiet if args.json else _echo)
        report["session"] = str(sess.path)
        report_path = S.save_report(sess.path, report, "discovery")
        report["report_file"] = str(report_path)
        _print(args, format_discovery_can(report) +
               f"\nsession: {sess.path}\nreport:  {report_path}", report)
        sess.close(); link.close()
        return 0 if report.get("result") == "ok" else 2
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


def _service_monitor(port: str, baud: int, device: int, interval: float,
                     sess, on_event, print_limits: bool):
    """Open a Seplos service socket and verify it answers. Returns
    (monitor, link) or (None, link) with the reason already printed."""
    from .analyzer.seplos_monitor import ServiceMonitor
    link = _open_serial(port, baud, "svc")
    mon = ServiceMonitor(link, sess, slave=device, interval_s=interval,
                         on_event=on_event, print_limits=print_limits)
    if mon.poll_once() is None:
        print(f"!! no Seplos service socket on {port} @ {baud} (device "
              f"{device}). It must be one of the pack's RS485-1/-2 sockets, "
              f"not the inverter socket and not CAN; `rsh discover --port "
              f"{port}` tells which kind of socket this is.", flush=True)
        return None, link
    return mon, link


def cmd_seplos_monitor(args) -> int:
    sess = S.SessionWriter(args.session or S.new_session_path("seplos-monitor"))
    mon, link = _service_monitor(args.port, args.baud, args.device,
                                 args.interval, sess, _echo, True)
    if mon is None:
        sess.close()
        link.close()
        return 1
    print(f"monitoring Seplos service socket {args.port} @ {args.baud} every "
          f"{args.interval}s (read-only); Ctrl+C to stop; session: {sess.path}",
          flush=True)
    stats = mon.run(duration=args.duration)
    _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
    sess.close()
    link.close()
    return 0


def cmd_seplos_set(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'seplos-set')
    from .analyzer.seplos_native import write_setting
    sess = S.SessionWriter(args.session or S.new_session_path("seplos-set"))
    link = _open_serial(args.port, args.baud, "svc")
    print(f"Seplos service socket {args.port} @ {args.baud}, device "
          f"{args.device}: " + ("APPLYING the change" if args.apply else
                                "dry run (nothing is written; add --apply)"),
          flush=True)
    report = write_setting(link, sess, "svc", args.device, args.setting,
                           args.value, apply=args.apply,
                           allow_increase=args.allow_increase,
                           on_event=_quiet if args.json else _echo)
    report["session"] = str(sess.path)
    human = [f"result: {report['result']}"]
    if "before" in report:
        human.append(f"  {report['setting']} {report['name']}: before "
                     f"{report['before']} {report['unit']}"
                     + (f", after {report['after']} {report['unit']}"
                        if "after" in report else ""))
    if report["result"] == "dry-run":
        human.append(f"  would send: {report['frame_hex']}")
    if report["result"] == "written" and "restore" in report:
        human.append(f"  to undo: rsh {report['restore']} --port {args.port}")
    human.append(f"session: {sess.path}")
    _print(args, "\n".join(human), report)
    sess.close()
    link.close()
    return 0 if report["result"] in ("written", "dry-run", "unchanged") else 1


def cmd_sniff(args) -> int:
    from .analyzer.sniff import Sniffer
    sess = S.SessionWriter(args.session or S.new_session_path(_labelled("sniff", args.label)))
    if args.label:
        sess.note(f"label {args.label}", label=args.label)
    links = {}
    links["ch1"] = open_link(args.ch1, args.baud, name="ch1", listen_only=True)
    if args.ch2:
        links["ch2"] = open_link(args.ch2, args.baud, name="ch2", listen_only=True)
    sn = Sniffer(links, sess, on_event=_quiet if args.json else _echo,
                 markers=not args.no_markers, quiet=args.quiet)
    mon = svc_link = None
    if args.service:
        # Seplos service socket in the same session, on the same clock
        mon, svc_link = _service_monitor(
            args.service, args.service_baud, args.service_device,
            args.service_interval, sess, _quiet if args.json else _echo, False)
        if mon is None:
            print("   continuing with the tap only", flush=True)
        else:
            mon.start(duration=args.duration)
    print(f"sniffing {', '.join(l.name for l in links.values())} @ "
          f"{args.baud} baud"
          + (f" + Seplos service socket {args.service} (read-only)" if mon else "")
          + f"; Ctrl+C to stop; session: {sess.path}", flush=True)
    stats = sn.run(duration=args.duration)
    if mon is not None:
        mon.stop()
        stats = {**stats, "service": mon.stats}
    _print(args, f"stats: {stats}", {"stats": stats, "session": str(sess.path)})
    sess.close()
    for link in links.values():
        link.close()
    if svc_link is not None:
        svc_link.close()
    return 0


def cmd_bridge(args) -> int:
    _arm_deadline(None, "bridge")
    from .analyzer.bridge import Bridge
    from .analyzer.translate import Translator
    from .analyzer.policy import PolicyEngine, make_policy
    sess = S.SessionWriter(args.session or S.new_session_path("bridge"))
    if is_can_link_spec(args.ch1) or is_can_link_spec(args.ch2):
        if not (is_can_link_spec(args.ch1) and is_can_link_spec(args.ch2)):
            print("bridge: both channels must be CAN (can:...) or both serial - a "
                  "CAN<->RS485 translation is not a bridge job", file=sys.stderr)
            return 2
        from .analyzer.can_bridge import CanBridge, CanPolicyEngine
        from .analyzer.policy import make_policy
        a = open_link(args.ch1, args.baud, name="ch1")
        b = open_link(args.ch2, args.baud, name="ch2")
        eng = None
        if args.policy:
            eng = CanPolicyEngine(make_policy(args.policy), loss_timeout_s=args.loss_timeout)
        br = CanBridge(a, b, sess, engine=eng, role_a=args.role_ch1, role_b=args.role_ch2,
                       markers=not args.no_markers, quiet=args.quiet,
                       on_event=_quiet if args.json else _echo)
        print(f"CAN gateway {args.ch1}({args.role_ch1}) <-> {args.ch2}({args.role_ch2}) @ "
              f"{a.bitrate}; MODE={br.mode}"
              + (f" [policy {eng.policy.describe()} on 0x351 BMS->INV]" if eng else "")
              + f"; Ctrl+C to stop; session: {sess.path}", flush=True)
        stats = br.run(duration=args.duration)
        _print(args, f"stats: {stats}", {"stats": stats, "mode": br.mode,
                                          "session": str(sess.path)})
        sess.close(); a.close(); b.close()
        return 0
    a = _open_serial(args.ch1, args.baud, "ch1")
    b = _open_serial(args.ch2, args.baud, "ch2")
    if args.detect_echo:
        a.detect_echo()
        b.detect_echo()
    tr = None
    if args.fix_cid2_echo or args.force_ver or args.force_adr or args.policy:
        tr = Translator()
        if args.fix_cid2_echo:
            tr.fix_cid2_echo = {"ch1->ch2", "ch2->ch1"}
        for spec in args.force_ver or []:
            d, v = spec.split("=")
            tr.force_ver[{"a2b": "ch1->ch2", "b2a": "ch2->ch1"}[d]] = int(v, 16)
        for spec in args.force_adr or []:
            d, v = spec.split("=")
            tr.force_adr[{"a2b": "ch1->ch2", "b2a": "ch2->ch1"}[d]] = int(v, 16)
        if args.policy:
            scales = {}
            if args.scale_a:
                scales["a"] = args.scale_a
            if args.scale_v:
                scales["v"] = args.scale_v
            tr.engine = PolicyEngine(make_policy(args.policy), scales=scales,
                                     loss_timeout_s=args.loss_timeout)
    br = Bridge(a, b, sess, translator=tr, role_a=args.role_ch1, role_b=args.role_ch2,
                markers=not args.no_markers, quiet=args.quiet,
                on_event=_quiet if args.json else _echo)
    print(f"bridging {args.ch1}({args.role_ch1}) <-> {args.ch2}({args.role_ch2}) @ "
          f"{args.baud} baud; MODE={br.mode}"
          + (f" [{tr.describe()}]" if tr else "")
          + f"; Ctrl+C to stop; session: {sess.path}", flush=True)
    stats = br.run(duration=args.duration)
    _print(args, f"stats: {stats}", {"stats": stats, "mode": br.mode,
                                      "session": str(sess.path)})
    sess.close()
    a.close()
    b.close()
    return 0


def cmd_selftest(args) -> int:
    _arm_deadline(getattr(args, 'max_seconds', None), 'selftest')
    from .analyzer.selftest import run_selftest
    sess = S.SessionWriter(args.session or S.new_session_path("selftest"))
    if is_can_link_spec(args.port1):
        from .analyzer.can_bridge import run_can_selftest
        print("CAN self-test requires both channels on ONE terminated bus: CAN-H to CAN-H, "
              "CAN-L to CAN-L, 120 ohm at each end.", flush=True)
        l1 = open_link(args.port1, 500000, name="ch0")
        l2 = open_link(args.port2, 500000, name="ch1")
        try:
            report = run_can_selftest(l1, l2, session=sess, on_event=_quiet if args.json else _echo)
        finally:
            l1.close(); l2.close()
    else:
        print("Self-test requires ch1 and ch2 joined on one RS485 bus: "
              "A-A, B-B (straight, not crossed) plus GND.", flush=True)
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
    p.add_argument("--can-profile", default="pylontech-lv",
                   choices=sorted(__import__("rs485harness.protocols.pylontech_can.dialects",
                                             fromlist=["CAN_BATTERY_PROFILES"]).CAN_BATTERY_PROFILES),
                   help="CAN ports: battery dialect (pylontech-lv, victron, dyness-l, "
                        "luxpower, seplos-can)")
    p.add_argument("--string", type=int, default=0,
                   help="CAN ports: LV-HUB string address (id + 0x1000*n)")
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
    p.add_argument("--can-profile", default="pylon-inverter",
                   choices=sorted(__import__("rs485harness.protocols.pylontech_can.dialects",
                                             fromlist=["CAN_INVERTER_PROFILES"]).CAN_INVERTER_PROFILES),
                   help="CAN ports: inverter behaviour (pylon-inverter, victron-gx, "
                        "pylon-unlock, silent-inverter)")
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
    p.add_argument("--bitrates", default="500000,250000,125000",
                   help="CAN ports (can:...): bitrates to try, listen-only")
    common(p)
    p.set_defaults(fn=cmd_identify)

    p = sub.add_parser("huawei", help="Huawei R48xx rectifier over CAN: status, info, "
                                      "calibrate-current, set, hold, gateway, caps, sim, "
                                      "frontend-sim")
    hs = p.add_subparsers(dest="sub", required=True)

    def hcommon(hp, bus=True):
        if bus:
            hp.add_argument("--port", required=True, help="can:gs:0 / can:gs:0.1 / "
                                                          "can:virtual:<name>")
            hp.add_argument("--bitrate", type=int, default=125000)
            hp.add_argument("--timeout", type=float, default=1.0, help="reply window s")
            hp.add_argument("--label", help="appended to the session file name "
                                            "(Phase B naming, e.g. B3.1-<serial>)")
        hp.add_argument("--addr", type=int, help="PSU address (default: config or 1)")
        common(hp, session=bus)

    def htarget(hp):
        hp.add_argument("--broadcast", action="store_true",
                        help="send to address 0 (all units); exclusive with --addr")
        hp.add_argument("--expect-acks", help="with --broadcast: addresses that must ack, "
                                              "e.g. 1,2 (default: the configured addr)")
    hp = hs.add_parser("status", help="one 0x40 request, decoded burst (read-only)")
    hcommon(hp)
    hp = hs.add_parser("info", help="0x50 + e-label: nominal current, serial, versions, "
                                    "slot id (read-only; stores nominal_a)")
    hcommon(hp)
    hp = hs.add_parser("set", help="setpoints; DRY RUN unless --apply")
    hp.add_argument("--voltage", type=float, help="V (on-line reg 0x00, or 0x01 with --persist)")
    hp.add_argument("--current", type=float, help="A limit (reg 0x03 / 0x04); needs a "
                                                  "calibrated scale and a current cap")
    hp.add_argument("--persist", action="store_true",
                    help="write the NON-VOLATILE default registers 0x01/0x04 instead")
    hp.add_argument("--apply", action="store_true", help="really send")
    hp.add_argument("--no-verify", action="store_true",
                    help="skip the measurement check after --apply")
    htarget(hp)
    hcommon(hp)
    hp = hs.add_parser("calibrate-current", help="find which current scale this unit "
                                                 "obeys (needs a load > --test-a)")
    hp.add_argument("--test-a", type=float, default=5.0)
    hcommon(hp)
    hp = hs.add_parser("hold", help="keep-alive loop: status 1 s, re-send setpoints 5 s, "
                                    "CHANGE lines; Ctrl+C stops; --poll-only / "
                                    "--silence-after measure the fallback")
    hp.add_argument("--voltage", type=float)
    hp.add_argument("--current", type=float)
    hp.add_argument("--duration", type=float)
    hp.add_argument("--poll-only", action="store_true",
                    help="status requests only, never a set frame (B3.4: does polling "
                         "keep the unit online?)")
    hp.add_argument("--silence-after", type=float, metavar="S",
                    help="after S s stop transmitting and only listen for --observe s: "
                         "time to output-disabled / zero current (B3.1, B4, B6)")
    hp.add_argument("--observe", type=float, default=120.0,
                    help="listen window after --silence-after (default 120 s)")
    hp.add_argument("--tap", metavar="PORT",
                    help="listen-only second channel (e.g. can:gs:0.1) the silence "
                         "measurement is taken from; recorded in the same session")
    htarget(hp)
    hcommon(hp)
    hp = hs.add_parser("caps", help="show/set hard caps and the current scale in "
                                    ".rsh-config.json (no bus access)")
    hp.add_argument("--voltage-max", type=float)
    hp.add_argument("--current-max", type=float)
    hp.add_argument("--current-scale", help="ratio1024 | x20 | x30 | none (per serial)")
    hp.add_argument("--nominal-a", type=float, help="per serial")
    hp.add_argument("--serial", help="12-hex serial the scale/nominal belong to (from "
                                     "`info`); default: the serial bound to --addr")
    hcommon(hp, bus=False)
    hp = hs.add_parser("sim", help="simulated R48xx PSU on a CAN link (bench substitute)")
    hp.add_argument("--nominal-a", type=float, default=52.0)
    hp.add_argument("--scale", default="ratio1024", choices=["ratio1024", "x20", "x30"])
    hp.add_argument("--load-ohm", type=float)
    hp.add_argument("--load-w", type=float)
    hp.add_argument("--battery-v", type=float)
    hp.add_argument("--duration", type=float)
    hp.add_argument("--online-timeout", type=float, default=60.0,
                    help="s without a command before the sim reverts to defaults")
    hp.add_argument("--default-current-a", type=float,
                    help="non-volatile current limit (reg 0x04) at start; default nominal")
    hp.add_argument("--serial", default="0000000000A1", help="12-hex serial of this unit")
    hp.add_argument("--polls-refresh-timeout", choices=["yes", "no"], default="yes",
                    help="do 0x40 status requests keep the unit online (yes) or only "
                         "0x80 sets (no)? - the B3.4 question")
    hp.add_argument("--reject-zero-default-current", action="store_true",
                    help="ack a default-current write of 0 with 0x21 (the B3.0 FAIL case)")
    hp.add_argument("--on-ac-loss", choices=["reset", "keep"], default="reset",
                    help="whether on-line values survive an AC loss (sources disagree)")
    hp.add_argument("--ac-off-after", type=float, metavar="S", help="drop AC after S s")
    hp.add_argument("--ac-on-after", type=float, metavar="S", help="restore AC after S s")
    hp.add_argument("--negotiate", action="store_true",
                    help="emit the power-up address negotiation (30 01 x3, 30 02 x8)")
    hcommon(hp)
    hp = hs.add_parser("frontend-sim", help="simulated add-on control board: status request + "
                                            "5 writes every 0.62 s, cfg 0x13 every 3 s "
                                            "(the gateway's test partner)")
    hp.add_argument("--period", type=float, default=0.62)
    hp.add_argument("--online-v", type=float, default=48.0)
    hp.add_argument("--online-i-raw", type=int, default=70)
    hp.add_argument("--default-v", type=float, default=50.0)
    hp.add_argument("--default-i-raw", type=int, default=210)
    hp.add_argument("--duration", type=float)
    hcommon(hp)
    hp = hs.add_parser("gateway", help="sit between the add-on control board and the module: "
                                       "our setpoints to the module, the board's requests "
                                       "forwarded, its writes blocked + acked")
    hp.add_argument("--front-end", required=True, metavar="PORT",
                    help="channel wired to the control board, e.g. can:gs:0.1")
    hp.add_argument("--voltage", type=float, help="our on-line voltage (caps apply)")
    hp.add_argument("--current", type=float, help="our on-line current limit (caps, scale, "
                                                  "identity apply)")
    hp.add_argument("--resend", type=float, default=5.0, help="re-send period s (default 5)")
    hp.add_argument("--status-period", type=float, default=1.0,
                    help="own status poll period when the board is quiet (default 1 s)")
    hp.add_argument("--front-end-addr", type=int, metavar="N",
                    help="address the board uses for its module (default: same as --addr); "
                         "e.g. --addr 2 --front-end-addr 1 relabels identifiers both ways")
    hp.add_argument("--duration", type=float)
    hcommon(hp)
    p.set_defaults(fn=cmd_huawei)

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
    p.add_argument("--adrs", help="comma-separated addresses (default: scan 0..15)")
    p.add_argument("--ver", help="protocol version byte hex (default 20)")
    p.add_argument("--listen", type=float, default=4.0,
                   help="CAN ports: seconds to collect broadcasts")
    p.add_argument("--passive-only", action="store_true",
                   help="CAN ports: never send the 0x305/0x307 unlock")
    common(p)
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("discover", help="full capability + state inventory "
                                        "of a responder (run identify first)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--adr", type=lambda s: int(s, 0),
                   help="skip the scan, discover at this address")
    p.add_argument("--ver", help="protocol version byte, hex (default 20)")
    p.add_argument("--listen", type=float, default=6.0,
                   help="CAN ports: seconds to collect broadcasts")
    p.add_argument("--passive-only", action="store_true",
                   help="CAN ports: never send the 0x305/0x307 unlock")
    common(p)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("seplos-set", help="EXPERIMENTAL: change one charge-"
                                          "request setting on a Seplos BMS "
                                          "3.0 service socket (dry run "
                                          "unless --apply)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=19200)
    p.add_argument("--device", type=int, default=0,
                   help="Modbus device id = pack number - 1 (default 0)")
    p.add_argument("--setting", type=int, required=True,
                   help="102 = Charging request voltage (V), "
                        "103 = Charging request current (A)")
    p.add_argument("--value", type=float, required=True)
    p.add_argument("--apply", action="store_true",
                   help="really write; without it only the reads happen")
    p.add_argument("--allow-increase", action="store_true",
                   help="permit a value higher than the present one "
                        "(needed to restore the old value)")
    common(p)
    p.set_defaults(fn=cmd_seplos_set)

    p = sub.add_parser("sniff", help="passive capture (tap) on one or two channels")
    p.add_argument("--ch1", required=True)
    p.add_argument("--ch2")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--duration", type=float)
    p.add_argument("--no-markers", action="store_true",
                   help="do not read MARK lines from stdin")
    p.add_argument("--quiet", action="store_true",
                   help="no per-frame lines; print only decoded CHANGE lines "
                        "(CCL/CVL/enable/OV-warn/current), a 30s STATE "
                        "heartbeat and your markers - recommended at the bench")
    p.add_argument("--service", metavar="PORT",
                   help="also monitor a Seplos BMS 3.0 service socket "
                        "(RS485-1/-2) on this port, read-only, into the same "
                        "session: all cell voltages, alarm/protection flags, "
                        "charge switch, balancing")
    p.add_argument("--service-baud", type=int, default=19200)
    p.add_argument("--service-device", type=int, default=0,
                   help="Modbus device id = pack number - 1 (default 0)")
    p.add_argument("--service-interval", type=float, default=2.0, help="s")
    p.add_argument("--label", help="appended to the session file name (e.g. B5.1-<serial>)")
    common(p)
    p.set_defaults(fn=cmd_sniff)

    p = sub.add_parser("seplos-monitor", help="continuous read-only monitor "
                                              "on a Seplos BMS 3.0 service "
                                              "socket (cells, flags, charge "
                                              "switch, balancing)")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=19200)
    p.add_argument("--device", type=int, default=0,
                   help="Modbus device id = pack number - 1 (default 0)")
    p.add_argument("--interval", type=float, default=2.0, help="s between polls")
    p.add_argument("--duration", type=float)
    common(p)
    p.set_defaults(fn=cmd_seplos_monitor)

    p = sub.add_parser("timeline", help="charge-control timeline: pairing, decoded "
                                        "series, events, timing, findings, CSV/JSON(/PNG)")
    p.add_argument("session", nargs="?", help="session file (default: newest capture)")
    p.add_argument("--large-ccl-increase", type=float, default=10.0, help="A")
    p.add_argument("--current-tolerance", type=float, default=2.0, help="A over CCL")
    p.add_argument("--response-delay", type=float, default=5.0,
                   help="s after a CCL change before judging current vs CCL")
    p.add_argument("--ccl-factor", type=float, default=1.0,
                   help="scaling override for CCL/DCL (H5), e.g. 0.1")
    p.add_argument("--current-factor", type=float, default=1.0,
                   help="scaling override for pack current (H5)")
    p.add_argument("--service", metavar="FILE",
                   help="merge a Seplos service-socket session recorded "
                        "separately by `rsh seplos-monitor` (not needed when "
                        "the capture came from `rsh sniff --service`)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_timeline)

    p = sub.add_parser("mark", help="append a MARK note to a session")
    p.add_argument("text", nargs="+", help="e.g. maxcell=3.550")
    p.add_argument("--session", help="session file (default: newest capture)")
    p.add_argument("--at", help="timestamp (epoch or ISO) instead of now")
    p.set_defaults(fn=cmd_mark)

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
    p.add_argument("--role-ch1", choices=["inverter", "battery"], default="inverter")
    p.add_argument("--role-ch2", choices=["inverter", "battery"], default="battery")
    p.add_argument("--policy", metavar="NAME[:k=v,...]",
                   help="payload policy on BMS->INV responses (MODIFY mode), "
                        "e.g. cap:ccl_max=40")
    p.add_argument("--loss-timeout", type=float, default=10.0,
                   help="s without a valid BMS frame before the policy resets")
    p.add_argument("--scale-a", type=float, help="raw units per ampere (default 10)")
    p.add_argument("--scale-v", type=float, help="raw units per volt (default 1000)")
    p.add_argument("--no-markers", action="store_true")
    p.add_argument("--quiet", action="store_true",
                   help="no per-frame lines; only CHANGE/STATE lines + markers")
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
    p = sub.add_parser("tui", help="full-screen interactive UI for the core "
                                   "functions (Textual; pip install textual)")
    p.add_argument("--port", default="", help="pre-fill PORT (COM5, /dev/ttyUSB0, usb:A)")
    p.add_argument("--port2", default="", help="pre-fill PORT2")
    p.add_argument("--baud", type=int, default=9600)
    p.set_defaults(fn=None)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "menu":
        from .runner import run_menu
        return run_menu()
    if args.cmd == "tui":
        from .tui import run_tui
        return run_tui(args.port, args.port2, args.baud)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
