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
        # write_timeout: a write must never block forever (FTDI drivers can
        # stall a write/flush indefinitely if the device wedges).
        self.ser = serial.Serial(port, baud, bytesize=8, parity="N",
                                 stopbits=1, timeout=0.05, write_timeout=1.0,
                                 rtscts=False, dsrdtr=False, xonxoff=False)
        self.name = name or port
        self.port = port
        # stall watchdog bookkeeping: (kind, started_at) while a blocking
        # serial call is in progress; the watchdog cancels calls that exceed
        # STALL_SECONDS so a wedged driver cannot hang the tool.
        self._inflight: tuple[str, float] | None = None
        self.write_timeouts = 0
        _watchdog.register(self)
        # None = unknown (can be determined via detect_echo on a quiet bus)
        self.echo_suppress = echo_suppress
        self._pushback = b""

    def set_baud(self, baud: int) -> None:
        if self.ser.baudrate != baud:
            self.ser.baudrate = baud

    @property
    def baud(self) -> int:
        try:
            return self.ser.baudrate
        except Exception:
            return getattr(self, "_last_baud", 0)

    def reopen(self) -> bool:
        """Re-create the port after a USB re-enumeration ('Access is denied',
        'ClearCommError failed'). Returns True on success."""
        baud = getattr(self, "_last_baud", None) or 9600
        try:
            baud = self.ser.baudrate or baud
        except Exception:
            pass
        self._last_baud = baud
        try:
            self.ser.close()
        except Exception:
            pass
        try:
            self.ser = self._serial_mod.Serial(
                self.port, baud, bytesize=8, parity="N", stopbits=1,
                timeout=0.05, write_timeout=1.0,
                rtscts=False, dsrdtr=False, xonxoff=False)
            self._pushback = b""
            return True
        except Exception:
            return False

    def read(self, timeout: float = 0.05) -> Chunk | None:
        if self._pushback:
            data, self._pushback = self._pushback, b""
            return Chunk(time.perf_counter(), data)
        # NEVER set ser.timeout per call: on Windows the setter runs a full
        # port reconfigure (SetCommState) and bytes arriving in that window
        # can be dropped (seen as intermittent 'silent' replies on a real
        # US5000). The port keeps a fixed 50ms timeout; longer waits loop.
        deadline = time.perf_counter() + timeout
        self._inflight = ("read", time.perf_counter())
        try:
            while True:
                first = self.ser.read(1)
                if first:
                    n = self.ser.in_waiting
                    data = first + (self.ser.read(n) if n else b"")
                    return Chunk(time.perf_counter(), data)
                if time.perf_counter() >= deadline:
                    return None
        finally:
            self._inflight = None

    def write(self, data: bytes) -> None:
        self._inflight = ("write", time.perf_counter())
        try:
            self.ser.write(data)   # bounded by write_timeout; no flush()
        except self._serial_mod.SerialTimeoutException:
            # TX did not drain within write_timeout: the driver/device is
            # not accepting bytes (wedged adapter, flow-control line held,
            # port not really connected). Record it and let the caller's
            # normal "no reply" path proceed instead of hanging or crashing.
            self.write_timeouts += 1
            print(f"[{self.name}] WRITE TIMEOUT - TX stalled "
                  f"(#{self.write_timeouts}); bytes not sent", flush=True)
        finally:
            self._inflight = None
        if self.echo_suppress:
            self._eat_echo(data)

    def _eat_echo(self, sent: bytes) -> None:
        """Read back exactly the echoed bytes; anything beyond (or different)
        is pushed back for the next read."""
        deadline = time.perf_counter() + 0.05 + len(sent) * 10 / self.baud * 2
        got = b""
        while len(got) < len(sent) and time.perf_counter() < deadline:
            b = self.ser.read(len(sent) - len(got))   # fixed 50ms port timeout
            if not b:
                continue
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
            got = b""
            deadline = time.perf_counter() + 0.2
            while len(got) < len(probe) and time.perf_counter() < deadline:
                got += self.ser.read(len(probe) - len(got))
            self.echo_suppress = got == probe
            return self.echo_suppress
        finally:
            if prev is not None:
                self.echo_suppress = prev

    def flush_input(self) -> None:
        self._pushback = b""
        self.ser.reset_input_buffer()

    def close(self) -> None:
        _watchdog.unregister(self)
        try:
            self.ser.close()
        except Exception:
            pass


class _StallWatchdog:
    """Cancels serial calls that block longer than STALL_SECONDS.

    Windows serial drivers (FTDI included) can wedge a read/write in kernel
    mode with no timeout honoured; the process then cannot even be killed
    until the device is unplugged. pyserial exposes CancelIoEx via
    cancel_read()/cancel_write(); a daemon thread polls registered links and
    fires them when a call has overrun. The cancelled call raises/returns
    empty, the tool's own timeout logic then proceeds normally.
    """

    STALL_SECONDS = 4.0
    POLL_S = 0.25

    def __init__(self):
        self._links: set = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.stalls: list[str] = []

    def register(self, link) -> None:
        with self._lock:
            self._links.add(link)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="serial-stall-watchdog")
                self._thread.start()

    def unregister(self, link) -> None:
        with self._lock:
            self._links.discard(link)

    def _run(self) -> None:
        while True:
            time.sleep(self.POLL_S)
            with self._lock:
                links = list(self._links)
            now = time.perf_counter()
            for link in links:
                inflight = getattr(link, "_inflight", None)
                if not inflight:
                    continue
                kind, t0 = inflight
                if now - t0 > self.STALL_SECONDS:
                    msg = (f"[watchdog] {link.name}: serial {kind} stalled "
                           f"{now - t0:.1f}s - cancelling (driver wedge?)")
                    self.stalls.append(msg)
                    print(msg, flush=True)
                    try:
                        if kind == "read":
                            link.ser.cancel_read()
                        else:
                            link.ser.cancel_write()
                    except Exception as e:  # cancel unsupported/failed
                        print(f"[watchdog] cancel failed: {e}", flush=True)
                    link._inflight = None


_watchdog = _StallWatchdog()


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
    """spec = COM port / /dev/tty path / 'usb:A' (FTDI over libusb, see
    core/ftdi_usb.py). 'LOOP' is handled by callers that own a loopback
    pair; here it is an error."""
    if spec.upper() == "LOOP":
        raise ValueError("LOOP links must be created via loopback_pair()")
    if spec.lower().startswith("usb:"):
        from .ftdi_usb import FtdiUsbLink   # Android/Termux: FTDI over libusb
        return FtdiUsbLink(spec, baud, name=name)
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
