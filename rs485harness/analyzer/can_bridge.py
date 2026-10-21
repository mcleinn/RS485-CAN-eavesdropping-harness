"""CAN gateway: relay between two CAN channels (two terminated segments)
with optional policy on the battery's 0x351 limits - the CAN twin of
analyzer/bridge.py, with the same invariants:

- store-and-forward relay of EVERY frame in both directions (about 1 ms on
  a 1 s broadcast cadence); error frames are logged, never forwarded;
- modify mode touches 0x351 only, in the BMS->INV direction only; CVL/CCL
  may only go DOWN (never above what the BMS sent, never below 0), DCL/DVL
  and every other identifier (0x359/0x35A/0x35C ...) are locked;
- communication loss (no 0x351 for loss_timeout_s) resets the policy; the
  gateway never synthesizes a frame, so no permissive state can outlive
  real data;
- a modified frame gives two records: the original (dir=rx, audit) and the
  bytes actually sent (dir=tx, modified=true); unmodified frames one
  record (dir=fwd) with the audit;
- policy exception -> the ORIGINAL frame is forwarded and the exception
  logged; a frame the relay cannot deliver (no ACK on the far side) is
  logged as such and the traffic continues.

Also here: `run_can_selftest` (channel 0 <-> channel 1 on one terminated
bus: frames cross, ACKs happen, listen-only really does not ACK).
"""
from __future__ import annotations

import threading
import time
import traceback

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..core.session import SessionWriter, start_marker_reader, MARKER_LEGEND
from ..protocols.pylontech_can import frames as PF
from .can_battery import decode_any
from .policy import PayloadPolicy, PolicyContext, make_policy  # noqa: F401 (make_policy re-exported)
from .timeline import LiveState

ROLE_FLOW = {"inverter": "INV->BMS", "battery": "BMS->INV", "psu": "PSU->CTL",
             "controller": "CTL->PSU"}


class CanPolicyEngine:
    """Applies a PayloadPolicy (same classes as the RS485 engine: cap, ...)
    to 0x351 frames. Fields: cvl/ccl may shrink, dcl/dvl locked."""

    def __init__(self, policy: PayloadPolicy, loss_timeout_s: float = 10.0):
        self.policy = policy
        self.loss_timeout_s = loss_timeout_s
        self.last_time: float | None = None
        self.stats = {"seen": 0, "modified": 0, "resets": 0, "skipped": 0, "clamped": 0}

    def reset(self, reason: str) -> None:
        self.policy.reset(reason)
        self.last_time = None
        self.stats["resets"] += 1

    def process(self, msg: CanMsg, dec: dict | None, now: float | None = None,
                requests: dict | None = None) -> tuple[bytes | None, dict]:
        now = time.perf_counter() if now is None else now
        audit: dict = {"policy": self.policy.describe(), "id": msg.id_str}
        if not dec or dec.get("name") != "limits" or dec.get("cvl_v") is None \
                or dec.get("ccl_a") is None:
            audit["skipped"] = "not a decodable 0x351"
            self.stats["skipped"] += 1
            return None, audit
        dt = None
        if self.last_time is not None:
            dt = now - self.last_time
            if dt > self.loss_timeout_s:
                self.reset(f"comm-loss {dt:.1f}s")
                audit["reset"] = f"policy reset after {dt:.1f}s without a 0x351"
                dt = None
        self.last_time = now
        self.stats["seen"] += 1
        fields = {"cvl": dec["cvl_v"], "ccl": dec["ccl_a"], "dcl": dec.get("dcl_a"),
                  "dvl": dec.get("dvl_v")}
        ctx = PolicyContext(now=now, wall=time.time(), request_cid2=0x351, fields=dict(fields),
                            status_bits=dict(requests or {}), info=msg.data.hex().upper(), dt=dt)
        proposed = self.policy.apply(ctx) or {}
        overrides: dict = {}
        notes: list[str] = []
        for name, value in proposed.items():
            if name not in ("cvl", "ccl"):
                notes.append(f"ignored '{name}' (locked/unknown)")
                continue
            orig = fields[name]
            value = float(value)
            if value > orig:
                notes.append(f"clamped {name} {value:g}->{orig:g} (must be <= BMS)")
                value = orig
                self.stats["clamped"] += 1
            if value < 0:
                value = 0.0
            if abs(value - orig) < 0.05:
                continue
            overrides[name] = value
        audit["original"] = fields
        audit["notes"] = notes
        if not overrides:
            audit["modified"] = {}
            return None, audit
        data = bytearray(msg.data)
        if "cvl" in overrides:
            data[0:2] = int(round(overrides["cvl"] * 10)).to_bytes(2, "little")
        if "ccl" in overrides:
            data[2:4] = int(round(overrides["ccl"] * 10)).to_bytes(2, "little", signed=True)
        check = PF.decode(CanMsg(0.0, msg.can_id, bytes(data), msg.ext))
        for name, value in overrides.items():
            got = check["cvl_v"] if name == "cvl" else check["ccl_a"]
            if abs(got - value) > 0.11:
                raise RuntimeError(f"rebuild mismatch for {name}: {got} != {value}")
        audit["modified"] = overrides
        self.stats["modified"] += 1
        return bytes(data), audit


class CanBridge:
    def __init__(self, link_a: CanLink, link_b: CanLink, session: SessionWriter,
                 chan_a: str = "ch1", chan_b: str = "ch2", engine: CanPolicyEngine | None = None,
                 on_event=None, role_a: str = "inverter", role_b: str = "battery",
                 markers: bool = True, quiet: bool = False, heartbeat_s: float = 30.0,
                 gap_note_s: float = 3.0):
        self.ends = {chan_a: link_a, chan_b: link_b}
        self.other = {chan_a: chan_b, chan_b: chan_a}
        self.roles = {chan_a: role_a, chan_b: role_b}
        self.session = session
        self.engine = engine
        self.mode = "modify" if engine is not None else "transparent"
        self.markers, self.quiet = markers, quiet
        self.live = LiveState(heartbeat_s=heartbeat_s)
        self.gap_note_s = gap_note_s
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats = {chan: {"frames": 0, "error_frames": 0, "rewritten": 0, "undelivered": 0,
                             "errors": 0, "gaps": 0} for chan in self.ends}
        self._last_frame_t: dict[str, float] = {}
        self._requests: dict = {}
        for link in self.ends.values():
            if link.listen_only:
                raise CanError(f"{link.name} is listen-only - a gateway must ACK on both "
                               f"segments; open both channels in normal mode")

    def _flow(self, src_chan: str) -> str:
        return ROLE_FLOW.get(self.roles[src_chan], f"{src_chan}->{self.other[src_chan]}")

    def _note_gap(self, src_chan: str, now: float) -> None:
        last = self._last_frame_t.get(src_chan)
        if last is not None and now - last > self.gap_note_s:
            self.stats[src_chan]["gaps"] += 1
            self.session.note(f"gap {now - last:.2f}s with no frame from {self.roles[src_chan]} "
                              f"({self._flow(src_chan)})", src_chan, flow=self._flow(src_chan),
                              mode=self.mode)
        self._last_frame_t[src_chan] = now

    def _pump(self, src_chan: str) -> None:
        while not self.stop_event.is_set():
            try:
                self._pump_inner(src_chan)
                return
            except Exception as e:
                self.stats[src_chan]["errors"] += 1
                msg = f"CAN error on {src_chan} ({self.ends[src_chan].name}): {e!r}"
                self.on_event("!! " + msg)
                self.session.note(msg, src_chan, mode=self.mode, serial_error=True)
                self.stop_event.wait(1.0)

    def _pump_inner(self, src_chan: str) -> None:
        src, dst = self.ends[src_chan], self.ends[self.other[src_chan]]
        flow = self._flow(src_chan)
        bms_side = self.roles[src_chan] == "battery"
        while not self.stop_event.is_set():
            msg = src.recv(timeout=0.05)
            if msg is None or msg.echo:
                continue
            dec = decode_any(msg)
            if msg.err:
                self.stats[src_chan]["error_frames"] += 1
                log_can(self.session, src_chan, "rx", msg, decoded=None, flow=flow, mode=self.mode)
                self.on_event(f"[{src_chan}] {msg}")
                continue
            self._note_gap(src_chan, msg.ts)
            self.stats[src_chan]["frames"] += 1
            if dec and dec.get("name") == "requests":
                self._requests = {k: v for k, v in dec.items() if isinstance(v, bool)}
            out, audit = None, None
            if self.engine is not None and bms_side:
                try:
                    out, audit = self.engine.process(msg, dec, requests=self._requests)
                except Exception as e:      # fail-safe: forward the original
                    out = None
                    self.session.note(f"policy exception -> forwarded ORIGINAL frame: {e!r}\n"
                                      f"{traceback.format_exc()}", src_chan, flow=flow,
                                      mode=self.mode, failsafe=True)
                    self.on_event(f"[{flow}] !! policy exception, original forwarded: {e!r}")
            fwd = CanMsg(msg.ts, msg.can_id, out if out is not None else msg.data, msg.ext,
                         msg.rtr, channel=dst.channel)
            delivered = True
            try:
                dst.send(fwd, timeout=0.3)
            except CanTxTimeout:
                delivered = False
                self.stats[src_chan]["undelivered"] += 1
            label = (dec.get("name") or dec.get("kind")) if dec else "?"
            if out is not None and out != msg.data:
                self.stats[src_chan]["rewritten"] += 1
                log_can(self.session, src_chan, "rx", msg, decoded=dec, flow=flow, mode=self.mode,
                        audit=audit, note="ORIGINAL (modified below)")
                mdec = decode_any(fwd)
                log_can(self.session, src_chan, "tx", fwd, decoded=mdec, flow=flow, mode=self.mode,
                        modified=True, acked=delivered,
                        note="MODIFIED: " + ", ".join(f"{k}={v:g}" for k, v in audit["modified"].items()))
                self.on_event(f"[{flow}] {msg}  => MODIFIED: "
                              + ", ".join(f"{k} {audit['original'][k]:g}->{v:g}"
                                          for k, v in audit["modified"].items()))
                dec = mdec
            else:
                log_can(self.session, src_chan, "fwd", msg, decoded=dec, flow=flow, mode=self.mode,
                        audit=audit, acked=delivered)
                if not self.quiet:
                    self.on_event(f"[{flow}] {msg}  {label}" + ("" if delivered else "  (NO ACK on far side)"))
            if not delivered and self.stats[src_chan]["undelivered"] in (1, 10, 100):
                self.on_event(f"!! {src_chan}->{self.other[src_chan]}: frame not acknowledged on "
                              f"{dst.name} (#{self.stats[src_chan]['undelivered']}) - far segment "
                              f"empty, wrong bitrate or unterminated")
            for line in self.live.feed_can(dec, time.monotonic()):
                self.on_event(time.strftime("%H:%M:%S ") + line)

    def run(self, duration: float | None = None) -> dict:
        ends_desc = ", ".join(f"{chan}={link.describe()}({self.roles[chan]})"
                              for chan, link in self.ends.items())
        pol = f" policy={self.engine.policy.describe()}" if self.engine else ""
        self.session.note(f"can-bridge start {ends_desc} mode={self.mode}{pol}", mode=self.mode)
        if start_marker_reader(self.session, self.on_event, self.markers):
            self.on_event(MARKER_LEGEND)
        threads = [threading.Thread(target=self._pump, args=(chan,), daemon=True,
                                    name=f"can-bridge-{chan}") for chan in self.ends]
        for t in threads:
            t.start()
        started = time.monotonic()
        try:
            while not self.stop_event.wait(0.5):
                if duration and time.monotonic() - started >= duration:
                    self.stop_event.set()
                    break
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in threads:
            t.join(timeout=1)
        extra = {"policy_stats": self.engine.stats} if self.engine else {}
        self.session.note(f"can-bridge stop stats={self.stats} {extra}", mode=self.mode)
        return {**self.stats, **extra}

    def stop(self) -> None:
        self.stop_event.set()


# ------------------------------------------------------------- selftest
def run_can_selftest(link1: CanLink, link2: CanLink, session: SessionWriter | None = None,
                     on_event=None, bitrates=(500_000, 250_000, 125_000)) -> dict:
    """Both channels on one terminated bus (CAN-H to CAN-H, CAN-L to CAN-L,
    120 ohm at each end). Checks per bitrate: a frame crosses each way
    byte-exact and is ACKed (the sender gets its echo), an 11-bit and a
    29-bit identifier, and a listen-only receiver does not ACK."""
    on_event = on_event or (lambda line: None)
    report: dict = {"links": [link1.name, link2.name], "checks": [], "passed": True}

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        on_event(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        if not ok:
            report["passed"] = False

    def cross(tx: CanLink, rx: CanLink, msg: CanMsg, label: str) -> None:
        rx.flush_input()
        tx.flush_input()
        t0 = time.perf_counter()
        acked = True
        try:
            tx.send(msg, timeout=0.5)
        except CanTxTimeout:
            acked = False
        got = None
        deadline = t0 + 0.5
        while time.perf_counter() < deadline:
            m = rx.recv(timeout=0.05)
            if m is not None and not m.echo and not m.err:
                got = m
                break
        ms = round((time.perf_counter() - t0) * 1000, 2)
        same = got is not None and got.can_id == msg.can_id and got.data == msg.data \
            and got.ext == msg.ext
        check(f"{label} {tx.name}->{rx.name} {msg.id_str}", acked and same,
              f"acked={acked} received={got is not None and same} {ms} ms")
        if session is not None:
            log_can(session, "selftest", "tx", msg, acked=acked, note=label)
            if got is not None:
                log_can(session, "selftest", "rx", got, note=label)

    for rate in bitrates:
        try:
            link1.set_bitrate(rate, listen_only=False)
            link2.set_bitrate(rate, listen_only=False)
        except CanError as e:
            check(f"open both @ {rate}", False, str(e))
            continue
        on_event(f"@ {rate // 1000} kbit/s")
        pat = bytes(range(0, 8))
        cross(link1, link2, CanMsg(0.0, 0x351, pat), f"{rate // 1000}k 11-bit")
        cross(link2, link1, CanMsg(0.0, 0x7FF, bytes(reversed(pat))), f"{rate // 1000}k 11-bit")
        cross(link1, link2, CanMsg(0.0, 0x108040FE, bytes(8), ext=True), f"{rate // 1000}k 29-bit")
        cross(link2, link1, CanMsg(0.0, 0x18BB0001, b"\x6c\x14\x04\x00\x3c\x00\x64\x00", ext=True),
              f"{rate // 1000}k 29-bit")
        cross(link1, link2, CanMsg(0.0, 0x35C, b"\xC0\x00"), f"{rate // 1000}k DLC 2")
    # listen-only must not ACK: with link2 silent, link1 gets no echo
    try:
        link1.set_bitrate(bitrates[0], listen_only=False)
        link2.set_bitrate(bitrates[0], listen_only=True)
        link1.flush_input(); link2.flush_input()
        acked = True
        try:
            link1.send(CanMsg(0.0, 0x305, bytes(8)), timeout=0.4)
        except CanTxTimeout:
            acked = False
        seen = None
        deadline = time.perf_counter() + 0.3
        while time.perf_counter() < deadline and seen is None:
            m = link2.recv(timeout=0.05)
            if m is not None and not m.err and not m.echo:
                seen = m
        check("listen-only peer does not ACK", not acked and seen is not None,
              f"acked={acked} listen-only side saw frame={seen is not None}")
    finally:
        try:
            link2.set_bitrate(bitrates[0], listen_only=False)
        except CanError:
            pass
    return report
