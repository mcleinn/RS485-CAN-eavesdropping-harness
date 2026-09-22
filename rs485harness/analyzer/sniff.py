"""Passive two-channel sniffer -> one merged session JSONL."""
from __future__ import annotations

import threading
import time

from ..core.transport import Link, FrameAccumulator
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from .classify import classify_raw


class Sniffer:
    def __init__(self, links: dict[str, Link], session: SessionWriter,
                 on_event=None):
        self.links = links
        self.session = session
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats = {chan: {"frames": 0, "raw_bytes": 0} for chan in links}
        self._threads: list[threading.Thread] = []

    def _watch(self, chan: str, link: Link) -> None:
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
                    self.session.log(chan, "rx", ev.data, "frame",
                                     decoded=pf.to_dict(), errors=pf.errors)
                    self.stats[chan]["frames"] += 1
                    self.on_event(f"[{chan}] {pf.describe('auto')}")
                else:
                    cls = classify_raw(ev.data)
                    self.session.log(chan, "rx", ev.data, "raw", decoded=cls)
                    self.stats[chan]["raw_bytes"] += len(ev.data)
                    self.on_event(f"[{chan}] raw {len(ev.data)}B "
                                  f"({cls['verdict']}): {ev.data.hex().upper()[:64]}")

    def run(self, duration: float | None = None) -> dict:
        ends_desc = ", ".join(f"{chan}={link.name}@{getattr(link, 'baud', '?')}"
                              for chan, link in self.links.items())
        self.session.note(f"sniff start {ends_desc}")
        for chan, link in self.links.items():
            t = threading.Thread(target=self._watch, args=(chan, link),
                                 daemon=True, name=f"sniff-{chan}")
            t.start()
            self._threads.append(t)
        try:
            if duration:
                self.stop_event.wait(duration)
                self.stop_event.set()
            else:
                while not self.stop_event.wait(0.5):
                    pass
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in self._threads:
            t.join(timeout=1)
        self.session.note(f"sniff stop stats={self.stats}")
        return self.stats

    def stop(self) -> None:
        self.stop_event.set()
