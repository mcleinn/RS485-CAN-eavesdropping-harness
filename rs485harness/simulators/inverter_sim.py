"""Inverter simulator: polls a battery the way an inverter would (master).

Modes come from InverterPollProfile (voltronic-6x, monitoring-4x, ...).
Tracks per-command success so 'the battery answers 0x42 but not 0x61' falls
straight out of the stats.
"""
from __future__ import annotations

import threading
import time

from ..core.transport import Link, FrameAccumulator
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P
from ..protocols.pylontech.dialects import InverterPollProfile


class InverterSimulator:
    def __init__(self, link: Link, profile: InverterPollProfile,
                 session: SessionWriter, chan: str = "inv", on_event=None):
        self.link = link
        self.profile = profile
        self.session = session
        self.chan = chan
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats: dict = {"polls": 0, "responses": 0, "timeouts": 0,
                            "per_cid2": {}, "latencies_ms": []}
        self._thread: threading.Thread | None = None

    def poll_once(self, cid2: int, adr: int | None = None, ver: int | None = None,
                  timeout: float | None = None) -> dict:
        """Send one request, wait for one response. Returns result dict."""
        p = self.profile
        adr = p.adr if adr is None else adr
        ver = p.ver if ver is None else ver
        timeout = p.response_timeout_s if timeout is None else timeout
        info = p.info_for.get(cid2, "")
        req = F.encode_frame(ver, adr, p.cid1, cid2, info)

        self.link.flush_input()
        t0 = time.perf_counter()
        self.link.write(req)
        self.session.log(self.chan, "tx", req, "frame",
                         decoded=F.parse_frame(req).to_dict())
        self.stats["polls"] += 1
        key = f"{cid2:02X}"
        per = self.stats["per_cid2"].setdefault(key, {"sent": 0, "ok": 0,
                                                      "rtn_err": 0, "timeout": 0})
        per["sent"] += 1

        acc = FrameAccumulator()
        deadline = t0 + timeout
        while time.perf_counter() < deadline:
            chunk = self.link.read(timeout=min(0.1, max(0.01, deadline - time.perf_counter())))
            if not chunk:
                continue
            for ev in acc.feed(chunk):
                if ev.kind != "frame":
                    self.session.log(self.chan, "rx", ev.data, "raw")
                    continue
                latency_ms = round((ev.ts - t0) * 1000, 1)
                pf = F.parse_frame(ev.data)
                self.session.log(self.chan, "rx", ev.data, "frame",
                                 decoded=pf.to_dict(), errors=pf.errors,
                                 note=f"latency_ms={latency_ms}")
                self.stats["responses"] += 1
                self.stats["latencies_ms"].append(latency_ms)
                decoded_payload = None
                rtn = pf.cid2
                if rtn in (0x00, cid2):  # correct RTN, or buggy cid2 echo
                    decoded_payload = P.decode_response_info(cid2, pf.info)
                    per["ok"] += 1
                else:
                    per["rtn_err"] += 1
                result = {"status": "response", "latency_ms": latency_ms,
                          "frame": pf.to_dict(), "rtn": rtn,
                          "rtn_meaning": F.RTN_CODES.get(rtn, "?"),
                          "cid2_echoed": rtn == cid2 and cid2 != 0,
                          "payload": decoded_payload}
                self.on_event(f"{F.CID2_NAMES.get(cid2, hex(cid2))}: "
                              f"RTN={rtn:02X} in {latency_ms} ms")
                return result
        per["timeout"] += 1
        self.stats["timeouts"] += 1
        self.on_event(f"{F.CID2_NAMES.get(cid2, hex(cid2))} @ADR {adr:02X}: timeout")
        self.session.note(f"timeout waiting for CID2 {cid2:02X} ADR {adr:02X}", self.chan)
        return {"status": "timeout", "cid2": cid2, "adr": adr}

    def run(self, duration: float | None = None) -> dict:
        self.session.note(f"inverter-sim start profile={self.profile.name} "
                          f"link={self.link.name}", self.chan)
        t_end = time.monotonic() + duration if duration else None
        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            for cid2 in self.profile.commands:
                if self.stop_event.is_set():
                    break
                self.poll_once(cid2)
            if t_end and time.monotonic() > t_end:
                break
            rest = self.profile.period_s - (time.monotonic() - cycle_start)
            if rest > 0:
                self.stop_event.wait(rest)
        self.session.note(f"inverter-sim stop stats={self._stats_summary()}", self.chan)
        return self._stats_summary()

    def _stats_summary(self) -> dict:
        s = dict(self.stats)
        lat = s.pop("latencies_ms")
        if lat:
            lat_sorted = sorted(lat)
            s["latency_ms"] = {"min": lat_sorted[0], "max": lat_sorted[-1],
                               "median": lat_sorted[len(lat) // 2],
                               "n": len(lat)}
        return s

    def start(self, duration: float | None = None) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, args=(duration,),
                                        daemon=True, name="inverter-sim")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
