"""gs_usb (candleLight) USB-CAN adapters over libusb - our own client.

Why not python-can's gs_usb backend: it starts the channel in normal mode
(no listen-only), hard-codes channel 0 and enumerates with pyusb only. This
client speaks the legacy gs_usb protocol that the Linux kernel driver uses
(docs/refs/can-adapters/linux-gs_usb.c, candleLight_fw-gs_usb.h):

  control (vendor, recipient interface):
    HOST_FORMAT 0  out  wValue=1, wIndex=interface   u32 0x0000BEEF
    BITTIMING   1  out  wValue=channel               5 x u32
    MODE        2  out  wValue=channel               u32 mode, u32 flags
    BT_CONST    4  in   wValue=channel               feature, fclk, 8 x u32 limits
    DEVICE_CONFIG 5 in  wValue=1, wIndex=interface   3 x u8, u8 icount, 2 x u32
    GET_STATE   14 in   wValue=channel               state, rxerr, txerr
  bulk IN 0x81 / OUT 0x02: gs_host_frame = u32 echo_id, u32 can_id,
    u8 dlc, u8 channel, u8 flags, u8 reserved, u8 data[8] (+ u32 ts_us)
    echo_id 0xFFFFFFFF = received frame; a transmitted frame comes back with
    its echo_id. On the kernel driver's firmwares that means "ACKed on the
    bus"; on Candlelight 2.5 (USBCANFD DUAL, measured 2026-10-24) the echo
    comes back ~0.3 ms after the write with NO bus attached, i.e. it only
    means "handed to the CAN controller". The missing ACK then arrives as an
    error frame ~0.15 ms later - with the CAN_ERR_ACK class, or (right after
    a channel start) with NO class bits at all and only TEC=8 in data[6] -
    then the controller retries for ~500 ms, reports TX_TIMEOUT and goes
    error-passive; error-state changes come as CRTL frames ~100 ms later.
    GET_STATE (BREQ 14) is not implemented (pipe error). So "sent" = echo
    received AND no tx-failure error frame (ACK / bus-error / protocol /
    class-less) on that channel within ACK_GRACE_S afterwards.

Devices: 1D50:606F (candleLight, CANable, ElmuSoft Candlelight 2.5 = the
USBCANFD DUAL's stock firmware), 1209:CA01 (CANnectivity). All channels of
one device share the bulk endpoints; the `channel` byte selects. Works
through WinUSB (auto-installed by the firmware's MS OS descriptor), Linux
libusb (kernel gs_usb auto-detached) and Android (termux-usb descriptor,
`USBContext(with_device_discovery=False).wrapSysDevice(fd)` as in
ftdi_usb.py). Pure helpers (frame pack/unpack, bit timing) are unit-tested
without hardware.
"""
from __future__ import annotations

import atexit
import os
import queue
import struct
import threading
import time

from .canbus import (CanLink, CanMsg, CanSpec, CanError, CanTxTimeout,
                     CAN_ERR_FLAG, CAN_ERR_CRTL, CAN_ERR_ACK, CAN_ERR_BUSERROR,
                     CAN_ERR_PROT)

GS_USB_IDS = {(0x1D50, 0x606F): "gs_usb (candleLight / CANable / Candlelight 2.5)",
              (0x1209, 0xCA01): "gs_usb (CANnectivity)"}
SLCAN_IDS = {(0x16D0, 0x117E): "ElmuSoft Slcan 2.5 (COM port)"}
DFU_IDS = {(0x0483, 0xDF11): "STM32 DFU bootloader - adapter stuck in DFU, "
                             "see docs/CAN-PLAN.md 1.2"}

# bRequest
BREQ_HOST_FORMAT, BREQ_BITTIMING, BREQ_MODE, BREQ_BERR, BREQ_BT_CONST = 0, 1, 2, 3, 4
BREQ_DEVICE_CONFIG, BREQ_TIMESTAMP, BREQ_IDENTIFY = 5, 6, 7
BREQ_DATA_BITTIMING, BREQ_BT_CONST_EXT, BREQ_GET_STATE = 10, 11, 14
# mode
MODE_RESET, MODE_START = 0, 1
# feature / mode flags
FEAT_LISTEN_ONLY = 1 << 0
FEAT_LOOP_BACK = 1 << 1
FEAT_TRIPLE_SAMPLE = 1 << 2
FEAT_ONE_SHOT = 1 << 3
FEAT_HW_TIMESTAMP = 1 << 4
FEAT_IDENTIFY = 1 << 5
FEAT_PAD_PKTS = 1 << 7
FEAT_FD = 1 << 8
FEAT_QUIRK_LPC546XX = 1 << 9
FEAT_BT_CONST_EXT = 1 << 10
FEAT_TERMINATION = 1 << 11
FEAT_BERR_REPORTING = 1 << 12
ACK_GRACE_S = 0.01     # window after the echo in which a CAN_ERR_ACK means 'not ACKed'
FEAT_GET_STATE = 1 << 13
FEAT_ELM_PROTOCOL = 1 << 14       # ElmuSoft extension; we stay legacy
FEATURE_NAMES = {FEAT_LISTEN_ONLY: "listen-only", FEAT_LOOP_BACK: "loopback",
                 FEAT_TRIPLE_SAMPLE: "triple-sample", FEAT_ONE_SHOT: "one-shot",
                 FEAT_HW_TIMESTAMP: "hw-timestamp", FEAT_IDENTIFY: "identify",
                 FEAT_PAD_PKTS: "pad-pkts", FEAT_FD: "fd", FEAT_TERMINATION: "termination",
                 FEAT_BERR_REPORTING: "berr-reporting", FEAT_GET_STATE: "get-state",
                 FEAT_ELM_PROTOCOL: "elmuesoft-2.5"}
# frame flags
FLAG_OVERFLOW = 1 << 0
FLAG_FD = 1 << 1
HOST_FORMAT_LE = 0x0000BEEF
ECHO_ID_RX = 0xFFFFFFFF
MAX_TX_SLOTS = 10                 # like GS_MAX_TX_URBS
STATE_NAMES = {0: "error-active", 1: "error-warning", 2: "error-passive",
               3: "bus-off", 4: "stopped", 5: "sleeping"}

_HDR = struct.Struct("<IIBBBB")   # echo_id, can_id, dlc, channel, flags, reserved
HDR_LEN = _HDR.size               # 12
FRAME_LEN = HDR_LEN + 8           # classic CAN, no timestamp
FRAME_LEN_TS = FRAME_LEN + 4
IO_ERRORS_FATAL = 200
IO_ERROR_PAUSE_S = 0.005
CTRL_RETRIES = 4


# ------------------------------------------------------------ pure helpers
def _is_tx_failure(err_id: int) -> bool:
    """Error-frame classes that mean our own transmission failed (see the
    module docstring): ACK error, bus/protocol error, or the class-less
    frame Candlelight 2.5 sends after the first failed attempt. A pure
    controller-state frame (CAN_ERR_CRTL alone, ~100 ms late) is not one."""
    cls = err_id & ~CAN_ERR_FLAG & 0x1FFFFFFF
    return cls == 0 or bool(cls & (CAN_ERR_ACK | CAN_ERR_BUSERROR | CAN_ERR_PROT))


def pack_host_frame(echo_id: int, msg: CanMsg, with_ts: bool = False,
                    quirk_pad: bool = False) -> bytes:
    if len(msg.data) > 8:
        raise ValueError("classic CAN frame has at most 8 data bytes")
    buf = _HDR.pack(echo_id, msg.raw_id(), len(msg.data), msg.channel, 0, 0)
    buf += bytes(msg.data).ljust(8, b"\0")
    if with_ts:
        buf += b"\0\0\0\0"
    if quirk_pad:
        buf += b"\0"
    return buf


def unpack_host_frames(buf: bytes, ts: float, frame_len: int) -> list[CanMsg]:
    """A bulk IN transfer normally holds one frame; parse as many whole
    frames as the buffer contains (tolerates packed/padded transfers)."""
    out = []
    n = len(buf)
    off = 0
    while off + FRAME_LEN <= n:
        echo_id, raw_id, dlc, channel, flags, _ = _HDR.unpack_from(buf, off)
        data = buf[off + HDR_LEN: off + HDR_LEN + min(dlc, 8)]
        ts_hw = None
        if frame_len >= FRAME_LEN_TS and off + FRAME_LEN_TS <= n:
            ts_hw = struct.unpack_from("<I", buf, off + FRAME_LEN)[0]
        msg = CanMsg.from_raw_id(raw_id, data, ts, channel=channel,
                                 echo=echo_id != ECHO_ID_RX, ts_hw_us=ts_hw)
        msg.extra["echo_id"] = echo_id
        if flags & FLAG_OVERFLOW:
            msg.extra["overflow"] = True
        out.append(msg)
        off += frame_len
    return out


def cia_sample_point(bitrate: int) -> float:
    """CiA 301 recommendation."""
    if bitrate > 800_000:
        return 0.75
    if bitrate > 500_000:
        return 0.80
    return 0.875


def calc_bit_timing(fclk: int, bitrate: int, btc: dict,
                    sample_point: float | None = None) -> dict:
    """Pick brp / tseg1 / tseg2 from the device's BT_CONST limits (same
    strategy as the kernel's can_calc_bittiming: best sample-point match,
    ties go to more time quanta per bit). Returns the five BITTIMING
    fields plus the achieved rate and sample point."""
    sp_target = sample_point or cia_sample_point(bitrate)
    tseg1_min, tseg1_max = btc["tseg1_min"], btc["tseg1_max"]
    tseg2_min, tseg2_max = btc["tseg2_min"], btc["tseg2_max"]
    best = None
    tq_max = 1 + tseg1_max + tseg2_max
    tq_min = 1 + tseg1_min + tseg2_min
    for tq_total in range(tq_max, tq_min - 1, -1):
        brp_exact = fclk / (bitrate * tq_total)
        brp = int(round(brp_exact / btc["brp_inc"])) * btc["brp_inc"]
        if brp < btc["brp_min"] or brp > btc["brp_max"] or brp == 0:
            continue
        rate = fclk / (brp * tq_total)
        rate_err = abs(rate - bitrate) / bitrate
        if rate_err > 0.001:                       # 0.1 %: not this tq count
            continue
        tseg1 = int(round(tq_total * sp_target)) - 1
        tseg1 = max(tseg1_min, min(tseg1_max, tseg1))
        tseg2 = tq_total - 1 - tseg1
        if tseg2 < tseg2_min or tseg2 > tseg2_max:
            tseg2 = max(tseg2_min, min(tseg2_max, tseg2))
            tseg1 = tq_total - 1 - tseg2
            if tseg1 < tseg1_min or tseg1 > tseg1_max:
                continue
        sp = (1 + tseg1) / tq_total
        score = (round(rate_err, 6), round(abs(sp - sp_target), 4))
        if best is None or score < best[0]:
            best = (score, brp, tseg1, tseg2, tq_total, rate, sp)
    if best is None:
        raise CanError(f"no bit timing for {bitrate} bit/s at fclk {fclk} Hz "
                       f"within {btc}")
    _, brp, tseg1, tseg2, tq_total, rate, sp = best
    sjw = max(1, min(btc["sjw_max"], tseg2))
    return {"prop_seg": 1, "phase_seg1": tseg1 - 1, "phase_seg2": tseg2,
            "sjw": sjw, "brp": brp, "tq_total": tq_total,
            "bitrate": int(round(rate)), "sample_point": round(sp, 4)}


def pack_bit_timing(bt: dict) -> bytes:
    return struct.pack("<5I", bt["prop_seg"], bt["phase_seg1"], bt["phase_seg2"],
                       bt["sjw"], bt["brp"])


def unpack_bt_const(buf: bytes) -> dict:
    f = struct.unpack_from("<10I", buf)
    return {"feature": f[0], "fclk_can": f[1], "tseg1_min": f[2], "tseg1_max": f[3],
            "tseg2_min": f[4], "tseg2_max": f[5], "sjw_max": f[6], "brp_min": f[7],
            "brp_max": f[8], "brp_inc": f[9]}


def unpack_device_config(buf: bytes) -> dict:
    r1, r2, r3, icount, sw, hw = struct.unpack_from("<4B2I", buf)
    return {"channels": icount + 1, "sw_version": sw, "hw_version": hw}


def feature_names(feature: int) -> list[str]:
    return [n for bit, n in FEATURE_NAMES.items() if feature & bit]


# -------------------------------------------------------------- the device
def _find_vendor_interface(dev):
    """(interface number, ep_in, ep_out) of the gs_usb data interface:
    vendor class 0xFF with one bulk IN and one bulk OUT endpoint. Legacy
    firmware: interface 0 with 0x81/0x02; a DFU runtime interface (class
    0xFE) sits beside it."""
    for cfg in dev.iterConfigurations():
        for iface in cfg:
            for setting in iface:
                if setting.getClass() != 0xFF:
                    continue
                ep_in = ep_out = None
                for ep in setting:
                    if ep.getAttributes() & 0x03 != 0x02:   # bulk only
                        continue
                    if ep.getAddress() & 0x80:
                        ep_in = ep_in or ep.getAddress()
                    else:
                        ep_out = ep_out or ep.getAddress()
                if ep_in is not None and ep_out is not None:
                    return setting.getNumber(), ep_in, ep_out
    raise CanError("no gs_usb data interface (vendor class, bulk IN+OUT) found")


class GsUsbDevice:
    """One libusb handle per adapter, shared by its channel links; one
    reader thread fans frames out to per-channel queues."""
    _by_key: dict = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, spec: CanSpec) -> "GsUsbDevice":
        key = ("fd", spec.fd) if spec.fd is not None else ("idx", spec.index)
        with cls._lock:
            d = cls._by_key.get(key)
            if d is None:
                d = GsUsbDevice(key)
                cls._by_key[key] = d
            return d

    def __init__(self, key):
        import usb1
        self.usb1 = usb1
        self.key = key
        if key[0] == "fd":
            self.ctx = usb1.USBContext(with_device_discovery=False)
            self.ctx.open()
            self.handle = self.ctx.wrapSysDevice(key[1])
            self._wrapped = True
        else:
            self.ctx = usb1.USBContext()
            self.ctx.open()
            devs = [d for d in self.ctx.getDeviceList()
                    if (d.getVendorID(), d.getProductID()) in GS_USB_IDS]
            devs.sort(key=lambda d: (d.getBusNumber(), d.getDeviceAddress()))
            if key[1] >= len(devs):
                self.ctx.close()
                raise CanError(f"gs_usb device index {key[1]} not found "
                               f"({len(devs)} present)")
            self.handle = devs[key[1]].open()
            self._wrapped = False
        dev = self.handle.getDevice()
        self.vid, self.pid = dev.getVendorID(), dev.getProductID()
        if (self.vid, self.pid) not in GS_USB_IDS:
            self._close_handle()
            raise CanError(f"USB device {self.vid:04x}:{self.pid:04x} is not a "
                           f"gs_usb adapter")
        try:
            self.product = dev.getProduct() or f"gs_usb {self.pid:04x}"
            self.serial = dev.getSerialNumber() or ""
        except usb1.USBError:
            self.product, self.serial = f"gs_usb {self.pid:04x}", ""
        self.intf, self.ep_in, self.ep_out = _find_vendor_interface(dev)
        self.max_packet = 64
        try:
            self.handle.setAutoDetachKernelDriver(True)   # Linux: kernel gs_usb
        except usb1.USBError:
            pass
        self.handle.claimInterface(self.intf)
        self.io_lock = threading.Lock()
        self._ctrl_out(BREQ_HOST_FORMAT, 1, self.intf, struct.pack("<I", HOST_FORMAT_LE))
        self.config = unpack_device_config(
            self._ctrl_in(BREQ_DEVICE_CONFIG, 1, self.intf, 12))
        self.n_channels = self.config["channels"]
        self.bt_const: dict[int, dict] = {}
        for ch in range(self.n_channels):
            self.bt_const[ch] = unpack_bt_const(self._ctrl_in(BREQ_BT_CONST, ch, 0, 40))
        self.feature = self.bt_const[0]["feature"]
        self.hw_timestamp = bool(self.feature & FEAT_HW_TIMESTAMP)
        self.frame_len = FRAME_LEN_TS if self.hw_timestamp else FRAME_LEN
        self.quirk_pad = bool(self.feature & FEAT_QUIRK_LPC546XX)
        self.started: dict[int, dict] = {}        # channel -> {bitrate, listen_only}
        self.rx: dict[int, queue.Queue] = {ch: queue.Queue() for ch in range(self.n_channels)}
        self._echo: dict[tuple[int, int], tuple[threading.Event, list]] = {}
        self._echo_lock = threading.Lock()
        self._ack_err: dict[int, threading.Event] = {ch: threading.Event()
                                                     for ch in range(self.n_channels)}
        self.ack_errors: dict[int, int] = {ch: 0 for ch in range(self.n_channels)}
        self.ack_grace_s = ACK_GRACE_S
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.io_errors = 0
        self.closed = False
        atexit.register(self._atexit)

    # ------------------------------------------------------------ control
    def _req(self, direction):
        u = self.usb1
        return u.REQUEST_TYPE_VENDOR | u.RECIPIENT_INTERFACE | direction

    def _ctrl_out(self, request: int, value: int, index: int, data: bytes) -> None:
        u = self.usb1
        for attempt in range(CTRL_RETRIES):
            try:
                with self.io_lock:
                    self.handle.controlWrite(self._req(u.ENDPOINT_OUT), request, value,
                                             index, data, timeout=1000)
                return
            except u.USBError as e:
                self.io_errors += 1
                if attempt == CTRL_RETRIES - 1 or isinstance(e, u.USBErrorPipe):
                    raise CanError(f"{self.product}: gs_usb request {request} "
                                   f"(ch {value}) rejected: {e!r}") from e
                time.sleep(0.01 * (attempt + 1))

    def _ctrl_in(self, request: int, value: int, index: int, length: int) -> bytes:
        u = self.usb1
        for attempt in range(CTRL_RETRIES):
            try:
                with self.io_lock:
                    return bytes(self.handle.controlRead(self._req(u.ENDPOINT_IN), request,
                                                         value, index, length, timeout=1000))
            except u.USBError as e:
                self.io_errors += 1
                if attempt == CTRL_RETRIES - 1 or isinstance(e, u.USBErrorPipe):
                    raise CanError(f"{self.product}: gs_usb request {request} "
                                   f"(ch {value}) failed: {e!r}") from e
                time.sleep(0.01 * (attempt + 1))
        raise CanError("unreachable")

    def describe(self) -> dict:
        return {"product": self.product, "serial": self.serial,
                "vid": self.vid, "pid": self.pid,
                "hwid": f"USB VID:PID={self.vid:04X}:{self.pid:04X}",
                "channels": self.n_channels,
                "sw_version": self.config["sw_version"],
                "hw_version": self.config["hw_version"],
                "fclk_can": self.bt_const[0]["fclk_can"],
                "features": feature_names(self.feature)}

    def state(self, channel: int) -> dict | None:
        if not self.feature & FEAT_GET_STATE:
            return None
        s, rxerr, txerr = struct.unpack("<3I", self._ctrl_in(BREQ_GET_STATE, channel, 0, 12))
        return {"state": STATE_NAMES.get(s, str(s)), "rxerr": rxerr, "txerr": txerr}

    # ---------------------------------------------------------- channels
    def start_channel(self, channel: int, bitrate: int, listen_only: bool) -> dict:
        if channel >= self.n_channels:
            raise CanError(f"{self.product}: channel {channel} does not exist "
                           f"({self.n_channels} channel(s))")
        if listen_only and not self.feature & FEAT_LISTEN_ONLY:
            raise CanError(f"{self.product}: firmware offers no listen-only mode; "
                           f"refusing to open a tap that would ACK")
        if channel in self.started:
            self.stop_channel(channel)
        btc = self.bt_const[channel]
        bt = calc_bit_timing(btc["fclk_can"], bitrate, btc)
        self._ctrl_out(BREQ_BITTIMING, channel, 0, pack_bit_timing(bt))
        flags = 0
        if listen_only:
            flags |= FEAT_LISTEN_ONLY
        if self.hw_timestamp:
            flags |= FEAT_HW_TIMESTAMP
        if self.feature & FEAT_BERR_REPORTING:
            flags |= FEAT_BERR_REPORTING
        self._ctrl_out(BREQ_MODE, channel, 0, struct.pack("<2I", MODE_START, flags))
        self.started[channel] = {"bitrate": bitrate, "listen_only": listen_only,
                                 "timing": bt}
        self._ensure_reader()
        return bt

    def stop_channel(self, channel: int) -> None:
        if channel in self.started:
            try:
                self._ctrl_out(BREQ_MODE, channel, 0, struct.pack("<2I", MODE_RESET, 0))
            finally:
                self.started.pop(channel, None)

    # ------------------------------------------------------------ reader
    def _ensure_reader(self) -> None:
        if self._reader is None or not self._reader.is_alive():
            self._stop.clear()
            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name=f"gs_usb-reader-{self.key}")
            self._reader.start()

    def _read_loop(self) -> None:
        u = self.usb1
        consecutive = 0
        size = max(self.frame_len * 8, self.max_packet * 2)
        while not self._stop.is_set() and self.started:
            try:
                raw = self.handle.bulkRead(self.ep_in, size, timeout=100)
                consecutive = 0
            except u.USBErrorTimeout:
                continue
            except u.USBError as e:
                if self._stop.is_set():
                    return
                self.io_errors += 1
                consecutive += 1
                if consecutive >= IO_ERRORS_FATAL:
                    for ch in self.rx.values():
                        ch.put(CanMsg(time.perf_counter(), CAN_ERR_CRTL, bytes(8),
                                      err=True, extra={"fatal": repr(e)}))
                    return
                time.sleep(IO_ERROR_PAUSE_S)
                continue
            ts = time.perf_counter()
            for msg in unpack_host_frames(bytes(raw), ts, self.frame_len):
                if msg.echo:
                    key = (msg.channel, msg.extra.get("echo_id"))
                    with self._echo_lock:
                        slot = self._echo.get(key)
                    if slot is not None:
                        slot[1].append(msg)
                        slot[0].set()
                elif msg.err and _is_tx_failure(msg.can_id):
                    self.ack_errors[msg.channel] = self.ack_errors.get(msg.channel, 0) + 1
                    ev = self._ack_err.get(msg.channel)
                    if ev is not None:
                        ev.set()
                q = self.rx.get(msg.channel)
                if q is not None:
                    q.put(msg)

    # --------------------------------------------------------------- tx
    def transmit(self, msg: CanMsg, timeout: float) -> CanMsg:
        """Send one frame; "sent" = the device's echo came back and no
        CAN_ERR_ACK error frame followed on this channel within
        ack_grace_s (see module docstring). Raises CanTxTimeout otherwise."""
        ch = msg.channel
        ack_err = self._ack_err[ch]
        with self._echo_lock:
            used = {k[1] for k in self._echo if k[0] == ch}
            free = [i for i in range(MAX_TX_SLOTS) if i not in used]
            if not free:
                raise CanTxTimeout(f"{self.product} ch{ch}: all {MAX_TX_SLOTS} tx slots "
                                   f"waiting for ACK - bus dead?")
            echo_id = free[0]
            ev, got = threading.Event(), []
            self._echo[(ch, echo_id)] = (ev, got)
        buf = pack_host_frame(echo_id, msg, self.hw_timestamp, self.quirk_pad)
        try:
            with self.io_lock:
                ack_err.clear()
                self.handle.bulkWrite(self.ep_out, buf, timeout=int(timeout * 1000) or 1)
            if not ev.wait(timeout):
                raise CanTxTimeout(f"{self.product} ch{ch}: no echo for {msg.id_str} "
                                   f"within {timeout:.2f}s")
            if self.ack_grace_s and ack_err.wait(self.ack_grace_s):
                raise CanTxTimeout(f"{self.product} ch{ch}: no ACK for {msg.id_str} "
                                   f"(controller reported CAN_ERR_ACK) - nobody on the "
                                   f"bus / wrong bitrate / termination?")
            return got[0]
        except self.usb1.USBError as e:
            raise CanError(f"{self.product} ch{ch}: USB write failed: {e!r}") from e
        finally:
            with self._echo_lock:
                self._echo.pop((ch, echo_id), None)

    # ------------------------------------------------------------ close
    def release_channel(self, channel: int) -> None:
        self.stop_channel(channel)
        if not self.started:
            self.close()

    def _close_handle(self) -> None:
        try:
            if self._wrapped:
                from .ftdi_usb import _close_wrapped_handle
                _close_wrapped_handle(self.handle)
            else:
                self.handle.close()
        finally:
            self.ctx.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        for ch in list(self.started):
            try:
                self.stop_channel(ch)
            except Exception:
                pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=0.5)
        with GsUsbDevice._lock:
            GsUsbDevice._by_key.pop(self.key, None)
        try:
            self.handle.releaseInterface(self.intf)
        except Exception:
            pass
        self._close_handle()

    def _atexit(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class GsUsbLink(CanLink):
    def __init__(self, spec: CanSpec, bitrate: int, listen_only: bool = False,
                 name: str | None = None):
        super().__init__()
        self.spec = spec
        self.dev = GsUsbDevice.get(spec)
        self.channel = spec.channel
        self.name = name or f"can:gs:{spec.device}.{spec.channel}"
        self.bitrate = bitrate
        self.listen_only = listen_only
        self.timing = self.dev.start_channel(self.channel, bitrate, listen_only)
        self._q = self.dev.rx[self.channel]
        self.flush_input()

    def recv(self, timeout: float = 0.05) -> CanMsg | None:
        try:
            msg = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if msg.extra.get("fatal"):
            raise CanError(f"{self.name}: USB link lost ({msg.extra['fatal']})")
        if msg.err:
            self.stats["err"] += 1
        elif not msg.echo:
            self.stats["rx"] += 1
        if msg.extra.get("overflow"):
            self.stats["overflow"] += 1
        return msg

    def send(self, msg: CanMsg, timeout: float = 0.5) -> None:
        if self.listen_only:
            raise CanError(f"{self.name}: link is listen-only, refusing to transmit")
        msg.channel = self.channel
        try:
            self.dev.transmit(msg, timeout)
        except CanTxTimeout:
            self.stats["tx_timeout"] += 1
            raise
        self.stats["tx"] += 1

    def set_bitrate(self, bitrate: int, listen_only: bool | None = None) -> None:
        if listen_only is None:
            listen_only = self.listen_only
        if bitrate == self.bitrate and listen_only == self.listen_only:
            return
        self.timing = self.dev.start_channel(self.channel, bitrate, listen_only)
        self.bitrate, self.listen_only = bitrate, listen_only
        self.flush_input()

    def flush_input(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def state(self) -> dict | None:
        return self.dev.state(self.channel)

    def close(self) -> None:
        self.dev.release_channel(self.channel)

    def describe(self) -> str:
        bt = self.timing
        return (f"{super().describe()} (brp {bt['brp']}, {bt['tq_total']} tq, "
                f"sample {bt['sample_point'] * 100:.1f} %, {self.dev.product})")


# ------------------------------------------------------------ enumeration
def list_can_adapters() -> list[dict]:
    """For `rsh ports`: gs_usb devices (opened briefly to read the channel
    count), Slcan COM ports and DFU-stuck boards by VID:PID. Never raises:
    a missing libusb or a busy device becomes a note in the entry."""
    out: list[dict] = []
    try:
        import usb1
    except ImportError:
        out.append({"device": "can:gs:?", "description": "python-libusb1 not installed "
                    "(pip install libusb1) - gs_usb adapters cannot be listed",
                    "hwid": "", "kind": "note"})
        usb1 = None
    if usb1 is not None:
        try:
            with usb1.USBContext() as ctx:
                devs = [d for d in ctx.getDeviceList()
                        if (d.getVendorID(), d.getProductID()) in set(GS_USB_IDS) | set(DFU_IDS)]
        except Exception as e:                   # libusb backend missing etc.
            devs = []
            out.append({"device": "can:gs:?", "description": f"libusb enumeration failed: {e}",
                        "hwid": "", "kind": "note"})
        idx = 0
        for d in sorted(devs, key=lambda d: (d.getBusNumber(), d.getDeviceAddress())):
            key = (d.getVendorID(), d.getProductID())
            hwid = f"USB VID:PID={key[0]:04X}:{key[1]:04X}"
            if key in DFU_IDS:
                out.append({"device": "(dfu)", "description": DFU_IDS[key], "hwid": hwid,
                            "kind": "dfu"})
                continue
            try:
                dev = GsUsbDevice.get(CanSpec("gs", str(idx), 0))
                info = dev.describe()
                dev.close()
                for ch in range(info["channels"]):
                    out.append({"device": f"can:gs:{idx}.{ch}", "hwid": hwid, "kind": "can",
                                "description": f"{info['product']} channel {ch} "
                                               f"({GS_USB_IDS[key]}, fw {info['sw_version']}, "
                                               f"{info['fclk_can'] // 1_000_000} MHz CAN clock"
                                               f"{', serial ' + info['serial'] if info['serial'] else ''})",
                                "features": info["features"]})
            except Exception as e:
                out.append({"device": f"can:gs:{idx}", "hwid": hwid, "kind": "can",
                            "description": f"{GS_USB_IDS[key]} - could not open: {e}"})
            idx += 1
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            for (vid, pid), desc in SLCAN_IDS.items():
                if p.vid == vid and p.pid == pid:
                    out.append({"device": f"can:slcan:{p.device}", "hwid": p.hwid,
                                "kind": "can", "description": f"{desc}; {p.description}"})
    except Exception:
        pass
    if os.path.isdir("/sys/class/net"):
        for name in sorted(os.listdir("/sys/class/net")):
            try:
                with open(f"/sys/class/net/{name}/type") as fh:
                    if fh.read().strip() == "280":         # ARPHRD_CAN
                        out.append({"device": f"can:socketcan:{name}", "hwid": "socketcan",
                                    "kind": "can", "description": "SocketCAN interface "
                                    "(bitrate/listen-only via ip link)"})
            except OSError:
                continue
    return out


def describe_fd_device(fd: int) -> dict:
    """Android: what is behind the termux-usb descriptor? ('ftdi' | 'gs_usb'
    | 'other') so `rsh ports` can list usb:A/B or can:gs:.0/.1."""
    import usb1
    from .ftdi_usb import VENDOR_FTDI, _close_wrapped_handle
    ctx = usb1.USBContext(with_device_discovery=False)
    ctx.open()
    try:
        h = ctx.wrapSysDevice(fd)
        try:
            d = h.getDevice()
            vid, pid = d.getVendorID(), d.getProductID()
        finally:
            _close_wrapped_handle(h)
    finally:
        ctx.close()
    kind = "ftdi" if vid == VENDOR_FTDI else "gs_usb" if (vid, pid) in GS_USB_IDS \
        else "dfu" if (vid, pid) in DFU_IDS else "other"
    return {"vid": vid, "pid": pid, "kind": kind,
            "hwid": f"USB VID:PID={vid:04X}:{pid:04X}"}
