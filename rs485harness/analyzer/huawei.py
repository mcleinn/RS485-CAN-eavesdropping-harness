"""Huawei R48xx rectifier control over CAN - the logic behind `rsh huawei`.

Safety invariants (docs/CAN-PLAN.md 3.6), enforced here, not in the CLI:
- hard caps from .rsh-config.json ("huawei": {"caps": {...}}); a setpoint
  above a cap is refused, never clamped. Until caps are set, the defaults
  are 55.2 V and 0 A - i.e. no current setpoint at all.
- current setpoints need an established scale policy for THIS unit,
  identified by its serial, never by its bus address (addresses are
  renegotiated whenever a unit joins or restarts - NOTES section 8).
  `info` binds serial -> address, `calibrate-current` stores the scale
  under the serial; before a current setpoint is sent the serial on the
  target address is checked again (one 0x50 request) and a changed serial
  refuses until `info` has been rerun. A scale is never carried from one
  serial to another.
- dry run by default; `apply=True` sends; `persist=True` is a separate,
  explicit choice for the non-volatile registers.
- every transmitted frame is logged with its ack and the audit fields
  (target address or broadcast, policy/scale/cap); set values are verified
  by measurement (0x75) because they cannot be read back.
- the hold loop stops sending on Ctrl+C or any error; the PSU then reverts
  to its stored defaults by itself (~60 s). `poll_only` and `silence_after`
  turn the loop into the measurement of that fallback (Phase B tests B3.1,
  B3.4, B4, B5.6/7, B6); the numbers reported are observed on the wire or
  null, never estimated.

Config layout (.rsh-config.json "huawei"):
  caps:     {voltage_max, current_max}
  addr:     default PSU address for the CLI
  units:    {<serial_hex>: {addr, nominal_a, current_scale, seen}}
  addr_map: {"<addr>": <serial_hex>}  - serial last seen answering on addr
  legacy:   the pre-per-serial flat current_scale/nominal_a, kept for
            reference only; never applied to a serial automatically.

Time: `clock`/`sleep` are injectable (tests run the loops on a fake clock);
reply windows on the bus stay on the real clock.
"""
from __future__ import annotations

import statistics
import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..core.session import SessionWriter
from ..protocols.huawei_r48xx import regs as R
from ..protocols.huawei_r48xx.ids import (HuaweiId, CMD_DATA, CMD_INFO, CMD_ELABEL,
                                          CMD_SET, BITRATE)

DEFAULT_CAPS = {"voltage_max": 55.2, "current_max": 0.0}
VERIFY_TOL_V = 0.3
VERIFY_SETTLE_S = 2.0
OFFLINE_CURRENT_A = 0.05          # "zero" measured current
OUTPUT_PRESENT_A = 0.5            # a baseline worth calling "output present"
PERSIST_REMINDER = ("reminder: the persistent defaults must already be zero current "
                    "(B3.0: `set --current 0 --persist --apply`) before a fallback test - "
                    "the fallback goes to whatever 0x01/0x04 hold")


def huawei_config(cfg: dict) -> dict:
    """The 'huawei' block of .rsh-config.json with defaults filled in and
    the old flat current_scale/nominal_a moved to `legacy` (per-serial
    layout). `_migrated` is set when that just happened (print a note,
    then save)."""
    h = dict(cfg.get("huawei") or {})
    caps = dict(DEFAULT_CAPS)
    caps.update(h.get("caps") or {})
    h["caps"] = caps
    h.setdefault("addr", 1)
    h["units"] = dict(h.get("units") or {})
    h["addr_map"] = {str(k): v for k, v in (h.get("addr_map") or {}).items()}
    flat = {k: h.pop(k) for k in ("current_scale", "nominal_a") if k in h}
    if any(v is not None for v in flat.values()):
        h["legacy"] = {**(h.get("legacy") or {}), **{k: v for k, v in flat.items()
                                                      if v is not None}}
        h["_migrated"] = (f"huawei.current_scale/nominal_a {flat} moved to huawei.legacy: "
                          f"scales are stored per serial now (huawei.units.<serial>) - run "
                          f"`info` and `calibrate-current` again, or bind explicitly with "
                          f"`caps --serial <hex> --current-scale ... --nominal-a ...`")
    return h


def bind_unit(hcfg: dict, serial: str, addr: int, nominal_a: float | None = None,
              scale: str | None = None) -> dict | None:
    """Record serial<->addr and the unit's data. Returns the serial that
    previously answered on this address if it differs (an address change)."""
    serial = serial.upper()
    unit = hcfg["units"].setdefault(serial, {})
    unit["addr"] = addr
    if nominal_a is not None:
        unit["nominal_a"] = nominal_a
    if scale is not None:
        unit["current_scale"] = scale
    unit["seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
    prev = hcfg["addr_map"].get(str(addr))
    hcfg["addr_map"][str(addr)] = serial
    for a, s in list(hcfg["addr_map"].items()):
        if s == serial and a != str(addr):
            del hcfg["addr_map"][a]          # a serial answers on one address only
    if prev and prev != serial:
        return {"addr": addr, "was": prev, "now": serial}
    return None


def unit_on_addr(hcfg: dict, addr: int) -> tuple[str | None, dict]:
    serial = hcfg["addr_map"].get(str(addr))
    return serial, (hcfg["units"].get(serial) or {}) if serial else {}


class HuaweiClient:
    def __init__(self, link: CanLink, session: SessionWriter, chan: str = "can",
                 addr: int = 1, hcfg: dict | None = None, on_event=None,
                 target_addr: int | None = None, expect_acks: list[int] | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.link = link
        self.session = session
        self.chan = chan
        self.addr = addr                              # unit expected to answer requests
        self.target_addr = addr if target_addr is None else target_addr   # 0 = broadcast
        self.expect_acks = sorted(set(expect_acks or [addr]))
        self.hcfg = hcfg or huawei_config({})
        self.on_event = on_event or (lambda line: None)
        self.clock = clock
        self.sleep = sleep
        self.serial, unit = unit_on_addr(self.hcfg, addr)
        self.nominal_a: float | None = unit.get("nominal_a")
        self.scale: str | None = unit.get("current_scale")
        self.other_controller_frames = 0
        self.unsolicited = 0
        self.last_unsol: dict[int, tuple[dict, float]] = {}    # addr -> (decoded, clock)
        self.t_last_tx: float | None = None
        self.t_last_tx_wall: float | None = None

    @property
    def broadcast(self) -> bool:
        return self.target_addr == 0

    def target_label(self, addr: int | None = None) -> str:
        a = self.target_addr if addr is None else addr
        return "broadcast" if a == 0 else f"addr:{a}"

    # ------------------------------------------------------------ plumbing
    def _log_rx(self, msg: CanMsg) -> dict | None:
        dec = R.decode(msg, self.nominal_a)
        if dec:
            if dec["flow"] == "CTL->PSU" and not msg.echo:
                self.other_controller_frames += 1
            if dec["kind"] == "unsolicited":
                self.unsolicited += 1
                self.last_unsol[dec["addr"]] = (dec, self.clock())
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
        self.t_last_tx = self.clock()
        self.t_last_tx_wall = time.time()
        target = self.target_label(HuaweiId.unpack(msg.can_id).addr)
        log_can(self.session, self.chan, "tx", msg, decoded=R.decode(msg, self.nominal_a),
                flow="CTL->PSU", acked=acked, mode="ctl", target=target,
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
    def status(self, timeout: float = 1.0, addr: int | None = None) -> dict:
        """One 0x40 request, decoded burst of this PSU."""
        addr = self.addr if addr is None else addr
        req = R.request_frame(CMD_DATA, addr)
        if not self._send(req, note="status request"):
            return {"error": "no ACK on status request", "complete": False, "frames": 0}
        msgs = self._collect(timeout,
                             lambda d: d["kind"] == "status" and d["addr"] == addr,
                             lambda d: not d["cont"])
        burst = R.StatusBurst()
        for m in msgs:
            burst.feed(m)
        return burst.snapshot(self.nominal_a, self.scale)

    def read_serial(self, timeout: float = 1.0, addr: int | None = None) -> dict:
        """One 0x50 request, only the identity: serial and nominal current
        (no e-label). The identity check before a current setpoint."""
        addr = self.addr if addr is None else addr
        out: dict = {"addr": addr}
        if not self._send(R.request_frame(CMD_INFO, addr), note="identity request"):
            out["error"] = "no ACK on info request"
            return out
        msgs = self._collect(timeout, lambda d: d["kind"] == "info" and d["addr"] == addr,
                             lambda d: not d["cont"])
        for m in msgs:
            dec = R.decode_info_data(m.data)
            if dec.get("name") == "characteristics":
                out["nominal_current_a"] = dec["nominal_current_a"]
            elif dec.get("name") == "serial":
                out["serial_hex"] = dec["serial_hex"]
            elif dec.get("name") == "barcode":
                out["barcode"] = out.get("barcode", "") + dec["ascii"]
            elif dec.get("name") == "versions":
                out["versions"] = {k: dec[k] for k in ("hw_version", "dcdc_sw", "pfc_sw")}
            elif dec.get("name") == "slot_id":
                out["slot_id"] = dec["slot_id"]
        out["info_frames"] = len(msgs)
        if not msgs:
            out["error"] = "no info reply"
        return out

    def _bind_from_info(self, info: dict) -> None:
        serial = info.get("serial_hex")
        if not serial:
            return
        change = bind_unit(self.hcfg, serial, info["addr"], info.get("nominal_current_a"))
        info["bound"] = {"serial_hex": serial.upper(), "addr": info["addr"]}
        if change:
            info["addr_change"] = change
            self.on_event(f"ADDR-CHANGE addr {change['addr']}: serial {change['was']} -> "
                          f"{change['now']} (per-serial scale applies to the new serial only)")
            self.session.note(f"ADDR-CHANGE addr {change['addr']}: {change['was']} -> "
                              f"{change['now']}", self.chan, addr_change=change)
        if info["addr"] == self.addr:
            self.serial, unit = unit_on_addr(self.hcfg, self.addr)
            self.nominal_a = unit.get("nominal_a") or self.nominal_a
            self.scale = unit.get("current_scale")

    def info(self, timeout: float = 1.0) -> dict:
        out = self.read_serial(timeout)
        if "error" in out and out["error"].startswith("no ACK"):
            return out
        if out.get("serial_hex"):
            self._bind_from_info(out)
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

    def verify_identity(self, addrs: list[int] | None = None, timeout: float = 1.0) -> list[str]:
        """The serial answering on each address must be the one the config
        bound there. Returns refusal texts (empty = all match)."""
        refusals = []
        for a in addrs or [self.addr]:
            bound, _ = unit_on_addr(self.hcfg, a)
            got = self.read_serial(timeout, addr=a)
            serial = got.get("serial_hex")
            if not serial:
                refusals.append(f"addr {a}: no identity reply ({got.get('error', 'no serial')}) - "
                                f"refusing to send a current setpoint blind")
            elif not bound:
                refusals.append(f"addr {a}: serial {serial} answers but nothing is bound to "
                                f"this address - run `info --addr {a}` first")
            elif serial.upper() != bound.upper():
                refusals.append(f"addr {a}: serial changed {bound} -> {serial} since `info` "
                                f"(address renegotiated?) - refusing; rerun `info --addr {a}` "
                                f"and calibrate that unit if it has no scale")
        return refusals

    # ----------------------------------------------------------- setpoints
    def _unit_scales(self) -> tuple[list[str], dict]:
        """(refusals, per-addr {scale, nominal_a}) for the addresses a set
        goes to (the expected-ack list on broadcast, else the target)."""
        addrs = self.expect_acks if self.broadcast else [self.target_addr]
        refusals, per = [], {}
        for a in addrs:
            serial, unit = unit_on_addr(self.hcfg, a)
            if not serial:
                refusals.append(f"addr {a}: no serial bound to this address - run "
                                f"`rsh huawei info --addr {a}` first (scales are per serial)")
                continue
            scale, nominal = unit.get("current_scale"), unit.get("nominal_a")
            if not scale:
                refusals.append(f"addr {a} (serial {serial}): current scale not established - "
                                f"run `rsh huawei calibrate-current --addr {a}` first (or bind "
                                f"explicitly with `caps --serial {serial} --current-scale ...`)")
            elif R.CURRENT_SCALES[scale].needs_nominal and not nominal:
                refusals.append(f"addr {a} (serial {serial}): ratio1024 scale needs the nominal "
                                f"current - run `info --addr {a}`")
            per[a] = {"serial": serial, "scale": scale, "nominal_a": nominal}
        if self.broadcast and not refusals and len({(u["scale"], u["nominal_a"])
                                                    for u in per.values()}) > 1:
            refusals.append(f"broadcast current setpoint refused: the expected units do not share "
                            f"one (scale, nominal) - {per} - one raw value would mean different "
                            f"amps on each unit; use per-unit sets")
        return refusals, per

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
            r, per = self._unit_scales()
            refusals += r
            if not r and self.target_addr in per:
                self.scale, self.nominal_a = per[self.target_addr]["scale"], \
                    per[self.target_addr]["nominal_a"]
            elif not r and self.broadcast:
                u = next(iter(per.values()))
                self.scale, self.nominal_a = u["scale"], u["nominal_a"]
        return refusals

    def plan_set(self, voltage: float | None, current: float | None,
                 persist: bool = False) -> list[dict]:
        """The frames a set would send (dry run output)."""
        plan = []
        if voltage is not None:
            name = "default_voltage" if persist else "online_voltage"
            msg, raw = R.set_frame(name, voltage, self.target_addr)
            plan.append({"name": name, "value": voltage, "unit": "V", "raw": raw,
                         "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                         "target": self.target_label(),
                         "in_range": R.voltage_in_range(name, voltage)})
        if current is not None:
            name = "default_current" if persist else "online_current"
            msg, raw = R.set_frame(name, current, self.target_addr, self.nominal_a, self.scale)
            plan.append({"name": name, "value": current, "unit": "A", "raw": raw,
                         "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                         "target": self.target_label(),
                         "scale": self.scale, "nominal_a": self.nominal_a,
                         "in_range": raw <= 1250 if self.scale == "ratio1024" else True})
        return plan

    def send_set(self, item: dict, ack_timeout: float = 0.5) -> dict:
        """Send one set frame and wait for the ack of every expected unit
        (the target address, or `expect_acks` on broadcast). A missing or
        error ack from any of them is an error."""
        msg: CanMsg = item["msg"]
        want = self.expect_acks if HuaweiId.unpack(msg.can_id).addr == 0 else \
            [HuaweiId.unpack(msg.can_id).addr]
        acked = self._send(msg, note=f"set {item['name']}={item['value']}{item['unit']}",
                           scale=item.get("scale"), cap=self.hcfg["caps"], policy="manual",
                           expect_acks=want)
        res = {"name": item["name"], "value": item["value"], "raw": item["raw"],
               "id": item["id"], "hex": item["hex"], "acked": acked, "ack": None,
               "target": self.target_label(HuaweiId.unpack(msg.can_id).addr),
               "expected_acks": want, "acks": {}}
        if not acked:
            res["ok"] = False
            return res
        pending = set(want)

        def take(d):
            if d["kind"] == "ack" and d["addr"] in pending:
                pending.discard(d["addr"])
                return True
            return False
        msgs = self._collect(ack_timeout, take, lambda d: not pending)
        for m in msgs:
            a = HuaweiId.unpack(m.can_id).addr
            res["acks"][a] = R.decode_ack_data(m.data)
        res["missing_acks"] = sorted(pending)
        if msgs:
            res["ack"] = res["acks"][HuaweiId.unpack(msgs[0].can_id).addr]
        res["ok"] = not pending and all(a["ok"] for a in res["acks"].values())
        if pending:
            res["error"] = f"no ack frame from addr {sorted(pending)} within timeout"
        return res

    def verify_voltage(self, target: float, settle_s: float = VERIFY_SETTLE_S,
                       tol: float = VERIFY_TOL_V) -> dict:
        self.sleep(settle_s)
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
        report: dict = {"addr": self.addr, "target": self.target_label(),
                        "expect_acks": self.expect_acks if self.broadcast else None,
                        "apply": apply, "persist": persist,
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
        if current is not None:
            # identity before a current setpoint: the scale belongs to a serial
            report["identity"] = self.verify_identity(
                self.expect_acks if self.broadcast else [self.target_addr])
            if report["identity"]:
                report["refusals"] = report["identity"]
                report["result"] = "refused"
                return report
        report["sent"] = [self.send_set(p) for p in plan]
        ok = all(s.get("ok") for s in report["sent"])
        report["result"] = "applied" if ok else "error"
        if verify and voltage is not None and ok and not persist and not self.broadcast:
            report["verify"] = self.verify_voltage(voltage)
            if not report["verify"]["verified"]:
                report["result"] = "applied-unverified"
        return report

    # --------------------------------------------------------- calibration
    def _raw_current_item(self, raw: int, value, scale: str | None) -> dict:
        msg = CanMsg(0.0, R.request_id(CMD_SET, self.target_addr),
                     bytes([0x01, 0x03, 0, 0]) + raw.to_bytes(4, "big"), ext=True)
        return {"name": "online_current", "value": value, "unit": "A" if value != "release" else "",
                "raw": raw, "id": msg.id_str, "hex": msg.data.hex(" ").upper(), "msg": msg,
                "scale": scale}

    def calibrate_current(self, test_a: float = 5.0, settle_s: float = 1.5) -> dict:
        """Which current scale does this unit obey? Needs a load that can
        draw more than test_a. For each policy: send the on-line current
        limit that would mean test_a under that policy, read back 0x76
        (limit ratio of nominal) and the measured 0x81 current. The policy
        whose 0x76 readout and measurement agree with test_a is the answer.
        Order: ratio1024 first (its raw for a small current is the
        smallest, so no policy ever asks for more than test_a x 1.5).
        The result is stored under the unit's SERIAL."""
        report: dict = {"test_a": test_a, "candidates": [], "result": None}
        if self.broadcast:
            report["error"] = "calibration is per unit - no broadcast"
            return report
        info = self.info()
        report["info"] = info
        if not self.nominal_a:
            report["error"] = "nominal current unknown (info 0x0001 missing) - cannot test ratio1024"
            return report
        if not info.get("serial_hex"):
            report["error"] = "serial unknown (info 0x0002 missing) - a scale is bound to a serial"
            return report
        report["serial_hex"] = info["serial_hex"]
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
            res = self.send_set(self._raw_current_item(raw, test_a, name))
            cand["ack_ok"] = res.get("ok")
            if not res.get("ok"):
                report["candidates"].append(cand)
                continue
            self.sleep(settle_s)
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
            bind_unit(self.hcfg, report["serial_hex"], self.addr, self.nominal_a, self.scale)
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
            self.send_set(self._raw_current_item(raw, "release", self.scale))
        return report

    # ---------------------------------------------------------------- hold
    def _online_tracker(self):
        """Closure: feed status snapshots, returns the first time the unit
        shows the fallback (unsolicited output_disabled, or measured 0x81
        current at zero after output had been present)."""
        st = {"had_output": False, "first_offline": None}

        def feed(snap: dict, now: float) -> str | None:
            i = snap.get("out_current_a")
            uns = self.last_unsol.get(self.addr)
            if uns and uns[0].get("load_raw", 0) > 0 or (i is not None and i >= OUTPUT_PRESENT_A):
                st["had_output"] = True
            if st["first_offline"] is not None:
                return None
            why = None
            if uns and uns[0].get("output_disabled"):
                why = "unsolicited byte 3 = output disabled"
            elif st["had_output"] and i is not None and i <= OFFLINE_CURRENT_A:
                why = "measured 0x81 current at zero after output had been present"
            if why:
                st["first_offline"] = now
            return why
        feed.state = st
        return feed

    def hold(self, voltage: float | None, current: float | None, duration: float | None,
             status_period: float = 1.0, resend_period: float = 5.0,
             stop_event=None, poll_only: bool = False, silence_after: float | None = None,
             observe_s: float = 120.0, tap_link: CanLink | None = None,
             tap_chan: str = "tap") -> dict:
        """Keep-alive loop: status every second, setpoints re-sent every
        five; CHANGE lines on output V/I/limit/flags; stops on error.
        poll_only: status requests only, never a set frame (B3.4) - reports
        `first_offline_s`. silence_after: after that many seconds stop
        transmitting altogether and only listen for `observe_s` (on the
        tap link when given) - reports the fallback timing (B3.1/B4/B6)."""
        if poll_only and (voltage is not None or current is not None):
            return {"refusals": ["poll-only hold takes no setpoints"], "status_polls": 0}
        refusals = [] if poll_only else self.check_caps(voltage, current)
        stats: dict = {"status_polls": 0, "resends": 0, "errors": 0, "changes": 0,
                       "refusals": refusals, "poll_only": poll_only, "addr": self.addr,
                       "target": self.target_label(), "first_offline_s": None}
        if refusals:
            return stats
        if silence_after is not None:
            stats["reminder"] = PERSIST_REMINDER
        plan = [] if poll_only else self.plan_set(voltage, current)
        if current is not None and not poll_only:
            ident = self.verify_identity(self.expect_acks if self.broadcast else [self.target_addr])
            if ident:
                stats["refusals"] = ident
                return stats
        tracker = self._online_tracker()
        last: dict = {}
        t0 = self.clock()
        next_status = t0
        next_resend = t0
        self.session.note(f"huawei hold start addr {self.addr} target {self.target_label()} "
                          f"V={voltage} A={current} poll_only={poll_only} "
                          f"silence_after={silence_after}", self.chan)
        silenced = False
        try:
            while True:
                now = self.clock()
                if stop_event is not None and stop_event.is_set():
                    break
                if duration and now - t0 >= duration:
                    break
                if silence_after is not None and now - t0 >= silence_after:
                    silenced = True
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
                    why = tracker(snap, self.clock())
                    if why:
                        stats["first_offline_s"] = round(tracker.state["first_offline"] - t0, 3)
                        stats["offline_evidence"] = why
                        self.on_event(time.strftime("%H:%M:%S ") +
                                      f"OFFLINE-TRANSITION t={stats['first_offline_s']} ({why})")
                        self.session.note(f"OFFLINE-TRANSITION t={stats['first_offline_s']} "
                                          f"{why}", self.chan, first_offline_s=stats["first_offline_s"])
                    if stats["status_polls"] % 30 == 1:
                        self.on_event(time.strftime("%H:%M:%S ") +
                                      f"STATE V={cur['out_voltage_v']} I={cur['out_current_a']} "
                                      f"limit_ratio={cur['current_limit_ratio']} "
                                      f"T={cur['out_temp_c']} flags={cur['status_flags']}")
                if plan and now >= next_resend:
                    next_resend = now + resend_period
                    for p in plan:
                        res = self.send_set(p)
                        stats["resends"] += 1
                        if not res.get("ok"):
                            stats["errors"] += 1
                            self.on_event(f"!! set {p['name']} failed: {res.get('ack') or res.get('error')}")
                            raise CanError(f"setpoint {p['name']} rejected / unacked - stopping")
                wake = next_status if not plan else min(next_status, next_resend)
                self.sleep(max(0.0, wake - self.clock()))
        except KeyboardInterrupt:
            self.on_event("hold stopped by user - PSU reverts to its defaults in ~60 s")
        except CanError as e:
            stats["errors"] += 1
            stats["stop_reason"] = str(e)
            self.on_event(f"!! {e}")
        if silenced and not stats.get("stop_reason"):
            stats["silence"] = self.observe_silence(observe_s, tap_link, tap_chan, stop_event)
        self.session.note(f"huawei hold stop stats={stats}", self.chan)
        self.session.summary({"sub": "hold", **stats}, self.chan)
        return stats

    def observe_silence(self, observe_s: float, tap_link: CanLink | None = None,
                        tap_chan: str = "tap", stop_event=None) -> dict:
        """Receive-only after the last transmitted frame: the fallback as
        seen on the wire. Measured on the tap link when given (the
        controller link still ACKs and is only drained into the log), on
        the controller link otherwise. Times are relative to the last
        transmitted frame; a value that was not observed stays null."""
        t_ref = self.t_last_tx if self.t_last_tx is not None else self.clock()
        src = tap_link if tap_link is not None else self.link
        src_chan = tap_chan if tap_link is not None else self.chan
        rep: dict = {"measured_on": src_chan, "observe_s": observe_s,
                     "t_last_tx_wall": self.t_last_tx_wall,
                     "t_first_output_disabled": None, "t_zero_current": None,
                     "residual_current_a": None, "residual_load_raw": None,
                     "unsolicited_period_s": None, "frames": 0, "per_addr": {}}
        uns0 = self.last_unsol.get(self.addr)
        load_before = uns0[0].get("load_raw") if uns0 else None
        rep["load_raw_before_silence"] = load_before
        if load_before == 0:
            rep["note"] = "load was already zero when transmission stopped"
        self.on_event(f"SILENCE: transmission stopped, listening {observe_s:.0f} s on {src_chan}")
        self.session.note(f"SILENCE start (last tx at wall {self.t_last_tx_wall}), observing on "
                          f"{src_chan} for {observe_s} s", self.chan, mode="silence")
        if tap_link is not None:
            # the tap buffered the whole hold phase (the controller link
            # already logged it); the observation starts at the last tx
            tap_link.flush_input()
        unsol_t: dict[int, list[float]] = {}
        t_start = self.clock()
        while self.clock() - t_start < observe_s:
            if stop_event is not None and stop_event.is_set():
                break
            if tap_link is not None:                      # drain + log, never measure
                m = self.link.recv(timeout=0)
                if m is not None and not m.echo:
                    self._log_rx(m)
            msg = src.recv(timeout=0.005)
            if msg is None:
                self.sleep(0.05)
                continue
            if msg.echo:
                continue
            now = self.clock()
            t_rel = round(now - t_ref, 3)
            dec = R.decode(msg, self.nominal_a)
            log_can(self.session, src_chan, "rx", msg, decoded=dec,
                    flow=dec["flow"] if dec else None, mode="silence", t_since_last_tx=t_rel)
            rep["frames"] += 1
            if not dec:
                continue
            a = dec["addr"]
            pa = rep["per_addr"].setdefault(a, {"t_first_output_disabled": None,
                                                "t_zero_current": None, "last_load_raw": None,
                                                "unsolicited": 0, "status_frames": 0})
            if dec["kind"] == "unsolicited":
                pa["unsolicited"] += 1
                unsol_t.setdefault(a, []).append(now)
                pa["last_load_raw"] = dec.get("load_raw")
                if dec.get("output_disabled") and pa["t_first_output_disabled"] is None:
                    pa["t_first_output_disabled"] = t_rel
                    self.on_event(f"  t={t_rel} s: addr {a} unsolicited says output disabled")
                if dec.get("load_raw") == 0 and pa["t_zero_current"] is None \
                        and (load_before or a != self.addr):
                    pa["t_zero_current"] = t_rel
                    self.on_event(f"  t={t_rel} s: addr {a} unsolicited load = 0")
            elif dec["kind"] == "status" and dec.get("name") == "out_current_a":
                pa["status_frames"] += 1          # another controller still polls
                if dec["value"] <= OFFLINE_CURRENT_A and pa["t_zero_current"] is None \
                        and load_before:
                    pa["t_zero_current"] = t_rel
                    self.on_event(f"  t={t_rel} s: addr {a} 0x81 = {dec['value']} A "
                                  f"(somebody else is still polling)")
            elif dec["kind"] in ("status_request", "set") and not msg.echo:
                self.other_controller_frames += 1
        mine = rep["per_addr"].get(self.addr)
        if mine:
            rep["t_first_output_disabled"] = mine["t_first_output_disabled"]
            rep["t_zero_current"] = mine["t_zero_current"]
            rep["residual_load_raw"] = mine["last_load_raw"]
            if mine["last_load_raw"] is not None and self.nominal_a:
                rep["residual_current_a"] = round(mine["last_load_raw"] / 1024 * self.nominal_a, 2)
        ts = unsol_t.get(self.addr) or []
        if len(ts) >= 3:
            rep["unsolicited_period_s"] = round(statistics.median(
                b - a for a, b in zip(ts, ts[1:])), 3)
        rep["other_controller_frames"] = self.other_controller_frames
        self.on_event(f"SILENCE result: output-disabled at {rep['t_first_output_disabled']} s, "
                      f"zero current at {rep['t_zero_current']} s, residual "
                      f"{rep['residual_current_a']} A, unsolicited period "
                      f"{rep['unsolicited_period_s']} s ({rep['frames']} frames on {src_chan})")
        self.session.note(f"SILENCE result {rep}", self.chan, mode="silence")
        return rep
