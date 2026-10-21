"""Simulated Huawei R48xx rectifier on a CAN link - the bench substitute
and the test fixture for `rsh huawei`.

Behaviour per docs/refs/huawei/NOTES.md: answers 0x40 (status burst, last
frame 0x83 with cont=0), 0x50 (info, 6 frames), 0xD2 (e-label); acks 0x80
sets, with 0x21 in byte 0 when the value is outside the ranges the real
units accept; applies the configured current-scale policy so the tool's
calibration can be exercised against each reading; sends the unsolicited
0x1001117E every ~377 ms; reverts on-line values to the stored defaults
after `timeout_s` without a frame addressed to it (~60 s on real units).

Electrical model (enough for verification-by-measurement tests): a
resistive load (`load_ohm`), a constant power load (`load_w`) or a battery
(`battery_v`, internal 50 mOhm); the output current is clipped at the
active current limit and the voltage sags accordingly.
"""
from __future__ import annotations

import threading
import time

from ..core.canbus import CanLink, CanMsg, CanTxTimeout, CanError, log_can
from ..protocols.huawei_r48xx import regs as R
from ..protocols.huawei_r48xx.ids import (HuaweiId, PROTO_PSU, CMD_DATA, CMD_INFO,
                                          CMD_SET, CMD_GET, CMD_ELABEL, CMD_UNSOL,
                                          reply_id)

STATUS_ORDER = [0x0E, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x78, 0x7F, 0x80,
                0x81, 0x82]            # then 0x83 as the last frame
ELABEL_TEXT = ("/$[ArchivesInfo Version]\r\n/$ArchivesInfoVersion=3.0\r\n\r\n"
               "[Board Properties]\r\nBoardType=R4850G2\r\nBarCode=2102310SIM0000000001\r\n"
               "Item=02310SIM\r\nDescription=Function Module,R4850G2,3000W Rectifier\r\n"
               "Manufactured=2020-01-01\r\nVendorName=Huawei\r\nIssueNumber=00\r\n"
               "CLEICode=\r\nBOM=\r\n")


class HuaweiPsuSimulator:
    def __init__(self, link: CanLink, session, addr: int = 1, nominal_a: float = 52.0,
                 scale: str = "ratio1024", load_ohm: float | None = None,
                 load_w: float | None = None, battery_v: float | None = None,
                 timeout_s: float = R.ONLINE_TIMEOUT_S, unsol_period_s: float = 0.377,
                 default_v: float = 53.5, default_current_a: float | None = None,
                 max_current_a: float | None = None, chan: str = "psu",
                 on_event=None, clock=time.monotonic):
        self.link = link
        self.session = session
        self.addr = addr
        self.nominal_a = nominal_a
        self.scale = scale
        self.load_ohm, self.load_w, self.battery_v = load_ohm, load_w, battery_v
        self.timeout_s = timeout_s
        self.unsol_period_s = unsol_period_s
        self.max_current_a = max_current_a or nominal_a * 1.17
        self.chan = chan
        self.on_event = on_event or (lambda line: None)
        self.clock = clock
        sc = R.CURRENT_SCALES[scale]
        self.default_v = default_v
        self.default_i_raw = sc.encode(default_current_a if default_current_a is not None
                                       else nominal_a, nominal_a)
        self.ovp_v = 59.0
        self.online_v = default_v
        self.online_i_raw = self.default_i_raw
        self.online = False
        self.standby = False
        self.fan_max = False
        self.last_cmd_t = -1e9
        self.ac_on = True
        self.hours = 1234
        self.stats = {"requests": 0, "sets": 0, "set_errors": 0, "unsol": 0,
                      "tx": 0, "tx_timeout": 0, "reverts": 0}
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_unsol = 0.0
        self.last_set: dict | None = None

    # -------------------------------------------------------------- model
    def current_limit_a(self) -> float:
        raw = self.online_i_raw if self.online else self.default_i_raw
        try:
            a = R.CURRENT_SCALES[self.scale].decode(raw, self.nominal_a)
        except ValueError:
            a = 0.0
        return min(a, self.max_current_a)

    def setpoint_v(self) -> float:
        return self.online_v if self.online else self.default_v

    def measure(self) -> tuple[float, float]:
        """(output voltage, output current) under the configured load."""
        if self.standby or not self.ac_on:
            return 0.0, 0.0
        v = self.setpoint_v()
        limit = self.current_limit_a()
        if self.load_ohm:
            i = v / self.load_ohm
            if i > limit:
                i = limit
                v = i * self.load_ohm
        elif self.load_w:
            i = self.load_w / v if v else 0.0
            if i > limit:
                i = limit            # constant-power load cannot be served: V sags
                v = max(0.0, self.load_w / i) if i else 0.0
                v = min(v, self.setpoint_v())
        elif self.battery_v is not None:
            r_int = 0.05
            i = max(0.0, (v - self.battery_v) / r_int)
            if i > limit:
                i = limit
                v = self.battery_v + i * r_int
        else:
            i = 0.0
        return round(v, 3), round(i, 3)

    # ---------------------------------------------------------------- io
    def _tx(self, can_id: int, data: bytes, decoded=None) -> None:
        msg = CanMsg(time.perf_counter(), can_id, data, ext=True)
        acked = True
        try:
            self.link.send(msg, timeout=0.3)
        except CanTxTimeout:
            acked = False
            self.stats["tx_timeout"] += 1
        except CanError as e:
            self.on_event(f"[{self.chan}] tx failed: {e}")
            return
        self.stats["tx"] += 1
        log_can(self.session, self.chan, "tx", msg,
                decoded=decoded or R.decode(msg, self.nominal_a), flow="PSU->CTL",
                acked=acked, mode="sim")

    def _status_frames(self) -> list[tuple[int, bytes]]:
        v, i = self.measure()
        limit_ratio = self.current_limit_a() / self.nominal_a
        p_out = v * i
        eff = 0.95 if p_out > 100 else 0.80
        vals = {
            0x0E: self.hours, 0x70: p_out / eff, 0x71: 50.0, 0x72: (p_out / eff) / 230.0,
            0x73: p_out, 0x74: eff, 0x75: v, 0x76: limit_ratio, 0x78: 230.0,
            0x7F: 35.0 + p_out / 150, 0x80: 28.0, 0x81: i, 0x82: i,
        }
        out = []
        for reg in STATUS_ORDER:
            raw = int(round(vals[reg] * (1 if reg == 0x0E else 1024)))
            out.append((reply_id(CMD_DATA, self.addr, True),
                        bytes([0x01, reg, 0, 0]) + raw.to_bytes(4, "big")))
        status = 0
        if self.standby:
            status |= 1 << 9                  # "standby via pin" (BL comment)
        # esphome reads bytes 2-5 as a word and takes bit 29 = byte 2 bit 5
        # as "input power failure"
        data = bytes([0x01, 0x83, 0 if self.ac_on else 0x20, 0]) + status.to_bytes(4, "big")
        out.append((reply_id(CMD_DATA, self.addr, False), data))
        return out

    def _info_frames(self) -> list[tuple[int, bytes]]:
        nominal_raw = int(self.nominal_a * 2) & 0x3FF
        char = bytes([0, 1, 0, 0]) + ((0x40 << 24) | (nominal_raw << 16) | 0x0E27).to_bytes(4, "big")
        serial = bytes.fromhex("0000000000A1")
        frames = [
            char,
            bytes([0, 2]) + serial,
            bytes([0, 3]) + b"210231",
            bytes([0, 4]) + b"SIM001",
            bytes([0, 5, 0x01, 0x00, 0x03, 0x02, 0x02, 0x01]),
            bytes([0, 6, 0x01, 0x01, 0, 0, 0, 0]),
        ]
        return [(reply_id(CMD_INFO, self.addr, i < len(frames) - 1), f)
                for i, f in enumerate(frames)]

    def _elabel_frames(self) -> list[tuple[int, bytes]]:
        text = ELABEL_TEXT.encode("ascii")
        chunks = [text[i:i + 6] for i in range(0, len(text), 6)]
        return [(reply_id(CMD_ELABEL, self.addr, i < len(chunks) - 1),
                 bytes([0, 0]) + c.ljust(6, b"\0")) for i, c in enumerate(chunks)]

    def _handle_set(self, data: bytes) -> None:
        d = bytes(data).ljust(8, b"\0")
        reg = d[1]
        name = R.SET_REG_BY_NUM.get(reg)
        err = False
        raw = int.from_bytes(d[4:8], "big")
        if name == "online_voltage":
            v = raw / 1024
            err = not R.voltage_in_range(name, v)
            if not err:
                self.online_v = v
        elif name == "default_voltage":
            v = raw / 1024
            err = not R.voltage_in_range(name, v)
            if not err:
                self.default_v = v
        elif name == "ovp_voltage":
            v = raw / 1024
            err = not R.voltage_in_range(name, v)
            if not err:
                self.ovp_v = v
        elif name in ("online_current", "default_current"):
            limit_raw = {"ratio1024": 1250, "x20": 1250, "x30": 1875}[self.scale]
            err = raw > limit_raw
            if not err:
                if name == "online_current":
                    self.online_i_raw = raw
                else:
                    self.default_i_raw = raw
        elif name == "standby":
            self.standby = bool(d[3])
        elif name == "fan_max":
            self.fan_max = bool(d[3])
        else:
            err = True
        self.stats["sets"] += 1
        if err:
            self.stats["set_errors"] += 1
        self.last_set = {"name": name, "raw": raw, "error": err}
        ack = (bytes([R.ACK_ERROR_BYTE]) if err else bytes([d[0]])) + d[1:]
        self._tx(reply_id(CMD_SET, self.addr, False), ack)

    def _handle(self, msg: CanMsg) -> None:
        if msg.echo or msg.err or not msg.ext:
            return
        hid = HuaweiId.unpack(msg.can_id)
        if not hid.is_huawei or not hid.to_psu or hid.cmd in (CMD_UNSOL,):
            return
        if hid.addr not in (0, self.addr):
            return
        log_can(self.session, self.chan, "rx", msg, decoded=R.decode(msg, self.nominal_a),
                flow="CTL->PSU", mode="sim")
        self.last_cmd_t = self.clock()
        if not self.online:
            self.online = True
            self.online_v, self.online_i_raw = self.default_v, self.default_i_raw
        if hid.cmd == CMD_DATA:
            self.stats["requests"] += 1
            for cid, data in self._status_frames():
                self._tx(cid, data)
        elif hid.cmd == CMD_INFO:
            for cid, data in self._info_frames():
                self._tx(cid, data)
        elif hid.cmd == CMD_ELABEL:
            for cid, data in self._elabel_frames():
                self._tx(cid, data)
        elif hid.cmd == CMD_SET:
            self._handle_set(msg.data)
        elif hid.cmd == CMD_GET:
            reg = bytes(msg.data)[1] if len(msg.data) > 1 else 0
            for cid, data in self._status_frames():
                if data[1] == reg:
                    self._tx(reply_id(CMD_GET, self.addr, False), data)

    def _tick(self) -> None:
        now = self.clock()
        if self.online and now - self.last_cmd_t > self.timeout_s:
            self.online = False
            self.stats["reverts"] += 1
            self.session.note(f"psu-sim addr {self.addr}: no command for {self.timeout_s:.0f}s "
                              f"- reverting to defaults ({self.default_v} V)", self.chan)
            self.on_event(f"[{self.chan}] OFFLINE: reverted to defaults")
        if now - self._last_unsol >= self.unsol_period_s:
            self._last_unsol = now
            v, i = self.measure()
            load_raw = int(round(i / self.nominal_a * 1024))
            data = bytes([0, 1, 0, 1 if (self.standby or not self.ac_on) else 0, 0, 0]) \
                + load_raw.to_bytes(2, "big")
            self.stats["unsol"] += 1
            self._tx(HuaweiId(PROTO_PSU, self.addr, CMD_UNSOL, False).pack(), data)

    # ---------------------------------------------------------- lifecycle
    def run(self, duration: float | None = None) -> dict:
        self.session.note(f"huawei-psu-sim start addr {self.addr} nominal {self.nominal_a} A "
                          f"scale {self.scale} on {self.link.name}", self.chan)
        t_end = self.clock() + duration if duration else None
        while not self.stop_event.is_set():
            if t_end and self.clock() >= t_end:
                break
            msg = self.link.recv(timeout=0.05)
            if msg is not None:
                self._handle(msg)
            self._tick()
        self.session.note(f"huawei-psu-sim stop stats={self.stats}", self.chan)
        return self.stats

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="huawei-psu-sim")
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
