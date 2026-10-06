"""Minimal Seplos BMS 3.0 service-interface responder (Modbus RTU) for
hardware-free tests: serves function 0x04 reads and 0x10 writes on the
PIA/PIB/SPA blocks and 0x01 reads of the PIC/SFA status coils, answers any other function with ILLEGAL FUNCTION and
ignores everything that is not addressed to it (e.g. Pylontech ASCII).

Default register contents are the example read-out quoted in the register
documentation (a real 16S pack), so decoded values are realistic.
"""
from __future__ import annotations

import threading
import time

from ..core.transport import Link
from ..analyzer.classify import crc16_modbus

EXAMPLE_PIA = [5236, 1301, 3800, 30400, 64, 125, 1000, 2, 3272, 2837, 3275,
               3268, 2845, 2831, 0, 180, 180, 1000]
EXAMPLE_PIB = [3273, 3273, 3268, 3272, 3274, 3274, 3275, 3275, 3274, 3273,
               3273, 3270, 3271, 3271, 3270, 3272, 2842, 2833, 2831, 2845,
               2731, 2731, 2731, 2731, 2833, 2829]
EXAMPLE_SPA = [4, 16, 5400, 5600, 5400, 5760, 4800, 4640, 4800, 4320, 3400,
               3500, 3400, 3650, 3100, 2900, 3100, 2700, 2000, 1000, 500, 203,
               205, 210, 100, 300, 300, 65333, 65331, 65326, 100, 65186, 300,
               65236, 106, 600, 5, 3000, 205, 10, 3500, 3400, 1500, 1000, 800,
               200, 30, 3201, 3231, 3231, 3281, 2781, 2751, 2731, 2631, 3231,
               3281, 3281, 3331, 2761, 2631, 2731, 2581, 3201, 3231, 3281,
               3331, 2761, 2731, 2731, 2631, 3581, 3681, 3581, 3831, 2831,
               2731, 3231, 2731, 10, 3400, 50, 30, 960, 150, 100, 70, 50,
               30400, 30400, 3800, 48, 60, 240, 10, 7, 0, 13, 0, 500, 300,
               5760, 180, 65356, 5, 8]


def _with_crc(pdu: bytes) -> bytes:
    crc = crc16_modbus(pdu)
    return pdu + bytes([crc & 0xFF, crc >> 8])


class SeplosNativeSim:
    def __init__(self, link: Link, slave: int = 0, accept_writes: bool = True):
        self.link = link
        self.slave = slave
        self.accept_writes = accept_writes
        self.regs: dict[int, int] = {}
        for base, vals in ((0x1000, EXAMPLE_PIA), (0x1100, EXAMPLE_PIB),
                           (0x1300, EXAMPLE_SPA)):
            for i, v in enumerate(vals):
                self.regs[base + i] = v
        # status coils (PIC 0x1200, 144) and function switches (SFA 0x1400, 80)
        self.coils: dict[int, bool] = {0x1200 + i: False for i in range(144)}
        self.coils.update({0x1400 + i: False for i in range(80)})
        for addr in (0x1241, 0x1278, 0x1279):     # Charge, both FETs on
            self.coils[addr] = True
        self.writes: list[tuple[int, int]] = []
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _handle(self, buf: bytes) -> tuple[bytes | None, int]:
        """Returns (reply or None, bytes consumed); 0 consumed = need more."""
        if buf[0] != self.slave:
            return None, 1
        if len(buf) < 8:
            return None, 0
        fn = buf[1]
        if fn == 0x04:
            frame = buf[:8]
            if _with_crc(frame[:6]) != frame:
                return None, 1
            start, count = (frame[2] << 8) | frame[3], (frame[4] << 8) | frame[5]
            if any(start + i not in self.regs for i in range(count)) or not count:
                return _with_crc(bytes([self.slave, 0x84, 0x02])), 8
            data = b"".join(self.regs[start + i].to_bytes(2, "big")
                            for i in range(count))
            return _with_crc(bytes([self.slave, 0x04, len(data)]) + data), 8
        if fn == 0x01:
            frame = buf[:8]
            if _with_crc(frame[:6]) != frame:
                return None, 1
            start, count = (frame[2] << 8) | frame[3], (frame[4] << 8) | frame[5]
            if any(start + i not in self.coils for i in range(count)) or not count:
                return _with_crc(bytes([self.slave, 0x81, 0x02])), 8
            data = bytearray((count + 7) // 8)
            for i in range(count):
                if self.coils[start + i]:
                    data[i // 8] |= 1 << (i % 8)
            return _with_crc(bytes([self.slave, 0x01, len(data)]) + bytes(data)), 8
        if fn == 0x10:
            n = buf[6]
            total = 7 + n + 2
            if len(buf) < total:
                return None, 0
            frame = buf[:total]
            if _with_crc(frame[:-2]) != frame:
                return None, 1
            start, count = (frame[2] << 8) | frame[3], (frame[4] << 8) | frame[5]
            if not self.accept_writes or n != 2 * count or \
                    any(start + i not in self.regs for i in range(count)):
                return _with_crc(bytes([self.slave, 0x90, 0x02])), total
            for i in range(count):
                v = int.from_bytes(frame[7 + 2 * i:9 + 2 * i], "big")
                self.regs[start + i] = v
                self.writes.append((start + i, v))
            return _with_crc(frame[:6]), total
        if _with_crc(buf[:6]) == buf[:8]:      # well-formed, unsupported fn
            return _with_crc(bytes([self.slave, fn | 0x80, 0x01])), 8
        return None, 1

    def _run(self) -> None:
        buf = b""
        last = time.perf_counter()
        while not self.stop_event.is_set():
            chunk = self.link.read(timeout=0.02)
            now = time.perf_counter()
            if chunk:
                buf += chunk.data
                last = now
            elif buf and now - last > 0.05:     # inter-frame silence: resync
                buf = b""
            while buf:
                reply, used = self._handle(buf)
                if used == 0:
                    break
                buf = buf[used:]
                if reply:
                    self.link.write(reply)

    def start(self) -> "SeplosNativeSim":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="seplos-native-sim")
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
