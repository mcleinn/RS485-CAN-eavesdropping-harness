"""Virtual 2-channel RS485 analyzer - a software model of the Waveshare
USB TO 2CH RS485 so the full tool suite (selftest, sniff, bridge, sims,
identify) runs with zero hardware.

Model:
- VirtualBus: a shared 2-wire medium. Every write by one endpoint is seen
  by all *other* endpoints on the bus (multi-drop), and by the writer
  itself if its `echo` flag is set (adapters differ in real life, so both
  behaviours are testable).
- Baud awareness: each endpoint has a baud; if writer and reader disagree,
  the reader receives garbled bytes (like a real UART at the wrong rate) -
  which is exactly what the wrong-baud diagnosis has to detect.
- VirtualAnalyzer: ch1 on bus1, ch2 on bus2 (independent, like the two
  Waveshare channels). `cross_wire()` joins both buses into one - the
  physical self-test setup.
"""
from __future__ import annotations

import queue
import threading
import time

from .transport import Link, Chunk


def garble(data: bytes, ratio: tuple[int, int]) -> bytes:
    """Rough model of reading UART data at the wrong baud rate: byte count
    scales with the rate ratio and values are framing junk (00/FF-heavy)."""
    tx_baud, rx_baud = ratio
    n = max(1, int(len(data) * min(tx_baud, rx_baud) / max(tx_baud, rx_baud)))
    out = bytearray()
    acc = 0
    for i in range(n):
        acc = (acc * 31 + data[i % len(data)] + i) & 0xFFFF
        m = acc % 5
        if m == 0:
            out.append(0x00)
        elif m == 1:
            out.append(0xFF)
        elif m == 2:
            out.append(0x80)
        else:
            out.append(acc & 0xFF)
    return bytes(out)


class VirtualBusEnd(Link):
    def __init__(self, bus: "VirtualBus", name: str, baud: int = 9600,
                 echo: bool = False):
        self.bus = bus
        self.name = name
        self.baud = baud
        self.echo = echo
        self._rx: queue.Queue[bytes] = queue.Queue()

    def read(self, timeout: float = 0.05) -> Chunk | None:
        try:
            data = self._rx.get(timeout=timeout)
        except queue.Empty:
            return None
        try:
            while True:
                data += self._rx.get_nowait()
        except queue.Empty:
            pass
        return Chunk(time.perf_counter(), data)

    def write(self, data: bytes) -> None:
        self.bus.transmit(self, data)

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def flush_input(self) -> None:
        try:
            while True:
                self._rx.get_nowait()
        except queue.Empty:
            pass

    def detect_echo(self, probe: bytes = b"\x55\xAA\x0F\xF0") -> bool:
        self.flush_input()
        self.write(probe)
        got = b""
        deadline = time.perf_counter() + 0.2
        while len(got) < len(probe) and time.perf_counter() < deadline:
            chunk = self.read(timeout=0.05)
            if chunk:
                got += chunk.data
        return got == probe

    def _deliver(self, sender_baud: int, data: bytes) -> None:
        if sender_baud != self.baud:
            data = garble(data, (sender_baud, self.baud))
        self._rx.put(data)


class VirtualBus:
    def __init__(self, name: str = "bus"):
        self.name = name
        self._lock = threading.Lock()
        self.ends: list[VirtualBusEnd] = []
        self.joined: "VirtualBus | None" = None

    def end(self, name: str, baud: int = 9600, echo: bool = False) -> VirtualBusEnd:
        e = VirtualBusEnd(self, name, baud, echo)
        with self._lock:
            self.ends.append(e)
        return e

    def _all_ends(self) -> list[VirtualBusEnd]:
        ends = list(self.ends)
        if self.joined is not None:
            ends += self.joined.ends
        return ends

    def transmit(self, sender: VirtualBusEnd, data: bytes) -> None:
        with self._lock:
            targets = self._all_ends()
        for e in targets:
            if e is sender:
                if e.echo:
                    e._rx.put(data)  # local echo is never baud-garbled
            else:
                e._deliver(sender.baud, data)

    def join(self, other: "VirtualBus") -> None:
        """Wire two buses together (A-A, B-B): one shared medium."""
        self.joined = other
        other.joined = self

    def unjoin(self) -> None:
        if self.joined is not None:
            self.joined.joined = None
            self.joined = None


class VirtualAnalyzer:
    """Two independent channels like the Waveshare USB TO 2CH RS485.

    analyzer.ch1 / analyzer.ch2 are the adapter-side Links (what sniff,
    bridge, selftest, identify or a simulator would open). Attach devices
    with analyzer.bus1.end('inverter') / analyzer.bus2.end('battery').
    """

    def __init__(self, echo: bool = False):
        self.bus1 = VirtualBus("bus1")
        self.bus2 = VirtualBus("bus2")
        self.ch1 = self.bus1.end("ch1", echo=echo)
        self.ch2 = self.bus2.end("ch2", echo=echo)

    def cross_wire(self) -> None:
        """Join ch1's and ch2's buses - the self-test wiring."""
        self.bus1.join(self.bus2)

    def un_cross_wire(self) -> None:
        self.bus1.unjoin()
