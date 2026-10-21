"""CAN bus layer: message type, link base class, virtual bus, port specs.

CAN is not a byte stream, so this is a sibling of `transport.Link`, not a
subclass: a `CanLink` delivers whole `CanMsg`s with their arrival time.
Everything a command needs to know about the medium comes from the link:

    link.recv(timeout)      -> CanMsg | None        (rx, tx-echo, error frames)
    link.send(msg, timeout) -> None | raises CanTxTimeout when nobody ACKs
    link.set_bitrate(rate, listen_only=...)         reopen at another rate
    link.listen_only        True = the harness never ACKs on someone else's bus

Port specs (accepted wherever a COM port is accepted, `open_link` routes):
    can:gs:0          own gs_usb client (core/gs_usb.py), device 0, channel 0
    can:gs:0.1        ... channel 1
    can:gs:fd12.1     device from an already-open USB descriptor (Android)
    can:gs:.1         device from $RSH_USB_FD (Android, like usb:A/usb:B)
    can:socketcan:can0, can:slcan:COM9, can:pcan:PCAN_USBBUS1
                      python-can backends (optional extra)
    can:virtual:<name>  in-process virtual bus (tests, demo)

Session records: kind="can", hex = data bytes, plus id ("0x351" /
"0x108040FE"), ext, dlc, can_ch, rtr/err when set, acked for tx. Build them
with `can_record_fields(msg)` so every command logs the same shape.

Bitrate is not a baud: at the wrong rate a CAN controller sees stuff/form
errors and never a garbled frame, so identify sweeps BITRATE_SWEEP in
listen-only mode and watches for error frames versus valid frames.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field

# SocketCAN-style flag bits inside a 32-bit can_id (gs_usb uses the same)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x000007FF
CAN_EFF_MASK = 0x1FFFFFFF

# error classes (can_id bits of an error frame), SocketCAN numbering
CAN_ERR_TX_TIMEOUT = 0x0001
CAN_ERR_LOSTARB = 0x0002
CAN_ERR_CRTL = 0x0004
CAN_ERR_PROT = 0x0008
CAN_ERR_TRX = 0x0010
CAN_ERR_ACK = 0x0020
CAN_ERR_BUSOFF = 0x0040
CAN_ERR_BUSERROR = 0x0080
CAN_ERR_RESTARTED = 0x0100
CAN_ERR_CNT = 0x0200
ERR_CLASS_NAMES = {
    CAN_ERR_TX_TIMEOUT: "tx-timeout", CAN_ERR_LOSTARB: "lost-arbitration",
    CAN_ERR_CRTL: "controller", CAN_ERR_PROT: "protocol", CAN_ERR_TRX: "transceiver",
    CAN_ERR_ACK: "no-ack", CAN_ERR_BUSOFF: "bus-off", CAN_ERR_BUSERROR: "bus-error",
    CAN_ERR_RESTARTED: "restarted", CAN_ERR_CNT: "error-counters",
}
# data[2] of a protocol error frame
PROT_TYPE_NAMES = {0x01: "bit", 0x02: "form", 0x04: "stuff", 0x08: "bit0",
                   0x10: "bit1", 0x20: "overload", 0x40: "active-flag", 0x80: "tx"}

# identify sweeps these (the devices we care about: Pylontech/Victron/Dyness/
# LuxPower 500 k, Victron alt / Dyness DIP 250 k, Huawei R48xx 125 k)
BITRATE_SWEEP = (500_000, 250_000, 125_000)
STANDARD_BITRATES = (10_000, 20_000, 50_000, 83_333, 100_000, 125_000, 250_000,
                     500_000, 800_000, 1_000_000)
DEFAULT_BITRATE = 500_000


class CanError(Exception):
    pass


class CanTxTimeout(CanError):
    """The frame was handed to the controller but no node acknowledged it
    within the timeout (nobody there, wrong bitrate, or we are listen-only)."""


@dataclass
class CanMsg:
    ts: float                 # perf_counter at arrival (or at send for tx)
    can_id: int               # identifier without flag bits
    data: bytes = b""
    ext: bool = False         # 29-bit identifier
    rtr: bool = False
    err: bool = False         # error frame: can_id = error class bits
    channel: int = 0          # adapter channel the frame was seen on
    echo: bool = False        # our own transmission, confirmed on the bus
    ts_hw_us: int | None = None   # adapter hardware timestamp if offered
    extra: dict = field(default_factory=dict)

    @property
    def dlc(self) -> int:
        return len(self.data)

    @property
    def id_str(self) -> str:
        if self.err:
            return "ERR"
        return f"0x{self.can_id:08X}" if self.ext else f"0x{self.can_id:03X}"

    def raw_id(self) -> int:
        """can_id with EFF/RTR/ERR flag bits (SocketCAN / gs_usb encoding)."""
        v = self.can_id & (CAN_EFF_MASK if self.ext else CAN_SFF_MASK)
        if self.ext:
            v |= CAN_EFF_FLAG
        if self.rtr:
            v |= CAN_RTR_FLAG
        if self.err:
            v |= CAN_ERR_FLAG
        return v

    @classmethod
    def from_raw_id(cls, raw: int, data: bytes, ts: float, channel: int = 0,
                    **kw) -> "CanMsg":
        err = bool(raw & CAN_ERR_FLAG)
        ext = bool(raw & CAN_EFF_FLAG)
        return cls(ts=ts, can_id=raw & (CAN_EFF_MASK if ext or err else CAN_SFF_MASK),
                   data=bytes(data), ext=ext, rtr=bool(raw & CAN_RTR_FLAG), err=err,
                   channel=channel, **kw)

    def describe_error(self) -> str:
        if not self.err:
            return ""
        parts = [n for bit, n in ERR_CLASS_NAMES.items() if self.can_id & bit]
        d = self.data + bytes(8)
        if self.can_id & CAN_ERR_PROT and d[2]:
            parts.append("prot=" + "+".join(n for b, n in PROT_TYPE_NAMES.items() if d[2] & b))
        if self.can_id & CAN_ERR_CNT:
            parts.append(f"txerr={d[6]} rxerr={d[7]}")
        return " ".join(parts) or "error-frame"

    def __str__(self) -> str:
        if self.err:
            return f"ch{self.channel} ERR {self.describe_error()}"
        kind = "R" if self.rtr else ""
        return (f"ch{self.channel} {self.id_str}{kind} [{self.dlc}] "
                f"{self.data.hex(' ').upper()}" + (" (echo)" if self.echo else ""))


def can_record_fields(msg: CanMsg) -> dict:
    """Extra fields for SessionWriter.log(..., kind="can", **fields)."""
    rec = {"id": msg.id_str, "ext": msg.ext, "dlc": msg.dlc, "can_ch": msg.channel}
    if msg.rtr:
        rec["rtr"] = True
    if msg.err:
        rec["err"] = msg.describe_error()
    if msg.ts_hw_us is not None:
        rec["ts_hw_us"] = msg.ts_hw_us
    return rec


def log_can(session, chan: str, direction: str, msg: CanMsg, decoded=None,
            errors=None, **extra) -> dict:
    """One session record per CAN frame; same clock/arrival semantics as the
    serial records (ts_mono = msg.ts)."""
    fields = can_record_fields(msg)
    fields.update(extra)
    return session.log(chan, direction, msg.data, kind="can", decoded=decoded,
                       errors=errors, ts_mono=msg.ts, **fields)


class CanLink:
    """Base class. Subclasses: GsUsbLink (core/gs_usb.py), PythonCanLink,
    VirtualCanEnd."""

    name: str = "can"
    bitrate: int = DEFAULT_BITRATE
    listen_only: bool = False
    channel: int = 0
    is_can = True

    def __init__(self):
        self.stats = {"rx": 0, "tx": 0, "err": 0, "overflow": 0, "tx_timeout": 0}

    def recv(self, timeout: float = 0.05) -> CanMsg | None:
        raise NotImplementedError

    def send(self, msg: CanMsg, timeout: float = 0.5) -> None:
        raise NotImplementedError

    def set_bitrate(self, bitrate: int, listen_only: bool | None = None) -> None:
        raise NotImplementedError

    def flush_input(self) -> None:
        pass

    def close(self) -> None:
        pass

    # the serial Link API that commands may touch generically
    def set_baud(self, baud: int) -> None:
        self.set_bitrate(baud)

    @property
    def baud(self) -> int:
        return self.bitrate

    def describe(self) -> str:
        mode = "listen-only" if self.listen_only else "normal"
        return f"{self.name} @ {self.bitrate // 1000} kbit/s {mode}"


# ------------------------------------------------------------- virtual bus
class VirtualCanEnd(CanLink):
    def __init__(self, bus: "VirtualCanBus", name: str, bitrate: int,
                 listen_only: bool, channel: int = 0):
        super().__init__()
        self.bus = bus
        self.name = name
        self.bitrate = bitrate
        self.listen_only = listen_only
        self.channel = channel
        self._rx: queue.Queue[CanMsg] = queue.Queue()
        self.closed = False

    def recv(self, timeout: float = 0.05) -> CanMsg | None:
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def send(self, msg: CanMsg, timeout: float = 0.5) -> None:
        if self.listen_only:
            raise CanError(f"{self.name}: link is listen-only, refusing to transmit")
        self.bus.transmit(self, msg)

    def set_bitrate(self, bitrate: int, listen_only: bool | None = None) -> None:
        self.bitrate = bitrate
        if listen_only is not None:
            self.listen_only = listen_only
        self.flush_input()

    def flush_input(self) -> None:
        try:
            while True:
                self._rx.get_nowait()
        except queue.Empty:
            pass

    def close(self) -> None:
        self.closed = True
        self.bus.detach(self)

    def _push(self, msg: CanMsg) -> None:
        if not self.closed:
            self._rx.put(msg)


class VirtualCanBus:
    """One shared CAN medium. Every end sees every other end's frames.

    ACK model: a frame is acknowledged iff at least one *other* attached end
    runs at the sender's bitrate and is not listen-only. Without an ACK the
    sender gets CanTxTimeout (what a real controller reports after its
    retries) - this is how "nobody there" and "listen-only really does not
    ACK" are tested. Ends at a different bitrate receive an error frame
    (stuff/form error) instead of the message, like a real controller.
    """

    _registry: dict[str, "VirtualCanBus"] = {}
    _reg_lock = threading.Lock()

    @classmethod
    def get(cls, name: str) -> "VirtualCanBus":
        with cls._reg_lock:
            bus = cls._registry.get(name)
            if bus is None:
                bus = cls._registry[name] = VirtualCanBus(name)
            return bus

    def __init__(self, name: str = "vcan"):
        self.name = name
        self.ends: list[VirtualCanEnd] = []
        self._lock = threading.Lock()
        self.frames_total = 0

    def end(self, name: str, bitrate: int = DEFAULT_BITRATE,
            listen_only: bool = False, channel: int = 0) -> VirtualCanEnd:
        e = VirtualCanEnd(self, name, bitrate, listen_only, channel)
        with self._lock:
            self.ends.append(e)
        return e

    def detach(self, end: VirtualCanEnd) -> None:
        with self._lock:
            if end in self.ends:
                self.ends.remove(end)

    def transmit(self, sender: VirtualCanEnd, msg: CanMsg) -> None:
        with self._lock:
            others = [e for e in self.ends if e is not sender]
        now = time.perf_counter()
        ackers = [e for e in others if e.bitrate == sender.bitrate and not e.listen_only]
        sender.stats["tx"] += 1
        if not ackers:
            sender.stats["tx_timeout"] += 1
            # the frame is still on the wire: listen-only peers at the right
            # rate see it (they just do not ACK)
            for e in others:
                if e.bitrate == sender.bitrate:
                    e._push(CanMsg(now, msg.can_id, bytes(msg.data), msg.ext, msg.rtr,
                                   channel=e.channel))
            raise CanTxTimeout(f"{sender.name}: no ACK for {msg.id_str} on bus "
                               f"{self.name} (no active peer at {sender.bitrate})")
        self.frames_total += 1
        for e in others:
            if e.bitrate == sender.bitrate:
                e._push(CanMsg(now, msg.can_id, bytes(msg.data), msg.ext, msg.rtr,
                               channel=e.channel))
                e.stats["rx"] += 1
            else:
                e.stats["err"] += 1
                e._push(CanMsg(now, CAN_ERR_PROT | CAN_ERR_BUSERROR,
                               bytes([0, 0, 0x04, 0, 0, 0, 0, 0]), err=True,
                               channel=e.channel))
        # tx echo back to the sender (what gs_usb delivers after a real ACK)
        sender._push(CanMsg(now, msg.can_id, bytes(msg.data), msg.ext, msg.rtr,
                            channel=sender.channel, echo=True))


# ------------------------------------------------------------ python-can
class PythonCanLink(CanLink):
    """Thin wrapper around a python-can Bus (socketcan on the Pi, slcan,
    pcan, ...). Optional extra: pip install python-can."""

    def __init__(self, interface: str, channel: str, bitrate: int = DEFAULT_BITRATE,
                 listen_only: bool = False, name: str | None = None, **kw):
        super().__init__()
        import can  # deferred
        self._can = can
        self.interface, self.chan_arg = interface, channel
        self.name = name or f"can:{interface}:{channel}"
        self.bitrate = bitrate
        self.listen_only = listen_only
        self._kw = kw
        self.bus = None
        self._open()

    def _open(self) -> None:
        can = self._can
        kw = dict(self._kw)
        if self.interface != "socketcan":      # socketcan: rate set by ip link
            kw["bitrate"] = self.bitrate
        if self.interface == "slcan" and self.listen_only:
            # python-can would send Lawicel 'L' which ElmuSoft Slcan 2.5 does
            # not define as listen-only; we send M1 ourselves below
            kw["listen_only"] = False
        elif self.listen_only and self.interface in ("pcan", "kvaser", "ixxat", "vector"):
            kw["receive_own_messages"] = False
        self.bus = can.Bus(interface=self.interface, channel=self.chan_arg,
                           receive_own_messages=False, **kw)
        if self.interface == "slcan" and self.listen_only:
            ser = getattr(self.bus, "serialPortOrig", None)
            if ser is not None:
                ser.write(b"C\rM1\rO\r")       # ElmuSoft: silent mode, then open

    def recv(self, timeout: float = 0.05) -> CanMsg | None:
        m = self.bus.recv(timeout)
        if m is None:
            return None
        ts = time.perf_counter()
        msg = CanMsg(ts, m.arbitration_id, bytes(m.data), m.is_extended_id,
                     m.is_remote_frame, m.is_error_frame, channel=self.channel)
        self.stats["err" if msg.err else "rx"] += 1
        return msg

    def send(self, msg: CanMsg, timeout: float = 0.5) -> None:
        if self.listen_only:
            raise CanError(f"{self.name}: link is listen-only, refusing to transmit")
        m = self._can.Message(arbitration_id=msg.can_id, data=msg.data,
                              is_extended_id=msg.ext, is_remote_frame=msg.rtr)
        try:
            self.bus.send(m, timeout=timeout)
        except self._can.CanError as e:
            self.stats["tx_timeout"] += 1
            raise CanTxTimeout(f"{self.name}: {e}") from e
        self.stats["tx"] += 1

    def set_bitrate(self, bitrate: int, listen_only: bool | None = None) -> None:
        if listen_only is not None:
            self.listen_only = listen_only
        if bitrate == self.bitrate and listen_only is None:
            return
        self.bitrate = bitrate
        self.close()
        self._open()

    def flush_input(self) -> None:
        while self.bus.recv(0) is not None:
            pass

    def close(self) -> None:
        if self.bus is not None:
            try:
                self.bus.shutdown()
            finally:
                self.bus = None


# --------------------------------------------------------------- port spec
@dataclass
class CanSpec:
    backend: str          # gs | socketcan | slcan | pcan | virtual | ...
    device: str           # "0", "fd12", "", "can0", "COM9", "<name>"
    channel: int = 0

    @property
    def fd(self) -> int | None:
        """Android: 'fd12' or '' with $RSH_USB_FD -> descriptor number."""
        if self.backend != "gs":
            return None
        if self.device.lower().startswith("fd") and self.device[2:].isdigit():
            return int(self.device[2:])
        if self.device == "" and os.environ.get("RSH_USB_FD"):
            return int(os.environ["RSH_USB_FD"])
        return None

    @property
    def index(self) -> int:
        return int(self.device) if self.device.isdigit() else 0


def is_can_spec(spec: str) -> bool:
    return spec.lower().startswith("can:")


def parse_can_spec(spec: str) -> CanSpec:
    """'can:gs:0.1' -> CanSpec('gs', '0', 1); 'can:virtual:lab' ->
    CanSpec('virtual', 'lab', 0); 'can:gs' -> ('gs', '', 0)."""
    if not is_can_spec(spec):
        raise ValueError(f"not a can: spec: {spec}")
    parts = spec.split(":", 2)
    backend = parts[1].lower() if len(parts) > 1 and parts[1] else "gs"
    device = parts[2] if len(parts) > 2 else ""
    channel = 0
    if backend == "gs":
        if "." in device:
            device, ch = device.rsplit(".", 1)
            if not ch.isdigit():
                raise ValueError(f"bad CAN channel in {spec!r}")
            channel = int(ch)
        if device and not (device.isdigit() or
                           (device.lower().startswith("fd") and device[2:].isdigit())):
            raise ValueError(f"bad gs_usb device {device!r} in {spec!r} "
                             f"(use an index, fdNN, or nothing for $RSH_USB_FD)")
    elif backend == "virtual":
        if "." in device:
            device, ch = device.rsplit(".", 1)
            channel = int(ch) if ch.isdigit() else 0
        device = device or "vcan"
    elif not device:
        raise ValueError(f"{spec!r}: backend {backend} needs a channel name")
    return CanSpec(backend, device, channel)


def open_can_link(spec: str, bitrate: int | None = None, listen_only: bool = False,
                  name: str | None = None) -> CanLink:
    cs = parse_can_spec(spec)
    rate = bitrate or DEFAULT_BITRATE
    if cs.backend == "gs":
        from .gs_usb import GsUsbLink
        return GsUsbLink(cs, rate, listen_only=listen_only, name=name or spec)
    if cs.backend == "virtual":
        end = VirtualCanBus.get(cs.device).end(name or spec, rate, listen_only,
                                               channel=cs.channel)
        return end
    return PythonCanLink(cs.backend, cs.device, rate, listen_only=listen_only,
                         name=name or spec)


def looks_like_bitrate(value: int | None) -> bool:
    return bool(value) and value >= 10_000
