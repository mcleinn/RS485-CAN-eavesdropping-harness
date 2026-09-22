"""Transport layer: serial ports and an in-process loopback pair.

Design notes:
- Blocking pyserial + threads (pyserial-asyncio is unmaintained and polls on
  Windows). At 9600 baud there is enormous headroom.
- Timestamps are taken per read burst with time.perf_counter(); byte-level
  timing through a USB adapter is not trustworthy (latency timer quantizes).
- 2-wire RS485 adapters may receive their own transmission (local echo,
  depends on whether /RE tracks DE on the board). This is detected per port
  and suppressed on write.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass


@dataclass
class Chunk:
    ts: float          # perf_counter at read completion
    data: bytes


class Link:
    """Byte-stream endpoint. Subclasses: SerialLink, LoopbackEnd."""

    name: str = "link"

    def read(self, timeout: float = 0.05) -> Chunk | None:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def set_baud(self, baud: int) -> None:
        pass  # meaningless for loopback

    def flush_input(self) -> None:
        pass

    def close(self) -> None:
        pass


class SerialLink(Link):
    def __init__(self, port: str, baud: int = 9600, name: str | None = None,
                 echo_suppress: bool | None = None):
        import serial  # deferred so loopback-only use needs no pyserial
        self._serial_mod = serial
        self.ser = serial.Serial(port, baud, bytesize=8, parity="N",
                                 stopbits=1, timeout=0.05)
        self.name = name or port
        self.port = port
        # None = unknown (can be determined via detect_echo on a quiet bus)
        self.echo_suppress = echo_suppress
        self._pushback = b""

    def set_baud(self, baud: int) -> None:
        if self.ser.baudrate != baud:
            self.ser.baudrate = baud

    @property
    def baud(self) -> int:
        return self.ser.baudrate

    def read(self, timeout: float = 0.05) -> Chunk | None:
        if self._pushback:
            data, self._pushback = self._pushback, b""
            return Chunk(time.perf_counter(), data)
        self.ser.timeout = timeout
        first = self.ser.read(1)
        if not first:
            return None
        n = self.ser.in_waiting
        data = first + (self.ser.read(n) if n else b"")
        return Chunk(time.perf_counter(), data)

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()
        if self.echo_suppress:
            self._eat_echo(data)

    def _eat_echo(self, sent: bytes) -> None:
        """Read back exactly the echoed bytes; anything beyond (or different)
        is pushed back for the next read."""
        deadline = time.perf_counter() + 0.05 + len(sent) * 10 / self.baud * 2
        got = b""
        while len(got) < len(sent) and time.perf_counter() < deadline:
            self.ser.timeout = max(0.0, deadline - time.perf_counter())
            b = self.ser.read(len(sent) - len(got))
            if not b:
                break
            got += b
        if got != sent[:len(got)]:
            self._pushback += got  # not an echo after all - keep it

    def detect_echo(self, probe: bytes = b"\x55\xAA\x0F\xF0") -> bool:
        """Requires a quiet bus. Writes probe bytes, checks if they come back."""
        self.flush_input()
        prev = self.echo_suppress
        self.echo_suppress = False
        try:
            self.ser.write(probe)
            self.ser.flush()
            self.ser.timeout = 0.2
            got = self.ser.read(len(probe))
            self.echo_suppress = got == probe
            return self.echo_suppress
        finally:
            if prev is not None:
                self.echo_suppress = prev

    def flush_input(self) -> None:
        self._pushback = b""
        self.ser.reset_input_buffer()

    def close(self) -> None:
        self.ser.close()


class LoopbackEnd(Link):
    """One end of an in-process pair; models a shared 2-wire bus enough for
    sim-vs-sim testing (a write appears on the peer, optionally echoed)."""

    def __init__(self, name: str, echo: bool = False):
        self.name = name
        self._rx: queue.Queue[bytes] = queue.Queue()
        self.peer: "LoopbackEnd | None" = None
        self.echo = echo
        self._lock = threading.Lock()

    def read(self, timeout: float = 0.05) -> Chunk | None:
        try:
            data = self._rx.get(timeout=timeout)
        except queue.Empty:
            return None
        # drain anything else immediately available into one burst
        try:
            while True:
                data += self._rx.get_nowait()
        except queue.Empty:
            pass
        return Chunk(time.perf_counter(), data)

    def write(self, data: bytes) -> None:
        with self._lock:
            if self.peer is not None:
                self.peer._rx.put(data)
            if self.echo:
                self._rx.put(data)

    def flush_input(self) -> None:
        try:
            while True:
                self._rx.get_nowait()
        except queue.Empty:
            pass


def loopback_pair(name_a: str = "simA", name_b: str = "simB",
                  echo: bool = False) -> tuple[LoopbackEnd, LoopbackEnd]:
    a, b = LoopbackEnd(name_a, echo), LoopbackEnd(name_b, echo)
    a.peer, b.peer = b, a
    return a, b


def open_link(spec: str, baud: int = 9600, name: str | None = None) -> Link:
    """spec = COM port / /dev/tty path. 'LOOP' is handled by callers that
    own a loopback pair; here it is an error."""
    if spec.upper() == "LOOP":
        raise ValueError("LOOP links must be created via loopback_pair()")
    return SerialLink(spec, baud, name=name)


@dataclass
class BusEvent:
    """A framed unit extracted from the byte stream."""
    ts: float
    kind: str          # "frame" (Pylontech ~..\r) | "raw" (unframed bytes)
    data: bytes


class FrameAccumulator:
    """Splits a byte stream into Pylontech frames + leftover raw chunks.

    Bytes that never form a frame (no '~', or no '\\r' within max_frame
    bytes / gap_flush seconds) are emitted as kind="raw" so wrong-baud
    garbage and foreign protocols (e.g. Modbus RTU) stay visible.
    """

    def __init__(self, gap_flush_s: float = 0.25, max_frame: int = 2048):
        self.buf = b""
        self.ts_first: float | None = None
        self.gap_flush_s = gap_flush_s
        self.max_frame = max_frame

    def feed(self, chunk: Chunk) -> list[BusEvent]:
        if not self.buf:
            self.ts_first = chunk.ts
        self.last_ts = chunk.ts
        self.buf += chunk.data
        events: list[BusEvent] = []
        while True:
            cr = self.buf.find(b"\r")
            if cr < 0:
                break
            piece, self.buf = self.buf[:cr + 1], self.buf[cr + 1:]
            tilde = piece.rfind(b"~")
            if tilde > 0:
                events.append(BusEvent(chunk.ts, "raw", piece[:tilde]))
                piece = piece[tilde:]
            if piece.startswith(b"~"):
                events.append(BusEvent(chunk.ts, "frame", piece))
            elif piece:
                events.append(BusEvent(chunk.ts, "raw", piece))
            self.ts_first = chunk.ts if self.buf else None
        if len(self.buf) > self.max_frame:
            events.append(BusEvent(chunk.ts, "raw", self.buf))
            self.buf = b""
        return events

    def flush_if_stale(self, now: float) -> BusEvent | None:
        """Emit buffered non-frame bytes after a quiet gap."""
        if self.buf and self.ts_first is not None and now - self.last_ts > self.gap_flush_s:
            ev = BusEvent(self.ts_first, "raw", self.buf)
            self.buf = b""
            self.ts_first = None
            return ev
        return None
