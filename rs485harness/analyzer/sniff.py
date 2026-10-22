"""Passive two-channel sniffer -> one merged session JSONL."""
from __future__ import annotations

import threading
import time

from ..core.transport import Link, FrameAccumulator
from ..core.session import SessionWriter, start_marker_reader, MARKER_LEGEND
from ..protocols.pylontech import frames as F
from .classify import classify_raw
from .timeline import LiveState


def infer_flow(pf: F.ParsedFrame) -> str | None:
    """On a single-wire tap both directions share one channel: a request is
    a known command with a short INFO (INV->BMS); everything else framed is
    a response (BMS->INV)."""
    if pf.cid2 is None:
        return None
    is_req = pf.cid2 in F.CID2_NAMES and len(pf.info) <= 4
    return "INV->BMS" if is_req else "BMS->INV"


class Sniffer:
    def __init__(self, links: dict[str, Link], session: SessionWriter,
                 on_event=None, markers: bool = True, quiet: bool = False,
                 heartbeat_s: float = 30.0):
        self.links = links
        self.session = session
        self.markers = markers
        self.quiet = quiet              # no per-frame lines, only changes/heartbeat
        self.live = LiveState(heartbeat_s=heartbeat_s)
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats = {chan: {"frames": 0, "raw_bytes": 0} for chan in links}
        self._threads: list[threading.Thread] = []
        self.addr_watch = None          # Huawei serial->address watch (CAN taps)

    def _watch(self, chan: str, link: Link) -> None:
        while not self.stop_event.is_set():
            try:
                self._watch_inner(chan, link)
                return
            except Exception as e:  # adapter re-enumerated etc.
                msg = f"serial error on {chan} ({link.name}): {e!r} - reopening"
                self.on_event("!! " + msg)
                self.session.note(msg, chan, mode="tap", serial_error=True)
                n = 0
                while not self.stop_event.is_set():
                    n += 1
                    if hasattr(link, "reopen") and link.reopen():
                        self.on_event(f"   {chan} reopened after {n} attempt(s)")
                        self.session.note(f"{chan} reopened after {n} attempt(s)", chan)
                        break
                    self.stop_event.wait(1.0)

    def _watch_can(self, chan: str, link) -> None:
        """CAN tap: every frame decoded (Huawei / Pylontech family / Dyness
        native), logged as kind="can", fed to the same LiveState."""
        from ..core.canbus import log_can
        from .can_battery import decode_any
        from .huawei_addr import AddrWatch
        if self.addr_watch is None:
            self.addr_watch = AddrWatch()
        self.stats[chan].setdefault("addr_events", 0)
        while not self.stop_event.is_set():
            msg = link.recv(timeout=0.1)
            if msg is None or msg.echo:
                continue
            dec = decode_any(msg)
            flow = dec.get("flow") if dec else None
            log_can(self.session, chan, "rx", msg, decoded=dec, flow=flow, mode="tap")
            if msg.err:
                self.stats[chan]["raw_bytes"] += 1
                self.on_event(f"[{chan}] {msg}")
                continue
            self.stats[chan]["frames"] += 1
            if dec and dec.get("proto") == "huawei-r48xx":
                for ev in self.addr_watch.feed(dec, time.time()):
                    self.stats[chan]["addr_events"] += 1
                    self.session.log(chan, "-", b"", kind="addr_event", mode="tap", **ev)
                    self.on_event(time.strftime("%H:%M:%S ") + f"[{chan}] {ev['text']}")
            if not self.quiet:
                label = (dec.get("name") or dec.get("kind")) if dec else "?"
                self.on_event(f"[{chan}] {flow or '?':8} {msg}  {label}")
            for line in self.live.feed_can(dec, time.monotonic()):
                self.on_event(time.strftime("%H:%M:%S ") + line)

    def _watch_inner(self, chan: str, link: Link) -> None:
        if getattr(link, "is_can", False):
            self._watch_can(chan, link)
            return
        acc = FrameAccumulator()
        while not self.stop_event.is_set():
            chunk = link.read(timeout=0.1)
            events = acc.feed(chunk) if chunk else []
            stale = acc.flush_if_stale(time.perf_counter())
            if stale:
                events.append(stale)
            for ev in events:
                if ev.kind == "frame":
                    pf = F.parse_frame(ev.data)
                    flow = infer_flow(pf)
                    self.session.log(chan, "rx", ev.data, "frame",
                                     decoded=pf.to_dict(), errors=pf.errors,
                                     ts_mono=ev.ts, flow=flow, mode="tap")
                    self.stats[chan]["frames"] += 1
                    role = "request" if flow == "INV->BMS" else "response"
                    if not self.quiet:
                        self.on_event(f"[{chan}] {flow or '?':8} {pf.describe(role)}")
                    for line in self.live.feed(flow, pf, time.monotonic()):
                        self.on_event(time.strftime("%H:%M:%S ") + line)
                else:
                    cls = classify_raw(ev.data)
                    self.session.log(chan, "rx", ev.data, "raw", decoded=cls,
                                     ts_mono=ev.ts, mode="tap")
                    self.stats[chan]["raw_bytes"] += len(ev.data)
                    self.on_event(f"[{chan}] raw {len(ev.data)}B "
                                  f"({cls['verdict']}): {ev.data.hex().upper()[:64]}")

    def run(self, duration: float | None = None) -> dict:
        ends_desc = ", ".join(f"{chan}={link.name}@{getattr(link, 'baud', '?')}"
                              for chan, link in self.links.items())
        self.session.note(f"sniff start {ends_desc} mode=tap")
        if start_marker_reader(self.session, self.on_event, self.markers):
            self.on_event(MARKER_LEGEND)
        for chan, link in self.links.items():
            t = threading.Thread(target=self._watch, args=(chan, link),
                                 daemon=True, name=f"sniff-{chan}")
            t.start()
            self._threads.append(t)
        started = time.monotonic()
        warned = False
        try:
            while not self.stop_event.wait(0.5):
                if duration and time.monotonic() - started >= duration:
                    self.stop_event.set()
                    break
                # wrong-channel / wrong-baud hint: a live inverter link shows
                # frames within a few seconds; prolonged silence is almost
                # always the other COM port (channel labels != COM order) or
                # the wrong baud.
                total_frames = sum(c["frames"] for c in self.stats.values())
                total_raw = sum(c["raw_bytes"] for c in self.stats.values())
                if not warned and time.monotonic() - started > 10:
                    warned = True
                    names = ", ".join(l.name for l in self.links.values())
                    if total_frames == 0 and total_raw == 0:
                        self.on_event(f"!! no bytes at all on {names} after 10s - "
                                      f"wrong channel? (on this adapter Port 1 was "
                                      f"COM6, Port 2 COM5), or A/B not on the live "
                                      f"pair. Ctrl+C and try the other COM.")
                        self.session.note("silence warning: no bytes after 10s")
                    elif total_frames == 0:
                        self.on_event(f"!! {total_raw} bytes but no valid frames on "
                                      f"{names} after 10s - wrong baud? (SAKO link = "
                                      f"9600, Pylontech monitor = 115200) or A/B swapped.")
                        self.session.note("silence warning: bytes but no frames after 10s")
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in self._threads:
            t.join(timeout=1)
        if self.addr_watch is not None and self.addr_watch.events:
            summ = self.addr_watch.summary()
            self.session.summary({"sub": "sniff", "addr_watch": summ})
            self.on_event(f"address watch: {summ['serial_to_addr']} "
                          f"({len(summ['changes'])} change(s))")
        self.session.note(f"sniff stop stats={self.stats}")
        return self.stats

    def stop(self) -> None:
        self.stop_event.set()
