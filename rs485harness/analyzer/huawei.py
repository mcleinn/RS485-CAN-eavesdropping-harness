"""Huawei R48xx rectifier control over CAN - the logic behind `rsh huawei`.

Safety invariants (docs/CAN-PLAN.md 3.6), enforced here, not in the CLI:
- hard caps from .rsh-config.json ("huawei": {"caps": {...}}); a setpoint
  above a cap is refused, never clamped. Until caps are set, the defaults
  are 55.2 V and 0 A - i.e. no current setpoint at all.
- current setpoints need an established scale policy for THIS unit
  (`calibrate-current` writes it); without it `set --current` refuses.
- dry run by default; `apply=True` sends; `persist=True` is a separate,
  explicit choice for the non-volatile registers.
- every transmitted frame is logged with its ack and the audit fields
  (policy/scale/cap), set values are verified by measurement (0x75)
  because they cannot be read back.
- the hold loop stops sending on Ctrl+C or any error; the PSU then reverts
  to its stored defaults by itself (~60 s).
"""
from __future__ import annotations

import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..core.session import SessionWriter
from ..protocols.huawei_r48xx import regs as R
from ..protocols.huawei_r48xx.ids import (HuaweiId, CMD_DATA, CMD_INFO, CMD_ELABEL,
                                          CMD_SET, BITRATE)

DEFAULT_CAPS = {"voltage_max": 55.2, "current_max": 0.0}
VERIFY_TOL_V = 0.3
VERIFY_SETTLE_S = 2.0


def huawei_config(cfg: dict) -> dict:
    """The 'huawei' block of .rsh-config.json with defaults filled in."""
    h = dict(cfg.get("huawei") or {})
    caps = dict(DEFAULT_CAPS)
    caps.update(h.get("caps") or {})
    h["caps"] = caps
    h.setdefault("current_scale", None)
    h.setdefault("nominal_a", None)
    h.setdefault("addr", 1)
    return h


class HuaweiClient:
    def __init__(self, link: CanLink, session: SessionWriter, chan: str = "can",
                 addr: int = 1, hcfg: dict | None = None, on_event=None):
        self.link = link
        self.session = session
        self.chan = chan
        self.addr = addr
        self.hcfg = hcfg or huawei_config({})
        self.on_event = on_event or (lambda line: None)
        self.nominal_a: float | None = self.hcfg.get("nominal_a")
        self.scale: str | None = self.hcfg.get("current_scale")
        self.other_controller_frames = 0
        self.unsolicited = 0

    # ------------------------------------------------------------ plumbing
    def _log_rx(self, msg: CanMsg) -> dict | None:
        dec = R.decode(msg, self.nominal_a)
        if dec:
            if dec["flow"] == "CTL->PSU" and not msg.echo:
                self.other_controller_frames += 1
            if dec["kind"] == "unsolicited":
                self.unsolicited += 1
        log_can(self.session, self.chan, "rx", msg, decoded=dec,
                flow=dec["flow"] if dec else None, mode="ctl")
        return dec

    def _send(self, msg: CanMsg, **audit) -> bool:
        acked = True
        err = None
        try:
            self.link.send(msg, timeout=0.5)
        except CanTxTimeout as e:
            acked, err = False, str(e)
        log_can(self.session, self.chan, "tx", msg, decoded=R.decode(msg, self.nominal_a),
                flow="CTL->PSU", acked=acked, mode="ctl",
                errors=[err] if err else None, **audit)
        if not acked:
            self.on_event(f"!! no ACK for {msg.id_str} - nobody on the bus / wrong "
                          f"bitrate / termination?")
        return acked

    def _collect(self, timeout: float, want, done) -> list[CanMsg]:
        """Receive until done(decoded) or timeout; every frame is logged,
        frames for which want(decoded) is true are returned."""
        got: list[CanMsg] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.link.recv(timeout=min(0.1, max(0.005, deadline - time.monotonic())))
            if msg is None or msg.echo:
                continue
            dec = self._log_rx(msg)
            if dec and want(dec):
                got.append(msg)
                if done(dec):
                    break
        return got

    # ----------------------------------------------------------- read-only
    def status(self, timeout: float = 1.0) -> dict:
        """One 0x40 request, decoded burst of this PSU."""
        req = R.request_frame(CMD_DATA, self.addr)
        if not self._send(req, note="status request"):
            return {"error": "no ACK on status request", "complete": False, "frames": 0}
        msgs = self._collect(timeout,
                             lambda d: d["kind"] == "status" and d["addr"] == self.addr,
                             lambda d: not d["cont"])
        burst = R.StatusBurst()
        for m in msgs:
            burst.feed(m)
        return burst.snapshot(self.nominal_a, self.scale)

    def info(self, timeout: float = 1.0) -> dict:
        out: dict = {"addr": self.addr}
        if not self._send(R.request_frame(CMD_INFO, self.addr), note="info request"):
            out["error"] = "no ACK on info request"
            return out
        msgs = self._collect(timeout, lambda d: d["kind"] == "info" and d["addr"] == self.addr,
                             lambda d: not d["cont"])
        for m in msgs:
            dec = R.decode_info_data(m.data)
            if dec.get("name") == "characteristics":
                out["nominal_current_a"] = dec["nominal_current_a"]
                self.nominal_a = dec["nominal_current_a"]
            elif dec.get("name") == "serial":
                out["serial_hex"] = dec["serial_hex"]
            elif dec.get("name") == "barcode":
                out["barcode"] = out.get("barcode", "") + dec["ascii"]
            elif dec.get("name") == "versions":
                out["versions"] = {k: dec[k] for k in ("hw_version", "dcdc_sw", "pfc_sw")}
            elif dec.get("name") == "slot_id":
                out["slot_id"] = dec["slot_id"]
        out["info_frames"] = len(msgs)
        if self._send(R.request_frame(CMD_ELABEL, self.addr), note="e-label request"):
            el = R.ELabel()
            msgs = self._collect(max(timeout, 2.0),
                                 lambda d: d["kind"] == "elabel" and d["addr"] == self.addr,
                                 lambda d: not d["cont"])
            for m in msgs:
                el.feed(m)
            out["elabel"] = el.fields()
            out["elabel_complete"] = el.complete
        return out

    # ----------------------------------------------------------- setpoints
    def check_caps(self, voltage: float | None, current: float | None) -> list[str]:
        caps = self.hcfg["caps"]
        refusals = []
        if voltage is not None and voltage > caps["voltage_max"]:
            refusals.append(f"voltage {voltage} V exceeds cap {caps['voltage_max']} V "
                            f"(.rsh-config.json huawei.caps.voltage_max) - refused, not clamped")
        if current is not None:
            if current > caps["current_max"]:
                refusals.append(f"current {current} A exceeds cap {caps['current_max']} A "
                                f"(huawei.caps.current_max; 0 until you set it) - refused")
            if not self.scale:
                refusals.append("current scale for this unit not established - run "
                                "`rsh huawei calibrate-current` first (or set "
                                "huawei.current_scale explicitly)")
            elif R.CURRENT_SCALES[self.scale].needs_nominal and not self.nominal_a:
                refusals.append("ratio1024 scale needs the nominal current - run `info` "
                                "(stored as huawei.nominal_a)")
        return refusals

    def plan_set(self, voltage: float | None, current: float | None,
                 persist: bool = False) -> list[dict]:
        """The frames a set would send (dry run output)."""
        plan = []
        if voltage is not None:
            name = "default_voltage" if persist else "online_voltage"
            msg, raw = R.set_frame(name, voltage, self.addr)
            plan.append({"name": name, "value": voltage, "unit": "V", "raw": raw,
                         "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                         "in_range": R.voltage_in_range(name, voltage)})
        if current is not None:
            name = "default_current" if persist else "online_current"
            msg, raw = R.set_frame(name, current, self.addr, self.nominal_a, self.scale)
            plan.append({"name": name, "value": current, "unit": "A", "raw": raw,
                         "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                         "scale": self.scale, "nominal_a": self.nominal_a,
                         "in_range": raw <= 1250 if self.scale == "ratio1024" else True})
        return plan

    def send_set(self, item: dict, ack_timeout: float = 0.5) -> dict:
        msg: CanMsg = item["msg"]
        acked = self._send(msg, note=f"set {item['name']}={item['value']}{item['unit']}",
                           scale=item.get("scale"), cap=self.hcfg["caps"], policy="manual")
        res = {"name": item["name"], "value": item["value"], "raw": item["raw"],
               "id": item["id"], "hex": item["hex"], "acked": acked, "ack": None}
        if not acked:
            return res
        msgs = self._collect(ack_timeout, lambda d: d["kind"] == "ack" and d["addr"] == self.addr,
                             lambda d: True)
        if msgs:
            res["ack"] = R.decode_ack_data(msgs[0].data)
            res["ok"] = res["ack"]["ok"]
        else:
            res["ok"] = False
            res["error"] = "no ack frame within timeout"
        return res

    def verify_voltage(self, target: float, settle_s: float = VERIFY_SETTLE_S,
                       tol: float = VERIFY_TOL_V) -> dict:
        time.sleep(settle_s)
        snap = self.status()
        v = snap.get("out_voltage_v")
        res = {"target_v": target, "measured_v": v, "tolerance_v": tol,
               "out_current_a": snap.get("out_current_a")}
        if v is None:
            res["verified"] = False
            res["note"] = "no status reply"
        else:
            res["verified"] = abs(v - target) <= tol
            if not res["verified"]:
                res["note"] = ("output below target: current limit or load holds it down"
                               if v < target - tol else "output above target")
        return res

    def set_points(self, voltage: float | None, current: float | None, apply: bool = False,
                   persist: bool = False, verify: bool = True) -> dict:
        report: dict = {"addr": self.addr, "apply": apply, "persist": persist,
                        "refusals": self.check_caps(voltage, current), "plan": []}
        if report["refusals"]:
            report["result"] = "refused"
            return report
        plan = self.plan_set(voltage, current, persist)
        report["plan"] = [{k: v for k, v in p.items() if k != "msg"} for p in plan]
        bad = [p for p in plan if not p["in_range"]]
        if bad:
            report["refusals"] = [f"{p['name']}={p['value']}{p['unit']} is outside the range "
                                  f"real units accept ({R.VOLTAGE_RANGES.get(p['name'])})"
                                  for p in bad]
            report["result"] = "refused"
            return report
        if not apply:
            report["result"] = "dry-run"
            return report
        report["sent"] = [self.send_set(p) for p in plan]
        ok = all(s.get("ok") for s in report["sent"])
        report["result"] = "applied" if ok else "error"
        if verify and voltage is not None and ok and not persist:
            report["verify"] = self.verify_voltage(voltage)
            if not report["verify"]["verified"]:
                report["result"] = "applied-unverified"
        return report

    # --------------------------------------------------------- calibration
    def calibrate_current(self, test_a: float = 5.0, settle_s: float = 1.5) -> dict:
        """Which current scale does this unit obey? Needs a load that can
        draw more than test_a. For each policy: send the on-line current
        limit that would mean test_a under that policy, read back 0x76
        (limit ratio of nominal) and the measured 0x81 current. The policy
        whose 0x76 readout and measurement agree with test_a is the answer.
        Order: ratio1024 first (its raw for a small current is the
        smallest, so no policy ever asks for more than test_a x 1.5)."""
        report: dict = {"test_a": test_a, "candidates": [], "result": None}
        info = self.info()
        report["info"] = info
        if not self.nominal_a:
            report["error"] = "nominal current unknown (info 0x0001 missing) - cannot test ratio1024"
            return report
        report["nominal_a"] = self.nominal_a
        before = self.status()
        report["before"] = {"out_current_a": before.get("out_current_a"),
                            "current_limit_ratio": before.get("current_limit_ratio")}
        if (before.get("out_current_a") or 0) < test_a * 0.8:
            report["error"] = (f"load draws only {before.get('out_current_a')} A; needs > "
                               f"{test_a} A for the limit to be visible")
            return report
        cap = self.hcfg["caps"]["current_max"]
        for name in ("ratio1024", "x20", "x30"):
            sc = R.CURRENT_SCALES[name]
            raw = sc.encode(test_a, self.nominal_a)
            # what each OTHER policy would make of this raw value: never exceed the cap
            worst = max(R.CURRENT_SCALES[o].decode(raw, self.nominal_a) for o in R.CURRENT_SCALES)
            cand = {"scale": name, "raw": raw, "worst_case_a": round(worst, 2)}
            if cap and worst > cap:
                cand["skipped"] = f"worst-case {worst:.1f} A exceeds cap {cap} A"
                report["candidates"].append(cand)
                continue
            msg = CanMsg(0.0, R.request_id(CMD_SET, self.addr),
                         bytes([0x01, 0x03, 0, 0]) + raw.to_bytes(4, "big"), ext=True)
            item = {"name": "online_current", "value": test_a, "unit": "A", "raw": raw,
                    "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                    "scale": name}
            res = self.send_set(item)
            cand["ack_ok"] = res.get("ok")
            if not res.get("ok"):
                report["candidates"].append(cand)
                continue
            time.sleep(settle_s)
            snap = self.status()
            ratio = snap.get("current_limit_ratio")
            meas = snap.get("out_current_a")
            cand.update({"limit_ratio_0x76": ratio, "measured_a": meas})
            if ratio is not None:
                cand["limit_a_if_ratio1024_readout"] = round(ratio * self.nominal_a, 2)
                implied = ratio * self.nominal_a
                cand["readout_matches"] = abs(implied - test_a) <= max(0.5, test_a * 0.1)
            if meas is not None:
                cand["measurement_matches"] = abs(meas - test_a) <= max(0.5, test_a * 0.15)
            report["candidates"].append(cand)
            if cand.get("readout_matches") and cand.get("measurement_matches", True):
                report["result"] = name
                break
        if report["result"]:
            self.scale = report["result"]
            report["source"] = R.CURRENT_SCALES[report["result"]].source
            # On a 50-52 A unit ratio1024 (A x 1024/52 = x19.7) and x20 are the
            # same thing within 1.5 % - which is exactly why the sources
            # disagree. Say which policies this unit cannot tell apart.
            raw0 = R.CURRENT_SCALES[report["result"]].encode(test_a, self.nominal_a)
            report["equivalent"] = sorted(
                n for n, sc in R.CURRENT_SCALES.items()
                if abs(sc.encode(test_a, self.nominal_a) - raw0) <= max(2, raw0 * 0.05))
            if len(report["equivalent"]) > 1:
                report["note"] = (f"for a {self.nominal_a} A unit the scales "
                                  f"{report['equivalent']} agree within 5 %; "
                                  f"{report['result']} is used")
        # release the limit again: back to nominal under the found scale
        if self.scale:
            raw = R.CURRENT_SCALES[self.scale].encode(min(self.nominal_a, cap or self.nominal_a),
                                                      self.nominal_a)
            msg = CanMsg(0.0, R.request_id(CMD_SET, self.addr),
                         bytes([0x01, 0x03, 0, 0]) + raw.to_bytes(4, "big"), ext=True)
            self.send_set({"name": "online_current", "value": "release", "unit": "", "raw": raw,
                           "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                           "scale": self.scale})
        return report

    # ---------------------------------------------------------------- hold
    def hold(self, voltage: float | None, current: float | None, duration: float | None,
             status_period: float = 1.0, resend_period: float = 5.0,
             stop_event=None) -> dict:
        """Keep-alive loop: status every second, setpoints re-sent every
        five; CHANGE lines on output V/I/limit/flags; stops on error."""
        refusals = self.check_caps(voltage, current)
        stats = {"status_polls": 0, "resends": 0, "errors": 0, "changes": 0,
                 "refusals": refusals}
        if refusals:
            return stats
        plan = self.plan_set(voltage, current)
        last: dict = {}
        t0 = time.monotonic()
        next_status = t0
        next_resend = t0
        self.session.note(f"huawei hold start addr {self.addr} V={voltage} A={current}", self.chan)
        try:
            while True:
                now = time.monotonic()
                if stop_event is not None and stop_event.is_set():
                    break
                if duration and now - t0 >= duration:
                    break
                if now >= next_status:
                    next_status = now + status_period
                    snap = self.status(timeout=0.8)
                    stats["status_polls"] += 1
                    if not snap.get("complete"):
                        stats["errors"] += 1
                        if stats["errors"] >= 5:
                            raise CanError("5 status polls without a complete burst - stopping")
                    cur = {k: snap.get(k) for k in ("out_voltage_v", "out_current_a",
                                                     "current_limit_ratio", "status_flags",
                                                     "out_temp_c")}
                    for k, v in cur.items():
                        if k not in last:
                            continue
                        old = last[k]
                        if v is None or old is None:
                            continue
                        changed = (v != old) if not isinstance(v, float) else (
                            abs(v - old) >= (0.1 if k == "out_voltage_v" else
                                             0.5 if k == "out_current_a" else
                                             2.0 if k == "out_temp_c" else 0.005))
                        if changed:
                            stats["changes"] += 1
                            self.on_event(time.strftime("%H:%M:%S ") +
                                          f"CHANGE {k}: {old} -> {v}")
                    last.update({k: v for k, v in cur.items() if v is not None})
                    if stats["status_polls"] % 30 == 1:
                        self.on_event(time.strftime("%H:%M:%S ") +
                                      f"STATE V={cur['out_voltage_v']} I={cur['out_current_a']} "
                                      f"limit_ratio={cur['current_limit_ratio']} "
                                      f"T={cur['out_temp_c']} flags={cur['status_flags']}")
                if now >= next_resend:
                    next_resend = now + resend_period
                    for p in plan:
                        res = self.send_set(p)
                        stats["resends"] += 1
                        if not res.get("ok"):
                            stats["errors"] += 1
                            self.on_event(f"!! set {p['name']} failed: {res.get('ack') or res.get('error')}")
                            raise CanError(f"setpoint {p['name']} rejected / unacked - stopping")
                time.sleep(max(0.0, min(next_status, next_resend) - time.monotonic()))
        except KeyboardInterrupt:
            self.on_event("hold stopped by user - PSU reverts to its defaults in ~60 s")
        except CanError as e:
            stats["errors"] += 1
            stats["stop_reason"] = str(e)
            self.on_event(f"!! {e}")
        self.session.note(f"huawei hold stop stats={stats}", self.chan)
        return stats
