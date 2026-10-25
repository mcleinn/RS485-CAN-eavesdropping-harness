"""Hardware-free tests for the CAN layer: gs_usb frame pack/unpack and bit
timing, port specs, open_link dispatch, the virtual CAN bus ACK model and
the session record shape."""
import json
import struct

import pytest

from rs485harness.core import canbus as C
from rs485harness.core import gs_usb as G
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.core.transport import open_link, is_can_link


# ------------------------------------------------------------ gs_usb frames
def test_pack_host_frame_classic_and_extended():
    m = C.CanMsg(0.0, 0x351, bytes.fromhex("2802F401F401F401"), channel=1)
    buf = G.pack_host_frame(3, m)
    assert len(buf) == 20
    echo_id, can_id, dlc, ch, flags, _ = struct.unpack_from("<IIBBBB", buf)
    assert (echo_id, can_id, dlc, ch, flags) == (3, 0x351, 8, 1, 0)
    assert buf[12:] == bytes.fromhex("2802F401F401F401")
    # Huawei status request: 29-bit id, EFF flag set, 8 zero bytes
    m = C.CanMsg(0.0, 0x108040FE, bytes(8), ext=True)
    buf = G.pack_host_frame(0, m)
    assert struct.unpack_from("<I", buf, 4)[0] == 0x108040FE | C.CAN_EFF_FLAG
    # short frames are zero padded, never truncated
    m = C.CanMsg(0.0, 0x35C, b"\xC0\x00")
    buf = G.pack_host_frame(0, m, with_ts=True)
    assert len(buf) == 24 and buf[12] == 0xC0 and buf[14:20] == bytes(6)
    with pytest.raises(ValueError):
        G.pack_host_frame(0, C.CanMsg(0.0, 1, bytes(9)))


def test_unpack_host_frames_rx_echo_error_and_timestamp():
    rx = struct.pack("<IIBBBB", G.ECHO_ID_RX, 0x355, 6, 0, 0, 0) + bytes.fromhex("5A00640000000000")
    echo = struct.pack("<IIBBBB", 2, 0x305, 8, 1, 0, 0) + bytes(8)
    err = struct.pack("<IIBBBB", G.ECHO_ID_RX, C.CAN_ERR_FLAG | C.CAN_ERR_ACK | C.CAN_ERR_CNT,
                      8, 0, 0, 0) + bytes([0, 0, 0, 0, 0, 0, 96, 0])
    msgs = G.unpack_host_frames(rx + echo + err, 1.0, G.FRAME_LEN)
    assert [m.id_str for m in msgs] == ["0x355", "0x305", "ERR"]
    assert msgs[0].data == bytes.fromhex("5A0064000000") and not msgs[0].echo
    assert msgs[1].echo and msgs[1].channel == 1 and msgs[1].extra["echo_id"] == 2
    assert msgs[2].err and "no-ack" in msgs[2].describe_error()
    assert "txerr=96" in msgs[2].describe_error()
    # with hardware timestamps the frame is 24 bytes
    ts_frame = rx + struct.pack("<I", 123456)
    (m,) = G.unpack_host_frames(ts_frame, 2.0, G.FRAME_LEN_TS)
    assert m.ts_hw_us == 123456 and m.ts == 2.0
    # a partial trailing frame is ignored, not mis-parsed
    assert len(G.unpack_host_frames(rx + rx[:7], 0.0, G.FRAME_LEN)) == 1


def test_extended_id_roundtrip_and_str():
    m = C.CanMsg.from_raw_id(0x1081407F | C.CAN_EFF_FLAG, b"\x01\x75\x00\x00\x00\x00\xd2\x00", 0.0)
    assert m.ext and m.can_id == 0x1081407F and m.id_str == "0x1081407F"
    assert m.raw_id() == 0x1081407F | C.CAN_EFF_FLAG
    assert "0x1081407F [8] 01 75 00 00 00 00 D2 00" in str(m)
    m11 = C.CanMsg.from_raw_id(0x7FF, b"", 0.0)
    assert not m11.ext and m11.id_str == "0x7FF"


# ----------------------------------------------------------------- timing
BTC_BXCAN = {"tseg1_min": 1, "tseg1_max": 16, "tseg2_min": 1, "tseg2_max": 8,
             "sjw_max": 4, "brp_min": 1, "brp_max": 1024, "brp_inc": 1}
BTC_FDCAN = {"tseg1_min": 1, "tseg1_max": 256, "tseg2_min": 1, "tseg2_max": 128,
             "sjw_max": 128, "brp_min": 1, "brp_max": 512, "brp_inc": 1}


@pytest.mark.parametrize("fclk,btc", [(48_000_000, BTC_BXCAN), (160_000_000, BTC_FDCAN),
                                      (80_000_000, BTC_FDCAN), (16_000_000, BTC_BXCAN)])
@pytest.mark.parametrize("rate", [125_000, 250_000, 500_000, 1_000_000])
def test_bit_timing_hits_rate_and_sample_point(fclk, btc, rate):
    bt = G.calc_bit_timing(fclk, rate, btc)
    tq = 1 + bt["prop_seg"] + bt["phase_seg1"] + bt["phase_seg2"]
    assert tq == bt["tq_total"]
    assert fclk / (bt["brp"] * tq) == pytest.approx(rate, rel=1e-3)
    assert abs(bt["sample_point"] - G.cia_sample_point(rate)) < 0.03
    assert btc["tseg1_min"] <= bt["prop_seg"] + bt["phase_seg1"] <= btc["tseg1_max"]
    assert btc["tseg2_min"] <= bt["phase_seg2"] <= btc["tseg2_max"]
    assert 1 <= bt["sjw"] <= min(btc["sjw_max"], bt["phase_seg2"])
    assert len(G.pack_bit_timing(bt)) == 20


def test_bit_timing_known_candlelight_value():
    # candleLight on 48 MHz bxCAN, 500 kbit/s: brp 6, 16 tq, 87.5 %
    bt = G.calc_bit_timing(48_000_000, 500_000, BTC_BXCAN)
    assert (bt["brp"], bt["tq_total"], bt["sample_point"]) == (6, 16, 0.875)
    with pytest.raises(C.CanError):
        G.calc_bit_timing(48_000_000, 7_000_000, BTC_BXCAN)


def test_unpack_device_config_and_bt_const():
    cfg = G.unpack_device_config(struct.pack("<4B2I", 0, 0, 0, 1, 0x20500, 0x100))
    assert cfg["channels"] == 2 and cfg["sw_version"] == 0x20500
    btc = G.unpack_bt_const(struct.pack("<10I", G.FEAT_LISTEN_ONLY | G.FEAT_HW_TIMESTAMP,
                                        160_000_000, 1, 256, 1, 128, 128, 1, 512, 1))
    assert btc["fclk_can"] == 160_000_000 and btc["brp_max"] == 512
    assert G.feature_names(btc["feature"]) == ["listen-only", "hw-timestamp"]


# ------------------------------------------------------------ port specs
def test_parse_can_spec_variants(monkeypatch):
    monkeypatch.delenv("RSH_USB_FD", raising=False)
    s = C.parse_can_spec("can:gs:0.1")
    assert (s.backend, s.device, s.channel, s.index, s.fd) == ("gs", "0", 1, 0, None)
    assert C.parse_can_spec("can:gs:2").index == 2
    assert C.parse_can_spec("CAN:GS").device == ""
    assert C.parse_can_spec("can:gs:fd12.1").fd == 12
    monkeypatch.setenv("RSH_USB_FD", "33")
    assert C.parse_can_spec("can:gs:.1").fd == 33
    assert C.parse_can_spec("can:gs:0").fd is None     # explicit index wins
    v = C.parse_can_spec("can:virtual:lab")
    assert (v.backend, v.device) == ("virtual", "lab")
    assert C.parse_can_spec("can:socketcan:can0").device == "can0"
    assert C.parse_can_spec("can:slcan:COM9").device == "COM9"
    for bad in ("COM5", "can:gs:x", "can:gs:0.b", "can:socketcan"):
        with pytest.raises(ValueError):
            C.parse_can_spec(bad)
    assert C.is_can_spec("can:gs:0") and not C.is_can_spec("usb:A")


def test_open_link_dispatches_can_specs():
    a = open_link("can:virtual:t-dispatch", 9600, name="a")       # 9600 is no bitrate
    try:
        assert is_can_link(a) and a.bitrate == C.DEFAULT_BITRATE
        b = open_link("can:virtual:t-dispatch", 125_000, name="b", listen_only=True)
        try:
            assert b.bitrate == 125_000 and b.listen_only
            assert b.baud == 125_000
        finally:
            b.close()
    finally:
        a.close()
    assert not is_can_link(object())


def test_open_link_gs_without_hardware_fails_cleanly():
    from rs485harness.core.gs_usb import list_can_adapters
    if any(a.get("kind") == "can" and a["device"].startswith("can:gs:") for a in list_can_adapters()):
        pytest.skip("a gs_usb adapter is plugged in")
    with pytest.raises(Exception) as ei:
        open_link("can:gs:0", 500_000)
    assert "serial" not in type(ei.value).__module__


# ------------------------------------------------------------ virtual bus
def test_virtual_bus_ack_model():
    bus = C.VirtualCanBus("t-ack")
    bms = bus.end("bms", 500_000)
    tap = bus.end("tap", 500_000, listen_only=True)
    frame = C.CanMsg(0.0, 0x351, bytes.fromhex("2802F401F401F401"))
    # only a listen-only peer: nobody ACKs -> tx timeout, tap still sees it
    with pytest.raises(C.CanTxTimeout):
        bms.send(frame)
    assert tap.recv(0.1).id_str == "0x351"
    assert bms.stats["tx_timeout"] == 1
    # the tap itself must never transmit
    with pytest.raises(C.CanError):
        tap.send(frame)
    # an active peer ACKs: sender gets its echo, everyone else the frame
    inv = bus.end("inverter", 500_000)
    bms.send(frame)
    echo = bms.recv(0.1)
    assert echo.echo and echo.id_str == "0x351"
    assert inv.recv(0.1).data == frame.data
    assert tap.recv(0.1).channel == 0
    assert bus.frames_total == 1


def test_virtual_bus_bitrate_mismatch_gives_error_frames_not_garbage():
    bus = C.VirtualCanBus("t-rate")
    a = bus.end("a", 500_000)
    b = bus.end("b", 500_000)
    wrong = bus.end("wrong", 250_000, listen_only=True)
    a.send(C.CanMsg(0.0, 0x355, b"\x5a\x00\x64\x00"))
    assert b.recv(0.1).id_str == "0x355"
    e = wrong.recv(0.1)
    assert e.err and "protocol" in e.describe_error() and "stuff" in e.describe_error()
    assert wrong.stats["err"] == 1
    wrong.set_bitrate(500_000)
    a.send(C.CanMsg(0.0, 0x355, b"\x5a\x00\x64\x00"))
    assert wrong.recv(0.1).id_str == "0x355"


def test_virtual_bus_registry_and_detach():
    x = C.VirtualCanBus.get("t-reg")
    assert C.VirtualCanBus.get("t-reg") is x
    e1, e2 = x.end("e1"), x.end("e2")
    e2.close()
    with pytest.raises(C.CanTxTimeout):
        e1.send(C.CanMsg(0.0, 0x305, bytes(8)))
    assert e2.recv(0.05) is None


# ------------------------------------------------------- session records
def test_can_session_record_shape(tmp_path):
    path = tmp_path / "s.jsonl"
    sw = SessionWriter(path)
    m = C.CanMsg(123.0, 0x108180FE, bytes.fromhex("01000000 0000D200"), ext=True, channel=1)
    C.log_can(sw, "can0", "tx", m, decoded={"reg": "online_voltage", "value": 52.5},
              flow="CTL->PSU", acked=True)
    err = C.CanMsg(124.0, C.CAN_ERR_ACK, bytes(8), err=True)
    C.log_can(sw, "can0", "rx", err)
    sw.close()
    recs = list(read_session(path))
    assert recs[0]["kind"] == "can" and recs[0]["id"] == "0x108180FE"
    assert recs[0]["ext"] is True and recs[0]["dlc"] == 8 and recs[0]["can_ch"] == 1
    assert recs[0]["hex"] == "010000000000D200" and recs[0]["acked"] is True
    assert recs[0]["flow"] == "CTL->PSU" and recs[0]["decoded"]["value"] == 52.5
    assert recs[0]["t_mono"] == 123.0
    assert recs[1]["id"] == "ERR" and recs[1]["err"] == "no-ack"
    json.dumps(recs)  # every field JSON-serialisable


# ------------------------------------------------- gs_usb tx-failure model
def test_tx_failure_tracker_follows_the_error_counter():
    """Measured on the USBCANFD DUAL (2026-10-24): every error frame carries
    TEC in data[6]; a failed attempt raises it by 8 (first report class-less,
    TEC=8), a no-ACK storm pins it at 128 with ACK-class frames every 100 ms,
    recovery shows CRTL frames with the TEC falling by 1 per frame, then
    class-less counter reports. Only a rising counter (or an ACK/bus-error
    class with a non-falling counter) is a failure; a falling counter is proof
    that frames go through."""
    from rs485harness.core.gs_usb import TxFailureTracker, _is_tx_failure_class
    from rs485harness.core.canbus import (CAN_ERR_FLAG, CAN_ERR_ACK, CAN_ERR_CRTL,
                                          CAN_ERR_TX_TIMEOUT, CAN_ERR_BUSERROR, CAN_ERR_LOSTARB)
    E = CAN_ERR_FLAG
    assert _is_tx_failure_class(E | CAN_ERR_ACK)
    assert not _is_tx_failure_class(E | CAN_ERR_BUSERROR)        # can be a receive error
    assert not _is_tx_failure_class(E) and not _is_tx_failure_class(E | CAN_ERR_CRTL)
    assert not _is_tx_failure_class(E | CAN_ERR_LOSTARB)

    def tec(n, b1=0):
        return bytes([0, b1, 0, 0, 0, 0, n, 0])
    tr = TxFailureTracker(grace_s=0.02, recover_s=0.6)
    clock = [0.0]
    now = lambda: clock[0]
    tr.reset(0)
    tr.before_send(0)
    assert tr.wait_verdict(0, now) is None                      # healthy
    # after a restart the baseline is unknown: the first class-less report only
    # sets it (measured: TEC 9 left over from the previous session while the
    # module was answering); an ACK-class frame is a failure regardless
    tr.before_send(0)
    tr.note_error(0, E, tec(9), 0.5)
    assert tr.wait_verdict(0, now) is None and tr.last_tec[0] == 9 and tr.failures == {}
    tr.before_send(0)
    tr.note_error(0, E, tec(8), 0.6)                            # falling: fine
    assert tr.wait_verdict(0, now) is None
    # a dead bus: class-less TEC report then the ACK-class frame -> failure
    tr.reset(0)
    tr.before_send(0)
    tr.note_error(0, E, tec(8), 0.9)                            # baseline only
    tr.note_error(0, E | CAN_ERR_ACK, tec(8), 1.0); clock[0] = 1.0
    assert "no ACK" in tr.wait_verdict(0, now)
    # storm: ACK-class frames with TEC pinned at 128 keep it failing; queued frames refused
    for k, t in enumerate((1.1, 1.5, 1.6, 1.7)):
        tr.note_error(0, E | CAN_ERR_CRTL | CAN_ERR_ACK | (CAN_ERR_TX_TIMEOUT if k == 1 else 0),
                      tec(128, 0x20), t)
        clock[0] = t + 0.05
        tr.before_send(0)
        assert "tx-failure state" in tr.wait_verdict(0, now)
    # recovery: CRTL frames with falling TEC -> cleared at once, no timer needed
    tr.note_error(0, E | CAN_ERR_CRTL, tec(127, 0x08), 2.5); clock[0] = 2.55
    tr.before_send(0)
    assert tr.wait_verdict(0, now) is None and tr.recoveries == {0: 1}
    # class-less counter reports while the TEC keeps falling are NOT failures
    for n, t in ((90, 3.0), (89, 3.1), (76, 3.2)):
        tr.note_error(0, E, tec(n), t)
    clock[0] = 3.25
    tr.before_send(0)
    assert tr.wait_verdict(0, now) is None and not tr.failing(0, now())
    # a rising counter again is a failure, even class-less
    tr.before_send(0)
    tr.note_error(0, E, tec(84), 4.0); clock[0] = 4.0
    assert tr.wait_verdict(0, now)
    # timer fallback: no failure report for longer than the retry window
    clock[0] = 4.61
    assert not tr.failing(0, now())
    # channel restart forgets the baseline; channels are independent
    tr.reset(0)
    tr.note_error(1, E | CAN_ERR_ACK, tec(8), 5.0); clock[0] = 5.0
    assert tr.failing(1, now()) and not tr.failing(0, now())
    assert tr.failures == {0: 6, 1: 1}
