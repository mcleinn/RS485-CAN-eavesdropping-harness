"""FTDI adapters over raw USB (libusb) - for Android/Termux without root.

Android exposes a USB device to an app only as an already-open file
descriptor (Termux: `termux-usb -r -e <cmd> /dev/bus/usb/BBB/DDD` runs
<cmd> with the fd as its last argument). There is no /dev/ttyUSB*, so
pyserial cannot be used; instead this module drives the FTDI chip directly
with the python-libusb1 binding (`pip install libusb1`, `pkg install libusb`).

Port spec (accepted wherever a COM port is accepted):
    usb:A           channel A of the device whose fd is in $RSH_USB_FD
    usb:B           channel B (FT2232H / FT4232H only)
    usb:<fd>:A      explicit fd
    usb:<fd>        single-port chip (FT232R/FT232H) or channel A
`rsh-usb` (tools/termux) sets $RSH_USB_FD via termux-usb and runs rsh.

Only the pieces of the FTDI protocol needed for plain 8N1 UART traffic are
implemented: reset/purge, baud, line format, flow control off, latency
timer, bulk read with status-byte stripping, bulk write. Works on Linux too
(fd from os.open("/dev/bus/usb/...")); the pure helpers are unit-tested
without hardware.
"""
from __future__ import annotations

import atexit
import os
import threading
import time

from .transport import Chunk, Link

VENDOR_FTDI = 0x0403
# FTDI vendor requests
SIO_RESET = 0x00
SIO_SET_MODEM_CTRL = 0x01
SIO_SET_FLOW_CTRL = 0x02
SIO_SET_BAUDRATE = 0x03
SIO_SET_DATA = 0x04
SIO_SET_LATENCY = 0x09
SIO_RESET_SIO, SIO_RESET_PURGE_RX, SIO_RESET_PURGE_TX = 0, 1, 2

# product id -> (number of UART channels, "H" high-speed family)
FTDI_PRODUCTS = {
    0x6001: (1, False),   # FT232R / FT232BM / FT245
    0x6010: (2, True),    # FT2232H / FT2232D
    0x6011: (4, True),    # FT4232H
    0x6014: (1, True),    # FT232H
    0x6015: (1, False),   # FT-X series (FT230X, FT231X)
}
CHANNEL_NAMES = "ABCD"
LATENCY_MS = 4
IO_ERROR_PAUSE_S = 0.005          # after a transient bulk-read error
IO_ERRORS_FATAL = 200             # consecutive errors before giving up
CTRL_RETRIES = 8                  # control transfers: retries on USB error


# ----------------------------------------------------------- pure helpers
def ftdi_baud_words(baud: int, h_type: bool = True) -> tuple[int, int]:
    """(wValue, wIndex-high) for SIO_SET_BAUDRATE, FTDI sub-integer divisor
    encoding. Base clock 3 MHz (all chips, baud <= 3 Mbaud); the H family
    could use 12 MHz for >3 Mbaud, not needed here."""
    if baud <= 0:
        raise ValueError("baud must be positive")
    div = 3_000_000 / baud
    if div < 1.25:                 # AN232B-05: divisor 0 = 3 Mbaud,
        idiv, frac_code = 0, 0     # divisor 1 = 2 Mbaud, nothing in between
    elif div < 1.75:
        idiv, frac_code = 1, 0
    else:
        idiv = int(div)
        frac = div - idiv
        frac_code = (0, 3, 2, 4, 1, 5, 6, 7)[min(7, int(round(frac * 8)))]
    value = (idiv & 0x3FFF) | (frac_code << 14)
    index = (value >> 16) & 0xFFFF
    return value & 0xFFFF, index


def strip_status(buf: bytes, max_packet: int) -> bytes:
    """FTDI prefixes every USB packet (max_packet bytes, 64 FS / 512 HS)
    with 2 modem-status bytes. Remove them from a bulk read result that may
    span several packets."""
    out = bytearray()
    for i in range(0, len(buf), max_packet):
        out += buf[i + 2:i + max_packet]
    return bytes(out)


def parse_usb_spec(spec: str) -> tuple[int, int]:
    """'usb:A' / 'usb:12:B' / 'usb:12' -> (fd, channel index)."""
    if not spec.lower().startswith("usb:"):
        raise ValueError(f"not a usb: spec: {spec}")
    parts = spec[4:].split(":")
    fd_s = None
    chan = "A"
    if len(parts) == 1:
        if parts[0].isdigit():
            fd_s = parts[0]
        elif parts[0]:
            chan = parts[0]
    elif len(parts) == 2:
        fd_s, chan = parts
    else:
        raise ValueError(f"bad usb: spec: {spec}")
    if fd_s is None:
        fd_s = os.environ.get("RSH_USB_FD")
        if not fd_s:
            raise ValueError("usb: spec without fd and $RSH_USB_FD not set - "
                             "run through tools/termux/rsh-usb (termux-usb -e)")
    chan = chan.upper()
    if chan.isdigit():
        idx = int(chan)
    elif chan in CHANNEL_NAMES:
        idx = CHANNEL_NAMES.index(chan)
    else:
        raise ValueError(f"bad channel {chan!r} (A-D or 0-3)")
    return int(fd_s), idx


# ----------------------------------------------------------- device cache
class _Device:
    """One libusb handle per fd, shared by the channel links."""
    _by_fd: dict[int, "_Device"] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, fd: int) -> "_Device":
        with cls._lock:
            d = cls._by_fd.get(fd)
            if d is None:
                d = cls._by_fd[fd] = _Device(fd)
            return d

    def __init__(self, fd: int):
        import usb1   # deferred: only needed on Android/Linux-libusb hosts
        self.usb1 = usb1
        self.fd = fd
        # no device discovery: Android forbids scanning /dev/bus/usb
        self.ctx = usb1.USBContext(with_device_discovery=False)
        self.ctx.open()
        self.handle = self.ctx.wrapSysDevice(fd)
        dev = self.handle.getDevice()
        self.vid, self.pid = dev.getVendorID(), dev.getProductID()
        if self.vid != VENDOR_FTDI:
            raise OSError(f"USB device {self.vid:04x}:{self.pid:04x} is not an "
                          f"FTDI chip (only FTDI is supported over usb:)")
        self.n_chan, self.h_type = FTDI_PRODUCTS.get(self.pid, (1, False))
        try:
            self.product = dev.getProduct() or f"FTDI {self.pid:04x}"
        except usb1.USBError as e:   # string descriptor fetch failed: not fatal
            self.product = f"FTDI {self.pid:04x} (descriptor read failed: {e})"
        self.max_packet = 512 if self.h_type else 64
        self.claimed: set[int] = set()
        self.io_lock = threading.Lock()
        atexit.register(self._atexit)

    def _atexit(self) -> None:
        """Close through the workaround path even if a caller forgot to,
        otherwise usb1's own exit finalizer trips its assertion."""
        if self.fd in _Device._by_fd:
            for idx in list(self.claimed):
                self.release(idx)
            try:
                self.close_if_unused()
            except Exception:
                pass

    def claim(self, idx: int) -> None:
        if idx >= self.n_chan:
            raise ValueError(f"{self.product}: channel {CHANNEL_NAMES[idx]} "
                             f"does not exist ({self.n_chan} channel(s))")
        if idx in self.claimed:
            return
        try:
            if self.handle.kernelDriverActive(idx):
                self.handle.detachKernelDriver(idx)
        except self.usb1.USBError:
            pass
        self.handle.claimInterface(idx)
        self.claimed.add(idx)

    def release(self, idx: int) -> None:
        if idx in self.claimed:
            try:
                self.handle.releaseInterface(idx)
            except self.usb1.USBError:
                pass
            self.claimed.discard(idx)

    def close_if_unused(self) -> None:
        if self.claimed:
            return
        with _Device._lock:
            if _Device._by_fd.pop(self.fd, None) is None:
                return                       # already closed
        try:
            _close_wrapped_handle(self.handle)
        finally:
            self.ctx.close()


def _close_wrapped_handle(handle) -> None:
    """Close a handle from USBContext.wrapSysDevice().

    python-libusb1 (3.3.1 and 3.4.0) registers the handle's finalizer in the
    device's finalizer dict AND lets the handle close the device; closing
    then re-enters that dict while the handle's own finalizer is still
    listed and trips an assertion before libusb_close() is reached (the fd
    then stays open and Android re-enumerates the adapter). Take the
    handle's finalizer out of the device's dict first; the finalizer's own
    pop() in its finally-clause raises KeyError afterwards, by which time
    libusb_close() has run."""
    try:
        dev = handle.getDevice()
        fdict = getattr(dev, "_finalizer_dict", None)
        if isinstance(fdict, dict):
            for key, fin in list(fdict.items()):
                if fin is handle.close:
                    fdict.pop(key, None)
    except Exception:
        pass
    try:
        handle.close()
    except KeyError:
        pass


class FtdiUsbLink(Link):
    def __init__(self, spec: str, baud: int = 9600, name: str | None = None,
                 echo_suppress: bool | None = None):
        fd, idx = parse_usb_spec(spec)
        self.dev = _Device.get(fd)
        self.dev.claim(idx)
        self.idx = idx
        self.port = idx + 1 if self.dev.n_chan > 1 else 0   # FTDI wIndex port
        self.ep_in, self.ep_out = 0x81 + 2 * idx, 0x02 + 2 * idx
        self.name = name or spec
        self.spec = spec
        self.echo_suppress = echo_suppress
        self._pushback = b""
        self._baud = 0
        self.write_timeouts = 0
        self.io_errors = 0            # transient USB errors ridden through
        self._consecutive_errors = 0
        u = self.dev.usb1
        self._req_out = u.REQUEST_TYPE_VENDOR | u.RECIPIENT_DEVICE | u.ENDPOINT_OUT
        self._ctrl(SIO_RESET, SIO_RESET_SIO)
        # latency timer: 4 ms (not 1 ms as on the PC driver) - every expiry
        # is a USB transaction even with no data, and marginal cables/OTG
        # links drop transactions under that rate; 4 ms is still far below
        # the protocol timings we measure (tens of ms).
        self._ctrl(SIO_SET_LATENCY, LATENCY_MS)
        self._ctrl(SIO_SET_FLOW_CTRL, 0)
        self._ctrl(SIO_SET_DATA, 0x0008)          # 8 data bits, no parity, 1 stop
        self.set_baud(baud)
        self.flush_input()

    # ------------------------------------------------------------ control
    def _ctrl(self, request: int, value: int, index_high: int = 0) -> None:
        u = self.dev.usb1
        for attempt in range(CTRL_RETRIES):
            try:
                with self.dev.io_lock:
                    self.dev.handle.controlWrite(self._req_out, request, value,
                                                 (index_high << 8) | self.port,
                                                 b"", timeout=500)
                return
            except u.USBError as e:          # transient on marginal links
                self.io_errors += 1
                if attempt == CTRL_RETRIES - 1:
                    raise OSError(f"{self.name}: FTDI control request "
                                  f"{request:#x} failed {CTRL_RETRIES}x ({e!r})") from e
                time.sleep(0.01 * (attempt + 1))

    def set_baud(self, baud: int) -> None:
        if baud != self._baud:
            value, index = ftdi_baud_words(baud, self.dev.h_type)
            self._ctrl(SIO_SET_BAUDRATE, value, index)
            self._baud = baud

    @property
    def baud(self) -> int:
        return self._baud

    def reopen(self) -> bool:
        """The fd cannot be re-acquired from inside the process (Android
        hands it over once); a re-enumeration ends the session."""
        return False

    # ----------------------------------------------------------------- io
    def read(self, timeout: float = 0.05) -> Chunk | None:
        if self._pushback:
            data, self._pushback = self._pushback, b""
            return Chunk(time.perf_counter(), data)
        u = self.dev.usb1
        deadline = time.perf_counter() + timeout
        while True:
            try:
                raw = self.dev.handle.bulkRead(self.ep_in, self.dev.max_packet * 4,
                                               timeout=max(1, int(timeout * 1000)))
                self._consecutive_errors = 0
            except u.USBErrorTimeout:
                raw = b""
            except u.USBError as e:
                # LIBUSB_ERROR_IO etc.: seen in bursts on marginal USB links
                # (OTG adapters, worn cables) while the device stays
                # enumerated; ride through, give up only if persistent.
                self.io_errors += 1
                self._consecutive_errors += 1
                if self._consecutive_errors >= IO_ERRORS_FATAL:
                    self._consecutive_errors = 0
                    raise OSError(f"{self.name}: {IO_ERRORS_FATAL} consecutive "
                                  f"USB read errors ({e!r}) - link lost?") from e
                time.sleep(IO_ERROR_PAUSE_S)
                raw = b""
            data = strip_status(raw, self.dev.max_packet)
            if data:
                return Chunk(time.perf_counter(), data)
            if time.perf_counter() >= deadline:
                return None

    def write(self, data: bytes) -> None:
        u = self.dev.usb1
        for attempt in range(CTRL_RETRIES):
            try:
                with self.dev.io_lock:
                    self.dev.handle.bulkWrite(self.ep_out, data, timeout=1000)
                break
            except u.USBErrorTimeout:
                self.write_timeouts += 1
                print(f"[{self.name}] WRITE TIMEOUT - TX stalled "
                      f"(#{self.write_timeouts}); bytes not sent", flush=True)
                return
            except u.USBError:
                self.io_errors += 1
                if attempt == CTRL_RETRIES - 1:
                    self.write_timeouts += 1
                    print(f"[{self.name}] WRITE FAILED after {CTRL_RETRIES} USB "
                          f"errors; bytes not sent", flush=True)
                    return
                time.sleep(0.01 * (attempt + 1))
        if self.echo_suppress:
            self._eat_echo(data)

    def _eat_echo(self, sent: bytes) -> None:
        deadline = time.perf_counter() + 0.05 + len(sent) * 10 / max(self._baud, 300) * 2
        got = b""
        while len(got) < len(sent) and time.perf_counter() < deadline:
            c = self.read(timeout=0.02)
            if c:
                got += c.data
        if got[:len(sent)] != sent[:len(got)]:
            self._pushback += got
        elif len(got) > len(sent):
            self._pushback += got[len(sent):]

    def detect_echo(self, probe: bytes = b"\x55\xAA\x0F\xF0") -> bool:
        self.flush_input()
        prev = self.echo_suppress
        self.echo_suppress = False
        try:
            self.write(probe)
            got = b""
            deadline = time.perf_counter() + 0.2
            while len(got) < len(probe) and time.perf_counter() < deadline:
                c = self.read(timeout=0.05)
                if c:
                    got += c.data
            self.echo_suppress = got == probe
            return self.echo_suppress
        finally:
            if prev is not None:
                self.echo_suppress = prev

    def flush_input(self) -> None:
        self._pushback = b""
        self._ctrl(SIO_RESET, SIO_RESET_PURGE_RX)

    def close(self) -> None:
        self.dev.release(self.idx)
        self.dev.close_if_unused()


def describe_device(fd: int) -> dict:
    """Enumerate the device behind an fd (for `rsh ports` on Termux)."""
    d = _Device.get(fd)
    info = {"device": f"usb:{fd}", "vid": d.vid, "pid": d.pid,
            "product": d.product, "channels": CHANNEL_NAMES[:d.n_chan],
            "hwid": f"USB VID:PID={d.vid:04X}:{d.pid:04X}"}
    d.close_if_unused()     # a handle left open trips usb1's exit finalizer
    return info
