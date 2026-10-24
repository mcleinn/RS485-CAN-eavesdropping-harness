"""Huawei gateway: the harness sits between a charger's add-on control board
("front-end") and the rectifier module, as the ONLY master the module
hears, while the front-end keeps talking to what it believes is the
module (docs/CHARGER-HUAWEI-UNITS.md 3.3, 4).

Wiring: the front-end's CAN pair is cut from the module and goes to one
adapter channel (`fe_link`), the module to the other (`psu_link`). Both
channels are bus ends: termination closed on both.

Rules:
- Module side: our setpoints only (same caps / per-serial scale / identity
  checks as `rsh huawei set`), re-sent every `resend_period`; status polls
  of our own only while the front-end is not polling. Every frame we send
  is logged with its ack and audit fields, policy "gateway".
- Front-end side: its status / info / e-label / get REQUESTS are forwarded
  to the module and the module's replies, heartbeats and address frames
  are forwarded back, so its screen shows real values. Its WRITES (0x80
  set registers) are never forwarded; each gets a synthesized ack so the
  board stays content; the values it wanted are recorded (`fe_setpoints`)
  and logged as BLOCKED. Its 0x81 config broadcasts are dropped without
  ack (the module was never seen to ack them either).
- The module's acks are consumed on the module side, never forwarded (they
  answer our writes, not the board's).
- Nothing is derived from stale data: our setpoints are what the operator
  gave, the board's wishes are information only (no "mirror" mode in v1).
- Address rewrite (`fe_addr`): a board built for a single module addresses
  it as 1; when the module is address 2 on a shared module bus, the gateway
  rewrites the address field of every identifier in flight - board frames
  for `fe_addr` go to the module as `addr`, module frames from `addr` reach
  the board as `fe_addr`. One board per channel is the precondition: the
  rewrite needs to know which board a frame came from.
"""
from __future__ import annotations

import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..core.session import SessionWriter
from ..protocols.huawei_r48xx import regs as R
from ..protocols.huawei_r48xx.ids import HuaweiId, CMD_DATA, CMD_SET, CMD_CFG, request_id, reply_id
from .huawei import HuaweiClient

FORWARD_FROM_PSU = ("status", "info", "elabel", "get_reply", "unsolicited", "addr_negotiation")
FORWARD_REQUESTS = ("status_request", "info_request", "elabel_request", "get")
ACK_TIMEOUT_S = 0.5


class HuaweiGateway:
    def __init__(self, psu_link: CanLink, fe_link: CanLink, session: SessionWriter,
                 hcfg: dict, addr: int = 1, voltage: float | None = None,
                 current: float | None = None, resend_period: float = 5.0,
                 status_period: float = 1.0, chan_psu: str = "psu", chan_fe: str = "fe",
                 on_event=None, clock=time.monotonic, sleep=time.sleep,
                 fe_addr: int | None = None):
        for name, link in (("module", psu_link), ("front-end", fe_link)):
            if getattr(link, "listen_only", False):
                raise CanError(f"gateway: the {name} link is listen-only - a gateway must ACK "
                               f"and transmit on both sides")
        self.psu, self.fe = psu_link, fe_link
        self.session = session
        self.addr = addr
        self.fe_addr = addr if fe_addr is None else fe_addr      # what the board calls the module
        self.voltage, self.current = voltage, current
        self.resend_period, self.status_period = resend_period, status_period
        self.chan_psu, self.chan_fe = chan_psu, chan_fe
        self.on_event = on_event or (lambda line: None)
        self.clock, self.sleep = clock, sleep
        self.client = HuaweiClient(psu_link, session, chan_psu, addr, hcfg, self.on_event,
                                   clock=clock, sleep=sleep)
        self.plan: list[dict] = []
        self.refusals: list[str] = []
        self.fe_setpoints: dict[str, dict] = {}
        self.pending_ack: dict[int, tuple[str, float]] = {}      # reg -> (name, t_sent)
        self.stats = {"our_sends": 0, "our_acks_ok": 0, "our_acks_error": 0,
                      "our_acks_missing": 0, "our_status_polls": 0, "psu_forwarded": 0,
                      "psu_dropped": 0, "fe_requests_forwarded": 0, "fe_writes_blocked": 0,
                      "fe_cfg_blocked": 0, "fe_dropped": 0, "acks_synthesized": 0,
                      "fe_undelivered": 0, "psu_undelivered": 0, "changes": 0,
                      "first_offline_s": None, "addr_rewritten": 0}
        self._burst = R.StatusBurst()
        self._last: dict = {}
        self._tracker = self.client._online_tracker()
        self._t0 = 0.0
        self._last_fe_request = -1e9
        self.stop_reason: str | None = None

    def _rewrite(self, msg: CanMsg, from_addr: int, to_addr: int) -> CanMsg:
        """Copy of msg with the identifier's address field changed (only when
        it equals from_addr and a rewrite is configured)."""
        out = CanMsg(msg.ts, msg.can_id, bytes(msg.data), msg.ext, msg.rtr)
        if self.fe_addr != self.addr:
            hid = HuaweiId.unpack(msg.can_id)
            if hid.addr == from_addr:
                out.can_id = HuaweiId(hid.proto, to_addr, hid.cmd, hid.to_psu, hid.cont,
                                      hid.group, hid.sw_addr).pack()
                self.stats["addr_rewritten"] += 1
        return out

    # ----------------------------------------------------------- prepare
    def prepare(self) -> list[str]:
        """Caps, per-serial scale, identity (one info request on the module
        side, before any relaying) and the frames we will send."""
        if self.voltage is None and self.current is None:
            self.refusals = []
            return self.refusals
        self.refusals = self.client.check_caps(self.voltage, self.current)
        if self.refusals:
            return self.refusals
        if self.current is not None:
            self.refusals = self.client.verify_identity([self.addr])
            if self.refusals:
                return self.refusals
        self.plan = self.client.plan_set(self.voltage, self.current)
        bad = [p for p in self.plan if not p["in_range"]]
        if bad:
            self.refusals = [f"{p['name']}={p['value']}{p['unit']} is outside the range real "
                             f"units accept ({R.VOLTAGE_RANGES.get(p['name'])})" for p in bad]
            self.plan = []
        return self.refusals

    # ---------------------------------------------------------------- io
    def _send(self, link: CanLink, chan: str, msg: CanMsg, flow: str, **audit) -> bool:
        acked, err = True, None
        try:
            link.send(msg, timeout=0.5)
        except CanTxTimeout as e:
            acked, err = False, str(e)
        log_can(self.session, chan, "tx", msg, decoded=R.decode(msg, self.client.nominal_a),
                flow=flow, acked=acked, mode="gateway", errors=[err] if err else None, **audit)
        return acked

    def _our_resend(self) -> None:
        for p in self.plan:
            msg: CanMsg = p["msg"]
            ok = self._send(self.psu, self.chan_psu, msg, "CTL->PSU",
                            note=f"gateway set {p['name']}={p['value']}{p['unit']}",
                            scale=p.get("scale"), cap=self.client.hcfg["caps"], policy="gateway",
                            target=self.client.target_label())
            self.stats["our_sends"] += 1
            if not ok:
                self.stats["psu_undelivered"] += 1
                self.on_event(f"!! module did not ACK our {p['name']} write")
                continue
            self.pending_ack[bytes(msg.data)[1]] = (p["name"], self.clock())

    def _our_status_poll(self) -> None:
        if self._send(self.psu, self.chan_psu, R.request_frame(CMD_DATA, self.addr), "CTL->PSU",
                      note="gateway status request (front-end quiet)", policy="gateway"):
            self.stats["our_status_polls"] += 1
        else:
            self.stats["psu_undelivered"] += 1

    def _snapshot(self, snap: dict) -> None:
        cur = {k: snap.get(k) for k in ("out_voltage_v", "out_current_a", "current_limit_ratio",
                                         "status_flags", "out_temp_c")}
        for k, v in cur.items():
            old = self._last.get(k)
            if v is None or old is None:
                continue
            changed = (v != old) if not isinstance(v, float) else (
                abs(v - old) >= (0.1 if k == "out_voltage_v" else 0.5 if k == "out_current_a"
                                 else 2.0 if k == "out_temp_c" else 0.005))
            if changed:
                self.stats["changes"] += 1
                self.on_event(time.strftime("%H:%M:%S ") + f"CHANGE {k}: {old} -> {v}")
        self._last.update({k: v for k, v in cur.items() if v is not None})
        why = self._tracker(snap, self.clock())
        if why:
            self.stats["first_offline_s"] = round(self._tracker.state["first_offline"] - self._t0, 3)
            self.on_event(f"OFFLINE-TRANSITION t={self.stats['first_offline_s']} ({why})")

    def _on_psu_frame(self, msg: CanMsg) -> None:
        dec = R.decode(msg, self.client.nominal_a)
        flow = dec["flow"] if dec else None
        if dec and dec["kind"] == "ack":
            log_can(self.session, self.chan_psu, "rx", msg, decoded=dec, flow=flow, mode="gateway",
                    note="ack of our write (consumed, not forwarded)")
            reg = bytes(msg.data)[1]
            if reg in self.pending_ack:
                name, _ = self.pending_ack.pop(reg)
                if dec["ok"]:
                    self.stats["our_acks_ok"] += 1
                else:
                    self.stats["our_acks_error"] += 1
                    self.on_event(f"!! module REJECTED our {name} write (0x21)")
            return
        if dec and dec["kind"] in FORWARD_FROM_PSU:
            log_can(self.session, self.chan_psu, "rx", msg, decoded=dec, flow=flow, mode="gateway")
            fwd = self._rewrite(msg, self.addr, self.fe_addr)
            if self._send(self.fe, self.chan_fe, fwd, flow, forwarded=True):
                self.stats["psu_forwarded"] += 1
            else:
                self.stats["fe_undelivered"] += 1
            if dec["kind"] == "status" and dec["addr"] == self.addr:
                if self._burst.feed(msg) and self._burst.complete:
                    self._snapshot(self._burst.snapshot(self.client.nominal_a, self.client.scale))
                    self._burst = R.StatusBurst()
            return
        log_can(self.session, self.chan_psu, "rx", msg, decoded=dec, flow=flow, mode="gateway",
                note="dropped")
        self.stats["psu_dropped"] += 1

    def _on_fe_frame(self, msg: CanMsg) -> None:
        dec = R.decode(msg, self.client.nominal_a)
        flow = dec["flow"] if dec else None
        if not dec or flow != "CTL->PSU":
            log_can(self.session, self.chan_fe, "rx", msg, decoded=dec, flow=flow, mode="gateway",
                    note="dropped")
            self.stats["fe_dropped"] += 1
            return
        kind = dec["kind"]
        hid = HuaweiId.unpack(msg.can_id)
        if kind in FORWARD_REQUESTS:
            log_can(self.session, self.chan_fe, "rx", msg, decoded=dec, flow=flow, mode="gateway")
            self._last_fe_request = self.clock()
            fwd = self._rewrite(msg, self.fe_addr, self.addr)
            if self._send(self.psu, self.chan_psu, fwd, flow, forwarded=True,
                          note="front-end request"):
                self.stats["fe_requests_forwarded"] += 1
            else:
                self.stats["psu_undelivered"] += 1
            return
        if kind == "set" and hid.cmd == CMD_SET:
            name = dec.get("name", "?")
            wanted = {"raw": dec.get("raw"), "t": round(self.clock() - self._t0, 3)}
            if "value_v" in dec:
                wanted["value_v"] = dec["value_v"]
            self.fe_setpoints[name] = wanted
            self.stats["fe_writes_blocked"] += 1
            log_can(self.session, self.chan_fe, "rx", msg, decoded=dec, flow=flow, mode="gateway",
                    blocked=True, note=f"BLOCKED front-end write {name} (not forwarded)")
            ack = CanMsg(0.0, reply_id(CMD_SET, hid.addr or self.fe_addr, False),
                         bytes([0x01]) + bytes(msg.data)[1:8].ljust(7, b"\0"), ext=True)
            if self._send(self.fe, self.chan_fe, ack, "PSU->CTL", synthesized=True,
                          note=f"synthesized ack for {name}"):
                self.stats["acks_synthesized"] += 1
            else:
                self.stats["fe_undelivered"] += 1
            return
        if kind == "set" and hid.cmd == CMD_CFG:
            log_can(self.session, self.chan_fe, "rx", msg, decoded=dec, flow=flow, mode="gateway",
                    blocked=True, note="BLOCKED front-end config broadcast (no ack)")
            self.stats["fe_cfg_blocked"] += 1
            return
        log_can(self.session, self.chan_fe, "rx", msg, decoded=dec, flow=flow, mode="gateway",
                note="dropped")
        self.stats["fe_dropped"] += 1

    # --------------------------------------------------------------- run
    def run(self, duration: float | None = None, stop_event=None) -> dict:
        if self.refusals:
            self.stats["refusals"] = self.refusals
            return self.stats
        self._t0 = self.clock()
        next_resend = self._t0
        next_status = self._t0 + self.status_period
        self.session.note(f"huawei gateway start: module on {self.psu.name}, front-end on "
                          f"{self.fe.name}, addr {self.addr}, V={self.voltage} A={self.current}, "
                          f"resend {self.resend_period}s", self.chan_psu)
        try:
            self._our_status_poll()               # baseline before our first setpoint
            while True:
                now = self.clock()
                if stop_event is not None and stop_event.is_set():
                    break
                if duration and now - self._t0 >= duration:
                    break
                if self.plan and now >= next_resend:
                    next_resend = now + self.resend_period
                    self._our_resend()
                if self.status_period and now >= next_status:
                    next_status = now + self.status_period
                    if now - self._last_fe_request > 2 * self.status_period:
                        self._our_status_poll()
                for reg, (name, t) in list(self.pending_ack.items()):
                    if now - t > ACK_TIMEOUT_S:
                        del self.pending_ack[reg]
                        self.stats["our_acks_missing"] += 1
                        self.on_event(f"!! no ack from the module for our {name} write")
                busy = False
                for _ in range(32):
                    m = self.psu.recv(timeout=0)
                    if m is None:
                        break
                    if not m.echo and not m.err:
                        self._on_psu_frame(m)
                        busy = True
                    elif m.err:
                        log_can(self.session, self.chan_psu, "rx", m, mode="gateway")
                for _ in range(32):
                    m = self.fe.recv(timeout=0)
                    if m is None:
                        break
                    if not m.echo and not m.err:
                        self._on_fe_frame(m)
                        busy = True
                    elif m.err:
                        log_can(self.session, self.chan_fe, "rx", m, mode="gateway")
                if not busy:
                    self.sleep(0.002)
        except KeyboardInterrupt:
            self.on_event("gateway stopped by user - the module reverts to its defaults in ~60 s; "
                          "the front-end now talks to nobody")
        except CanError as e:
            self.stop_reason = str(e)
            self.on_event(f"!! {e}")
        self.stats["fe_setpoints"] = self.fe_setpoints
        self.stats["stop_reason"] = self.stop_reason
        self.session.note(f"huawei gateway stop stats={self.stats}", self.chan_psu)
        self.session.summary({"sub": "gateway", "addr": self.addr, "voltage": self.voltage,
                              "current": self.current, **self.stats}, self.chan_psu)
        return self.stats
