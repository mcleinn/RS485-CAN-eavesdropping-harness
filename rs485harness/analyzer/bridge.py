"""Bridge: forward traffic between two channels while logging everything.

Two modes, always visible in the session (start note + `mode` on every
record):

- **transparent** (no translator): byte-level pass-through. Every chunk
  read on one side is written to the other side immediately - unparsed,
  malformed or foreign bytes included - so the bridge adds only USB/serial
  latency, no store-and-forward delay. Frames are parsed in parallel purely
  for the log.
- **modify** (translator attached): frame-level. Complete Pylontech frames
  are passed through the translator (header rules and/or a payload policy)
  and re-checksummed. Non-frame bytes are still forwarded unchanged.

Fail-safes in modify mode:
- translator exception  -> forward the ORIGINAL frame, log the exception
- malformed / bad-CRC   -> forwarded unchanged (never "repaired")
- unknown frame         -> forwarded unchanged
- one side silent       -> nothing is synthesized; gap is logged; policy
                           resets after its loss timeout
Every modified frame produces two records: the original (dir=rx, with the
audit) and the modified bytes actually sent (dir=tx).
"""
from __future__ import annotations

import threading
import time
import traceback

from ..core.transport import Link, FrameAccumulator
from ..core.session import SessionWriter, start_marker_reader, MARKER_LEGEND
from ..protocols.pylontech import frames as F
from .translate import Translator
from .timeline import LiveState

ROLE_FLOW = {"inverter": "INV->BMS", "battery": "BMS->INV"}


class SilentStall(Exception):
    """Raised inside a pump to route a liveness-triggered reopen through the
    normal serial-error recovery path."""


class Bridge:
    def __init__(self, link_a: Link, link_b: Link, session: SessionWriter,
                 chan_a: str = "ch1", chan_b: str = "ch2",
                 translator: Translator | None = None, on_event=None,
                 role_a: str = "inverter", role_b: str = "battery",
                 markers: bool = True, gap_note_s: float = 3.0,
                 quiet: bool = False, heartbeat_s: float = 30.0,
                 stall_reopen_s: float = 6.0):
        # liveness guard: once traffic has been seen, total silence on BOTH
        # channels for stall_reopen_s means the adapter/driver went deaf
        # (seen on real hardware: device still enumerated, no bytes for 55s
        # until Windows finally errored). Reopen the ports proactively.
        self.stall_reopen_s = stall_reopen_s
        self._last_rx_t: dict[str, float | None] = {chan_a: None, chan_b: None}
        self._force_reopen: set[str] = set()
        self._last_forced = 0.0
        self.ends = {chan_a: link_a, chan_b: link_b}
        self.other = {chan_a: chan_b, chan_b: chan_a}
        self.roles = {chan_a: role_a, chan_b: role_b}
        self.session = session
        self.translator = translator
        self.mode = "modify" if translator is not None else "transparent"
        self.markers = markers
        self.quiet = quiet
        self.live = LiveState(heartbeat_s=heartbeat_s)
        self.gap_note_s = gap_note_s
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats = {chan: {"frames": 0, "raw_bytes": 0, "rewritten": 0,
                             "errors": 0, "gaps": 0} for chan in self.ends}
        self._last_frame_t: dict[str, float] = {}
        self._role_counts = {chan: {"req": 0, "resp": 0} for chan in self.ends}
        self._role_warned = False
        if translator is not None:
            # tell the translator which direction is BMS -> inverter
            bms_chan = chan_a if role_a == "battery" else chan_b
            translator.bms_to_inv = f"{bms_chan}->{self.other[bms_chan]}"

    def _flow(self, src_chan: str) -> str:
        return ROLE_FLOW.get(self.roles[src_chan], f"{src_chan}->{self.other[src_chan]}")

    def _check_roles(self, src_chan: str, pf: F.ParsedFrame) -> None:
        """Warn once if the traffic contradicts the configured roles, i.e.
        requests arrive on the 'battery' side - the COM ports are swapped."""
        if self._role_warned or pf.cid2 is None:
            return
        is_req = pf.cid2 in F.CID2_NAMES and len(pf.info) <= 4
        c = self._role_counts[src_chan]
        c["req" if is_req else "resp"] += 1
        role = self.roles[src_chan]
        wrong = ((role == "battery" and c["req"] >= 3 and c["resp"] == 0) or
                 (role == "inverter" and c["resp"] >= 3 and c["req"] == 0))
        if wrong:
            self._role_warned = True
            msg = (f"!! ROLE MISMATCH: {src_chan} is configured as {role} but "
                   f"carries {'requests' if role == 'battery' else 'responses'} - "
                   f"the COM ports are swapped. Stop (Ctrl+C) and swap --ch1/--ch2 "
                   f"(keep the roles). Transparent forwarding is unaffected, but "
                   f"flow labels - and any policy direction - would be inverted.")
            self.on_event(msg)
            self.session.note(msg, src_chan, mode=self.mode, role_mismatch=True)

    def _note_gap(self, src_chan: str, now: float) -> None:
        last = self._last_frame_t.get(src_chan)
        if last is not None and now - last > self.gap_note_s:
            self.stats[src_chan]["gaps"] += 1
            self.session.note(f"gap {now - last:.2f}s with no frame from "
                              f"{self.roles[src_chan]} ({self._flow(src_chan)})",
                              src_chan, flow=self._flow(src_chan), mode=self.mode)
        self._last_frame_t[src_chan] = now

    # ------------------------------------------------------------ liveness

    def _check_liveness(self) -> None:
        """Called twice a second from run(). If bytes were flowing and both
        channels have now been silent for stall_reopen_s, ask each pump to
        reopen its port (rate-limited so a genuinely quiet bus is not
        hammered)."""
        if self.stall_reopen_s <= 0:
            return
        seen = [t for t in self._last_rx_t.values() if t is not None]
        if not seen:
            return                      # never saw traffic: nothing to protect
        now = time.perf_counter()
        silent = now - max(seen)
        if silent > self.stall_reopen_s and \
                now - self._last_forced > max(15.0, 2 * self.stall_reopen_s):
            self._last_forced = now
            msg = (f"no bytes on either channel for {silent:.1f}s although "
                   f"traffic was flowing - suspected silent adapter stall; "
                   f"reopening both ports")
            self.on_event("!! " + msg)
            self.session.note(msg, mode=self.mode, stall_reopen=True)
            self._force_reopen.update(self.ends)

    def _liveness_gate(self, src_chan: str) -> None:
        if src_chan in self._force_reopen:
            self._force_reopen.discard(src_chan)
            raise SilentStall(f"liveness guard: forced reopen of {src_chan}")

    # ------------------------------------------------------------ pumps

    def _recover(self, chan: str, err: Exception) -> bool:
        """A pump hit a serial error (typically the adapter re-enumerated
        while wiring was touched). Log it, then keep trying to reopen the
        port until it comes back or we are told to stop. Returns False if
        stopping."""
        link = self.ends[chan]
        msg = f"serial error on {chan} ({link.name}): {err!r} - reopening"
        self.on_event("!! " + msg)
        self.session.note(msg, chan, mode=self.mode, serial_error=True)
        self.stats[chan]["errors"] += 1
        if not hasattr(link, "reopen"):
            return True         # virtual/loopback links: nothing to reopen
        attempts = 0
        while not self.stop_event.is_set():
            attempts += 1
            if hasattr(link, "reopen") and link.reopen():
                self.on_event(f"   {chan} ({link.name}) reopened after {attempts} attempt(s)")
                self.session.note(f"{chan} reopened after {attempts} attempt(s)", chan,
                                  mode=self.mode)
                return True
            self.stop_event.wait(1.0)
        return False


    def _pump_transparent(self, src_chan: str) -> None:
        while not self.stop_event.is_set():
            try:
                self._pump_transparent_inner(src_chan)
                return
            except Exception as e:  # serial.SerialException, OSError, ...
                if not self._recover(src_chan, e):
                    return

    def _pump_transparent_inner(self, src_chan: str) -> None:
        src, dst = self.ends[src_chan], self.ends[self.other[src_chan]]
        flow = self._flow(src_chan)
        acc = FrameAccumulator(gap_flush_s=0.05)
        while not self.stop_event.is_set():
            self._liveness_gate(src_chan)
            chunk = src.read(timeout=0.05)
            if chunk:
                self._last_rx_t[src_chan] = chunk.ts
                dst.write(chunk.data)          # byte-level, immediately
            events = acc.feed(chunk) if chunk else []
            stale = acc.flush_if_stale(time.perf_counter())
            if stale:
                events.append(stale)
            for ev in events:
                if ev.kind == "frame":
                    pf = F.parse_frame(ev.data)
                    self._note_gap(src_chan, ev.ts)
                    self._check_roles(src_chan, pf)
                    self.stats[src_chan]["frames"] += 1
                    if pf.errors:
                        self.stats[src_chan]["errors"] += 1
                    self.session.log(src_chan, "fwd", ev.data, "frame",
                                     decoded=pf.to_dict(), errors=pf.errors,
                                     ts_mono=ev.ts, flow=flow, mode=self.mode)
                    role = "request" if flow == "INV->BMS" else "response"
                    if not self.quiet:
                        self.on_event(f"[{flow}] {pf.describe(role)}")
                    for line in self.live.feed(flow, pf, time.monotonic()):
                        self.on_event(time.strftime("%H:%M:%S ") + line)
                else:
                    self.stats[src_chan]["raw_bytes"] += len(ev.data)
                    self.session.log(src_chan, "fwd", ev.data, "raw",
                                     ts_mono=ev.ts, flow=flow, mode=self.mode)
                    if not self.quiet:
                        self.on_event(f"[{flow}] raw {len(ev.data)}B forwarded")

    def _pump_modify(self, src_chan: str) -> None:
        while not self.stop_event.is_set():
            try:
                self._pump_modify_inner(src_chan)
                return
            except Exception as e:
                if not self._recover(src_chan, e):
                    return

    def _pump_modify_inner(self, src_chan: str) -> None:
        src, dst = self.ends[src_chan], self.ends[self.other[src_chan]]
        direction = f"{src_chan}->{self.other[src_chan]}"
        flow = self._flow(src_chan)
        acc = FrameAccumulator(gap_flush_s=0.05)
        while not self.stop_event.is_set():
            self._liveness_gate(src_chan)
            chunk = src.read(timeout=0.05)
            if chunk:
                self._last_rx_t[src_chan] = chunk.ts
            events = acc.feed(chunk) if chunk else []
            stale = acc.flush_if_stale(time.perf_counter())
            if stale:
                events.append(stale)
            for ev in events:
                if ev.kind != "frame":
                    dst.write(ev.data)         # non-frame bytes: unchanged
                    self.stats[src_chan]["raw_bytes"] += len(ev.data)
                    self.session.log(src_chan, "fwd", ev.data, "raw",
                                     ts_mono=ev.ts, flow=flow, mode=self.mode)
                    continue
                pf = F.parse_frame(ev.data)
                self._note_gap(src_chan, ev.ts)
                self._check_roles(src_chan, pf)
                self.stats[src_chan]["frames"] += 1
                if pf.errors:
                    self.stats[src_chan]["errors"] += 1
                out, changes, audit = ev.data, [], None
                try:
                    out, changes = self.translator.apply(pf, direction)
                    audit = getattr(self.translator, "last_audit", None)
                except Exception as e:  # fail-safe: never drop, never invent
                    out, changes = ev.data, []
                    self.session.note(
                        f"translator exception -> forwarded ORIGINAL frame: "
                        f"{e!r}\n{traceback.format_exc()}", src_chan,
                        flow=flow, mode=self.mode, failsafe=True)
                    self.on_event(f"[{flow}] !! translator exception, "
                                  f"original forwarded: {e!r}")
                dst.write(out)
                role = "request" if flow == "INV->BMS" else "response"
                if out != ev.data:
                    self.stats[src_chan]["rewritten"] += 1
                    # audit: original as received ...
                    self.session.log(src_chan, "rx", ev.data, "frame",
                                     decoded=pf.to_dict(), errors=pf.errors,
                                     ts_mono=ev.ts, flow=flow, mode=self.mode,
                                     audit=audit, note="ORIGINAL (modified below)")
                    # ... and the bytes actually sent
                    mpf = F.parse_frame(out)
                    self.session.log(src_chan, "tx", out, "frame",
                                     decoded=mpf.to_dict(), errors=mpf.errors,
                                     flow=flow, mode=self.mode, modified=True,
                                     note="MODIFIED: " + "; ".join(changes))
                    self.on_event(f"[{flow}] {pf.describe(role)}  => MODIFIED: "
                                  + "; ".join(changes))
                else:
                    self.session.log(src_chan, "fwd", ev.data, "frame",
                                     decoded=pf.to_dict(), errors=pf.errors,
                                     ts_mono=ev.ts, flow=flow, mode=self.mode,
                                     audit=audit)
                    self.on_event(f"[{flow}] {pf.describe(role)}")

    # ------------------------------------------------------------ run

    def run(self, duration: float | None = None) -> dict:
        ends_desc = ", ".join(f"{chan}={link.name}@{getattr(link, 'baud', '?')}"
                              f"({self.roles[chan]})"
                              for chan, link in self.ends.items())
        tr = ""
        if self.translator is not None:
            tr = " translator=" + self.translator.describe()
        self.session.note(f"bridge start {ends_desc} mode={self.mode}{tr}",
                          mode=self.mode)
        if start_marker_reader(self.session, self.on_event, self.markers):
            self.on_event(MARKER_LEGEND)
        target = self._pump_modify if self.mode == "modify" else self._pump_transparent
        threads = [threading.Thread(target=target, args=(chan,), daemon=True,
                                    name=f"bridge-{chan}") for chan in self.ends]
        for t in threads:
            t.start()
        started = time.monotonic()
        try:
            while not self.stop_event.wait(0.5):
                if duration and time.monotonic() - started >= duration:
                    self.stop_event.set()
                    break
                self._check_liveness()
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in threads:
            t.join(timeout=1)
        extra = {}
        if self.translator is not None and getattr(self.translator, "engine", None):
            extra["policy_stats"] = self.translator.engine.stats
        self.session.note(f"bridge stop stats={self.stats} {extra}", mode=self.mode)
        return {**self.stats, **extra}

    def stop(self) -> None:
        self.stop_event.set()
