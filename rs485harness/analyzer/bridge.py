"""Bridge: forward traffic between two channels while logging everything.

Default is transparent byte forwarding (unparseable data crosses too, so
the bridge never breaks a link the devices would otherwise have). With a
Translator attached, complete Pylontech frames are rewritten at frame
boundaries; non-frame bytes are still forwarded raw.
"""
from __future__ import annotations

import threading
import time

from ..core.transport import Link, FrameAccumulator
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from .translate import Translator


class Bridge:
    def __init__(self, link_a: Link, link_b: Link, session: SessionWriter,
                 chan_a: str = "ch1", chan_b: str = "ch2",
                 translator: Translator | None = None, on_event=None):
        self.ends = {chan_a: link_a, chan_b: link_b}
        self.other = {chan_a: chan_b, chan_b: chan_a}
        self.session = session
        self.translator = translator
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats = {chan: {"frames": 0, "raw_bytes": 0, "rewritten": 0}
                      for chan in self.ends}

    def _pump(self, src_chan: str) -> None:
        src = self.ends[src_chan]
        dst_chan = self.other[src_chan]
        dst = self.ends[dst_chan]
        direction = f"{src_chan}->{dst_chan}"
        acc = FrameAccumulator(gap_flush_s=0.05)
        while not self.stop_event.is_set():
            chunk = src.read(timeout=0.05)
            events = acc.feed(chunk) if chunk else []
            stale = acc.flush_if_stale(time.perf_counter())
            if stale:
                events.append(stale)
            for ev in events:
                if ev.kind == "frame":
                    pf = F.parse_frame(ev.data)
                    out, changes = ((ev.data, []) if self.translator is None
                                    else self.translator.apply(pf, direction))
                    dst.write(out)
                    self.stats[src_chan]["frames"] += 1
                    note = None
                    if changes:
                        self.stats[src_chan]["rewritten"] += 1
                        note = "rewrote: " + "; ".join(changes)
                    self.session.log(src_chan, "fwd", ev.data, "frame",
                                     decoded=pf.to_dict(), errors=pf.errors,
                                     note=note)
                    line = f"[{direction}] {pf.describe('auto')}"
                    if changes:
                        line += f"  [{'; '.join(changes)}]"
                    self.on_event(line)
                else:
                    dst.write(ev.data)
                    self.stats[src_chan]["raw_bytes"] += len(ev.data)
                    self.session.log(src_chan, "fwd", ev.data, "raw")
                    self.on_event(f"[{direction}] raw {len(ev.data)}B forwarded")

    def run(self, duration: float | None = None) -> dict:
        ends_desc = ", ".join(f"{chan}={link.name}@{getattr(link, 'baud', '?')}"
                              for chan, link in self.ends.items())
        self.session.note(f"bridge start {ends_desc} "
                          f"translate={'on' if self.translator else 'off'}")
        threads = [threading.Thread(target=self._pump, args=(chan,),
                                    daemon=True, name=f"bridge-{chan}")
                   for chan in self.ends]
        for t in threads:
            t.start()
        try:
            if duration:
                self.stop_event.wait(duration)
                self.stop_event.set()
            else:
                while not self.stop_event.wait(0.5):
                    pass
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in threads:
            t.join(timeout=1)
        self.session.note(f"bridge stop stats={self.stats}")
        return self.stats

    def stop(self) -> None:
        self.stop_event.set()
