"""Simulated CAN battery (Pylontech / Victron / Dyness / LuxPower / Seplos
dialects) and simulated CAN inverter / GX - the bench substitutes and the
fixtures for identify/discover/dump/sniff/timeline over CAN.

The battery emits the profile's frame set from a BatteryState every
`period_s`; extension frames follow the profile's unlock rule (Pylontech:
only after the inverter sent 0x305 and 0x307). The inverter sends its
keep-alive, logs what it hears and, in the Victron profile, flags charge/
discharge loss when 0x351 stays away for 3 s.
"""
from __future__ import annotations

import threading
import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..protocols.pylontech_can import frames as PF
from ..protocols.pylontech_can.dialects import CanDialectProfile, CanInverterProfile
from .battery_state import BatteryState


class CanBatterySimulator:
    def __init__(self, link: CanLink, state: BatteryState, profile: CanDialectProfile,
                 session, chan: str = "bat", on_event=None, string: int = 0,
                 clock=time.monotonic):
        self.link, self.state, self.profile, self.session = link, state, profile, session
        self.chan = chan
        self.on_event = on_event or (lambda line: None)
        self.string = string
        self.clock = clock
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.unlocked = not profile.extensions_need_unlock
        self.seen_305 = 0
        self.seen_307 = 0
        self.last_305_t: float | None = None
        self.stats = {"cycles": 0, "frames": 0, "tx_timeout": 0, "keepalives": 0,
                      "idents": 0, "heartbeats": 0, "extension_cycles": 0}
        self._last_ext = 0.0

    # ------------------------------------------------------------ frames
    def _ext(self) -> bool:
        return self.profile.extended_ids

    def core_frames(self) -> list[CanMsg]:
        s, p = self.state, self.profile
        kw = {"ext": self._ext(), "string": self.string}
        prot = [a for a, on in s.alarms.items() if on and a in PF.PROT_BITS_0.values()
                or on and a in PF.PROT_BITS_1.values()]
        al = [a for a, on in s.alarms.items() if on and (a in PF.ALARM_BITS_2.values()
                                                       or a in PF.ALARM_BITS_3.values())]
        frames = [
            PF.encode_limits(s.charge_voltage_limit_mv / 1000, s.charge_current_limit_da / 10,
                             s.discharge_current_limit_da / 10, s.discharge_voltage_min_mv / 1000,
                             dlc=p.dlcs.get(0x351, 8), **kw),
            PF.encode_soc(s.soc_percent, s.soh_percent, dlc=p.dlcs.get(0x355, 4), **kw),
            PF.encode_analog(s.voltage_mv / 1000, s.current_a, s.temperature_c,
                             dlc=p.dlcs.get(0x356, 6), **kw),
        ]
        if p.sends_359:
            frames.append(PF.encode_alarms(prot, al, module_count=p.module_count,
                                           dlc=p.dlcs.get(0x359, 7),
                                           online_bitmap=(1 << p.module_count) - 1
                                           if p.online_bitmap else 0, **kw))
        if p.sends_35a:
            frames.append(PF.encode_pairs(alarms=self._pair_alarms(prot), **kw))
        frames.append(PF.encode_requests(s.charge_enable, s.discharge_enable, s.force_charge,
                                         False, False, dlc=p.dlcs.get(0x35C, 2), **kw))
        frames.append(PF.encode_brand(p.brand, **kw))
        if p.model_frame or 0x35F in p.optional_ids:
            frames.append(PF.encode_model(0, 0x0105, s.remaining_mah // 1000, **kw))
        if 0x379 in p.optional_ids and p.capacity_frame and not p.extensions_need_unlock:
            frames.append(PF.encode_installed(p.installed_ah, p.capacity_frame, **kw))
        if p.native_frames:
            frames += PF.encode_dyness_native(s.voltage_mv / 1000, s.current_a, s.soc_percent,
                                              s.soh_percent, s.cell_mv,
                                              s.charge_current_limit_da / 10,
                                              s.discharge_current_limit_da / 10,
                                              s.max_temp_c, s.min_temp_c)
        return frames

    @staticmethod
    def _pair_alarms(prot: list[str]) -> list[str]:
        m = {"cell_or_module_over_voltage": "high_voltage", "cell_or_module_under_voltage": "low_voltage",
             "cell_over_temperature": "high_temperature", "cell_under_temperature": "low_temperature",
             "discharge_over_current": "high_current", "charge_over_current": "high_charge_current",
             "system_error": "bms_internal"}
        return [m[a] for a in prot if a in m]

    def extension_frames(self) -> list[CanMsg]:
        s, p = self.state, self.profile
        kw = {"ext": self._ext(), "string": self.string}
        out = []
        ids = set(p.extension_ids)
        if 0x35A in ids and not p.sends_35a:
            out.append(PF.encode_pairs(**kw))
        if 0x370 in ids and p.names:
            out.append(PF.encode_text(0x370, p.names[0], **kw))
            if len(p.names) > 1:
                out.append(PF.encode_text(0x371, p.names[1], **kw))
        if 0x372 in ids:
            out.append(PF.encode_modules(p.module_count, 0 if s.charge_enable else p.module_count,
                                         0 if s.discharge_enable else p.module_count, 0, **kw))
        if 0x373 in ids:
            out.append(PF.encode_cell_extremes(s.min_cell_mv, s.max_cell_mv, s.min_temp_c,
                                               s.max_temp_c, **kw))
        if 0x374 in ids:
            lo = s.cell_mv.index(s.min_cell_mv) + 1
            hi = s.cell_mv.index(s.max_cell_mv) + 1
            out.append(PF.encode_text(0x374, f"01{lo:02d}", **kw))
            out.append(PF.encode_text(0x375, f"01{hi:02d}", **kw))
            out.append(PF.encode_text(0x376, "0101", **kw))
            out.append(PF.encode_text(0x377, "0101", **kw))
        if 0x379 in ids and p.capacity_frame:
            out.append(PF.encode_installed(p.installed_ah, p.capacity_frame, **kw))
        return out

    # ---------------------------------------------------------------- io
    def _tx(self, msg: CanMsg) -> None:
        msg.ts = time.perf_counter()
        acked = True
        try:
            self.link.send(msg, timeout=0.3)
        except CanTxTimeout:
            acked = False
            self.stats["tx_timeout"] += 1
        except CanError as e:
            self.on_event(f"[{self.chan}] tx failed: {e}")
            return
        self.stats["frames"] += 1
        log_can(self.session, self.chan, "tx", msg, decoded=PF.decode(msg), flow="BMS->INV",
                acked=acked, mode="sim")

    def _handle(self, msg: CanMsg) -> None:
        if msg.echo or msg.err:
            return
        dec = PF.decode(msg)
        if not dec or dec["flow"] != "INV->BMS":
            return
        log_can(self.session, self.chan, "rx", msg, decoded=dec, flow="INV->BMS", mode="sim")
        base = int(dec["id"], 16)
        if base == PF.ID_KEEPALIVE:
            self.seen_305 += 1
            self.stats["keepalives"] += 1
            self.last_305_t = self.clock()
        elif base == PF.ID_INV_IDENT:
            self.seen_307 += 1
            self.stats["idents"] += 1
            if self.profile.extensions_need_unlock and self.seen_305 and not self.unlocked:
                if dec.get("all_zero") or dec.get("victron"):
                    self.unlocked = True
                    self.session.note(f"{self.chan}: extension frames unlocked by 0x305+0x307 "
                                      f"({'VIC' if dec.get('victron') else 'zero'})", self.chan)
                    self.on_event(f"[{self.chan}] extension frames unlocked")
        elif base == PF.ID_HEARTBEAT:
            self.stats["heartbeats"] += 1

    def run(self, duration: float | None = None) -> dict:
        p = self.profile
        self.session.note(f"can-battery-sim start profile {p.name} brand {p.brand!r} on "
                          f"{self.link.name} @ {self.link.bitrate}", self.chan)
        t_end = self.clock() + duration if duration else None
        next_cycle = self.clock()
        while not self.stop_event.is_set():
            now = self.clock()
            if t_end and now >= t_end:
                break
            if now >= next_cycle:
                next_cycle = now + p.period_s
                self.state.tick()
                for f in self.core_frames():
                    self._tx(f)
                self.stats["cycles"] += 1
                if self.unlocked and now - self._last_ext >= p.extension_period_s:
                    self._last_ext = now
                    ext = self.extension_frames()
                    for f in ext:
                        self._tx(f)
                    if ext:
                        self.stats["extension_cycles"] += 1
            msg = self.link.recv(timeout=min(0.05, max(0.001, next_cycle - self.clock())))
            if msg is not None:
                self._handle(msg)
        self.session.note(f"can-battery-sim stop stats={self.stats}", self.chan)
        return self.stats

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="can-battery-sim")
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)


class CanInverterSimulator:
    """Sends the inverter side (0x305 / 0x307 / 0x1001), logs the battery,
    keeps a decoded snapshot; Victron profile drops charging when 0x351
    stays away for `stop_without_351_s`."""

    def __init__(self, link: CanLink, profile: CanInverterProfile, session,
                 chan: str = "inv", on_event=None, clock=time.monotonic):
        self.link, self.profile, self.session = link, profile, session
        self.chan = chan
        self.on_event = on_event or (lambda line: None)
        self.clock = clock
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.snapshot: dict = {}
        self.ids_seen: dict[str, int] = {}
        self.last_351_t: float | None = None
        self.charging_allowed = True
        self.stats = {"keepalives": 0, "rx": 0, "tx_timeout": 0, "loss_events": 0}

    def _tx(self, msg: CanMsg) -> None:
        msg.ts = time.perf_counter()
        acked = True
        try:
            self.link.send(msg, timeout=0.3)
        except CanTxTimeout:
            acked = False
            self.stats["tx_timeout"] += 1
        except CanError:
            return
        log_can(self.session, self.chan, "tx", msg, decoded=PF.decode(msg), flow="INV->BMS",
                acked=acked, mode="sim")

    def run(self, duration: float | None = None) -> dict:
        p = self.profile
        self.session.note(f"can-inverter-sim start profile {p.name} on {self.link.name}", self.chan)
        t_end = self.clock() + duration if duration else None
        next_tx = self.clock()
        while not self.stop_event.is_set():
            now = self.clock()
            if t_end and now >= t_end:
                break
            if now >= next_tx:
                next_tx = now + p.period_s
                if p.keepalive:
                    self._tx(PF.encode_keepalive())
                    self.stats["keepalives"] += 1
                if p.ident:
                    self._tx(PF.encode_inverter_ident(victron=p.ident == "vic"))
                if p.heartbeat:
                    self._tx(CanMsg(0.0, PF.ID_HEARTBEAT, bytes(8)))
                if (p.stop_without_351_s and self.last_351_t is not None
                        and now - self.last_351_t > p.stop_without_351_s and self.charging_allowed):
                    self.charging_allowed = False
                    self.stats["loss_events"] += 1
                    self.session.note(f"{self.chan}: no 0x351 for {p.stop_without_351_s} s - "
                                      f"charge/discharge stopped (Victron rule)", self.chan)
                    self.on_event(f"[{self.chan}] 0x351 lost - charging stopped")
            msg = self.link.recv(timeout=min(0.05, max(0.001, next_tx - self.clock())))
            if msg is None or msg.echo or msg.err:
                continue
            dec = PF.decode(msg)
            log_can(self.session, self.chan, "rx", msg, decoded=dec,
                    flow=dec["flow"] if dec else None, mode="sim")
            self.stats["rx"] += 1
            self.ids_seen[msg.id_str] = self.ids_seen.get(msg.id_str, 0) + 1
            if dec and dec["flow"] == "BMS->INV":
                self.snapshot.update({k: v for k, v in dec.items()
                                      if k not in ("proto", "id", "name", "dlc", "flow")})
                if dec["name"] == "limits":
                    self.last_351_t = self.clock()
                    if not self.charging_allowed:
                        self.charging_allowed = True
                        self.on_event(f"[{self.chan}] 0x351 back - charging allowed")
        self.session.note(f"can-inverter-sim stop stats={self.stats}", self.chan)
        return self.stats

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="can-inverter-sim")
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
