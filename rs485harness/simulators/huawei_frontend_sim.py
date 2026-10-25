"""Simulated add-on control board ("front-end") of a Huawei-based charger -
the foreign CAN master the gateway has to keep happy.

Modelled on the board measured on our unit (docs/CHARGER-HUAWEI-UNITS.md
3.3): every `period_s` (0.62 s) a status request to the PSU address, then
within a few ms five writes - default voltage, default current, online
voltage (twice), standby, online current - each expecting an ack; every
`cfg_period_s` (3 s) a broadcast config frame `0x108081FE 01 13 00 01 ..`
that gets no ack. It keeps what it sees: the last complete status burst
(what its screen would show) and ack bookkeeping per write.

Values are what our board sent in standby: 50.0 V / raw 210 defaults,
48.0 V online (encoded V x 1023.6 like the real board), raw 70 online
current, standby 0. `set_points()` changes them at run time (what a user
does on the board's app).
"""
from __future__ import annotations

import threading
import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..protocols.huawei_r48xx import regs as R
from ..protocols.huawei_r48xx.ids import (HuaweiId, PROTO_CTL, CMD_DATA, CMD_SET, CMD_CFG,
                                          CMD_INFO, request_id)

BOARD_VOLT_SCALE = 1023.6          # measured: 48.0 V -> raw 0xBFEE


class HuaweiFrontEndSimulator:
    def __init__(self, link: CanLink, session, addr: int = 1, period_s: float = 0.62,
                 cfg_period_s: float = 3.0, ack_timeout_s: float = 0.3,
                 default_v: float = 50.0, default_i_raw: int = 210,
                 online_v: float = 48.0, online_i_raw: int = 70, standby: int = 0,
                 chan: str = "fe", on_event=None, clock=time.monotonic, tick_s: float = 0.005,
                 disguised_standby: bool = False):
        self.link = link
        self.session = session
        self.addr = addr
        self.period_s = period_s
        self.cfg_period_s = cfg_period_s
        self.ack_timeout_s = ack_timeout_s
        self.chan = chan
        self.on_event = on_event or (lambda line: None)
        self.clock = clock
        self.tick_s = tick_s
        self.default_v, self.default_i_raw = default_v, default_i_raw
        self.online_v, self.online_i_raw, self.standby = online_v, online_i_raw, standby
        self.disguised_standby = disguised_standby     # measured: standby payload under the 0x50 id, once
        self.stats = {"cycles": 0, "requests": 0, "bursts_complete": 0, "writes": 0,
                      "acks_ok": 0, "acks_error": 0, "acks_missing": 0, "cfg_frames": 0,
                      "tx_timeout": 0, "status_frames": 0, "unsolicited": 0}
        self.last_status: dict = {}
        self.acks: dict[str, dict] = {}
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_cycle = 0.0
        self._next_cfg = 0.0

    # ----------------------------------------------------------- setpoints
    def set_points(self, online_v: float | None = None, online_i_raw: int | None = None,
                   default_v: float | None = None, default_i_raw: int | None = None,
                   standby: int | None = None) -> None:
        if online_v is not None:
            self.online_v = online_v
        if online_i_raw is not None:
            self.online_i_raw = online_i_raw
        if default_v is not None:
            self.default_v = default_v
        if default_i_raw is not None:
            self.default_i_raw = default_i_raw
        if standby is not None:
            self.standby = standby

    def _writes(self) -> list[tuple[str, bytes]]:
        def u32(reg, raw):
            return bytes([0x01, reg, 0, 0]) + int(raw).to_bytes(4, "big")
        return [
            ("default_voltage", u32(0x01, round(self.default_v * BOARD_VOLT_SCALE))),
            ("default_current", u32(0x04, self.default_i_raw)),
            ("online_voltage", u32(0x00, round(self.online_v * BOARD_VOLT_SCALE))),
            ("standby", bytes([0x01, 0x32, 0, self.standby, 0, 0, 0, 0])),
            ("online_voltage", u32(0x00, round(self.online_v * BOARD_VOLT_SCALE))),
            ("online_current", u32(0x03, self.online_i_raw)),
        ]

    # ------------------------------------------------------------------ io
    def _tx(self, can_id: int, data: bytes, note: str) -> bool:
        msg = CanMsg(time.perf_counter(), can_id, data, ext=True)
        acked = True
        try:
            self.link.send(msg, timeout=0.3)
        except CanTxTimeout:
            acked = False
            self.stats["tx_timeout"] += 1
        except CanError as e:
            self.on_event(f"[{self.chan}] tx failed: {e}")
            return False
        log_can(self.session, self.chan, "tx", msg, decoded=R.decode(msg), flow="CTL->PSU",
                acked=acked, mode="frontend-sim", note=note)
        return acked

    def _rx(self, msg: CanMsg) -> dict | None:
        if msg.echo or msg.err:
            return None
        dec = R.decode(msg)
        log_can(self.session, self.chan, "rx", msg, decoded=dec,
                flow=dec["flow"] if dec else None, mode="frontend-sim")
        if not dec:
            return None
        if dec["kind"] == "unsolicited":
            self.stats["unsolicited"] += 1
        elif dec["kind"] == "status" and dec["addr"] == self.addr:
            self.stats["status_frames"] += 1
            self._burst.feed(msg)
            if self._burst.complete:
                self.stats["bursts_complete"] += 1
                self.last_status = self._burst.snapshot()
                self._burst = R.StatusBurst()
        return dec

    def _wait_ack(self, name: str, data: bytes) -> None:
        deadline = time.monotonic() + self.ack_timeout_s
        while time.monotonic() < deadline:
            msg = self.link.recv(timeout=0.005)
            if msg is None:
                continue
            dec = self._rx(msg)
            if dec and dec["kind"] == "ack" and dec["addr"] == self.addr and \
                    bytes(msg.data)[1] == data[1]:
                ack = R.decode_ack_data(msg.data)
                self.acks[name] = ack
                self.stats["acks_ok" if ack["ok"] else "acks_error"] += 1
                return
        self.stats["acks_missing"] += 1
        self.acks[name] = {"ok": False, "missing": True}

    def _cycle(self) -> None:
        self.stats["cycles"] += 1
        self._burst = R.StatusBurst()
        self.stats["requests"] += 1
        self._tx(request_id(CMD_DATA, self.addr), bytes(8), "front-end status request")
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            msg = self.link.recv(timeout=0.005)
            if msg is None:
                continue
            dec = self._rx(msg)
            if dec and dec["kind"] == "status" and not dec["cont"]:
                break
        for name, data in self._writes():
            self.stats["writes"] += 1
            if self._tx(request_id(CMD_SET, self.addr), data, f"front-end write {name}"):
                self._wait_ack(name, data)
            else:
                self.stats["acks_missing"] += 1

    def run(self, duration: float | None = None) -> dict:
        self._burst = R.StatusBurst()
        self.session.note(f"huawei-frontend-sim start addr {self.addr} period {self.period_s}s "
                          f"online {self.online_v} V raw {self.online_i_raw}", self.chan)
        t0 = self.clock()
        self._next_cycle = t0
        self._next_cfg = t0 + 0.1
        if self.disguised_standby:
            self._tx(request_id(CMD_INFO, self.addr), bytes([0x01, 0x32, 0, self.standby, 0, 0, 0, 0]),
                     "front-end standby payload under the INFO id (measured quirk)")
        while not self.stop_event.is_set():
            now = self.clock()
            if duration and now - t0 >= duration:
                break
            if now >= self._next_cycle:
                self._next_cycle = now + self.period_s
                self._cycle()
            if now >= self._next_cfg:
                self._next_cfg = now + self.cfg_period_s
                self.stats["cfg_frames"] += 1
                self._tx(request_id(CMD_CFG, 0), bytes([0x01, 0x13, 0x00, 0x01, 0, 0, 0, 0]),
                         "front-end cfg 0x13 broadcast")
            msg = self.link.recv(timeout=self.tick_s)
            if msg is not None:
                self._rx(msg)
        self.session.note(f"huawei-frontend-sim stop stats={self.stats}", self.chan)
        return self.stats

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="huawei-frontend-sim")
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.5)
