"""Battery simulator: pretends to be a Pylontech-RS485 pack (slave side).

Answers both command families from a BatteryState through a DialectProfile.
Every poll - answered or not - is logged to the session, which is exactly
the diagnostic value: run this against the real inverter and you learn what
the inverter asks for and whether it accepts the answers.
"""
from __future__ import annotations

import threading
import time

from ..core.transport import Link, FrameAccumulator
from ..core.session import SessionWriter
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P
from ..protocols.pylontech.dialects import DialectProfile
from .battery_state import BatteryState


class BatterySimulator:
    def __init__(self, link: Link, state: BatteryState, profile: DialectProfile,
                 session: SessionWriter, chan: str = "bat",
                 on_event=None):
        self.link = link
        self.state = state
        self.profile = profile
        self.session = session
        self.chan = chan
        self.on_event = on_event or (lambda line: None)
        self.stop_event = threading.Event()
        self.stats = {"requests": 0, "answered": 0, "rtn04": 0, "ignored": 0,
                      "unparseable": 0, "cid2_seen": {}}
        self._acc = FrameAccumulator()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- responses

    def _build_response_info(self, cid2: int) -> str | None:
        s = self.state
        fam = F.cid2_family(cid2)
        if fam not in self.profile.families:
            return None
        if cid2 == 0x60:
            return P.build_60(s)
        if cid2 == 0x61:
            return P.build_61(s)
        if cid2 == 0x62:
            return P.build_62(s)
        if cid2 == 0x63:
            return P.build_63(s)
        if cid2 == 0x42:
            return P.build_42_single(s)
        if cid2 == 0x44:
            return P.build_62(s)  # alarm info: same 4 status bytes serve here
        if cid2 == 0x47:
            return P.build_47(s)
        if cid2 == 0x4F:
            return ""
        if cid2 == 0x51:
            return P.build_51(s)
        if cid2 == 0x92:
            return P.build_92(s)
        if cid2 == 0x93:
            return P.build_93(s)
        return None

    def _respond(self, req: F.ParsedFrame) -> None:
        p = self.profile
        self.stats["requests"] += 1
        key = f"{req.cid2:02X}"
        self.stats["cid2_seen"][key] = self.stats["cid2_seen"].get(key, 0) + 1

        if p.listen_adrs is not None and req.adr not in p.listen_adrs:
            self.stats["ignored"] += 1
            self.session.note(f"ignored poll for ADR {req.adr:02X} "
                              f"(listening only on {list(p.listen_adrs)})",
                              self.chan)
            self.on_event(f"ignored poll for ADR {req.adr:02X} (not ours)")
            return

        info = self._build_response_info(req.cid2)
        adr = p.response_adr_for(req.adr)
        if info is None:
            if p.unknown_cmd == "silence":
                self.stats["ignored"] += 1
                self.session.note(f"staying silent on unsupported CID2 "
                                  f"{req.cid2:02X} (profile "
                                  f"unknown_cmd=silence)", self.chan)
                self.on_event(f"CID2 {req.cid2:02X} unsupported -> staying silent")
                return
            self.stats["rtn04"] += 1
            resp = F.encode_frame(p.ver, adr, req.cid1 or 0x46, 0x04, "")
            note = f"RTN=04 (CID2 {req.cid2:02X} unsupported)"
        else:
            cid2_pos = req.cid2 if p.response_cid2 == "echo" else 0x00
            resp = F.encode_frame(p.ver, adr, req.cid1 or 0x46, cid2_pos, info)
            note = f"answered {F.CID2_NAMES.get(req.cid2, hex(req.cid2))}"
            self.stats["answered"] += 1

        if p.response_delay_s:
            time.sleep(p.response_delay_s)
        self.link.write(resp)
        self.session.log(self.chan, "tx", resp, "frame",
                         decoded=F.parse_frame(resp).to_dict(), note=note)
        self.on_event(f"-> {note}")

    # ------------------------------------------------------------- main loop

    def run(self, duration: float | None = None) -> dict:
        self.session.note(f"battery-sim start profile={self.profile.name} "
                          f"link={self.link.name}", self.chan)
        t_end = time.monotonic() + duration if duration else None
        while not self.stop_event.is_set():
            if t_end and time.monotonic() > t_end:
                break
            self.state.tick()
            chunk = self.link.read(timeout=0.05)
            events = self._acc.feed(chunk) if chunk else []
            stale = self._acc.flush_if_stale(time.perf_counter())
            if stale:
                events.append(stale)
            for ev in events:
                if ev.kind != "frame":
                    self.session.log(self.chan, "rx", ev.data, "raw")
                    self.on_event(f"raw bytes: {ev.data.hex().upper()}")
                    continue
                pf = F.parse_frame(ev.data)
                self.session.log(self.chan, "rx", ev.data, "frame",
                                 decoded=pf.to_dict(), errors=pf.errors)
                self.on_event(f"<- {pf.describe('request')}")
                if pf.ver is None:
                    self.stats["unparseable"] += 1
                    continue
                self._respond(pf)
        self.session.note(f"battery-sim stop stats={self.stats}", self.chan)
        return self.stats

    def start(self, duration: float | None = None) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, args=(duration,),
                                        daemon=True, name="battery-sim")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
