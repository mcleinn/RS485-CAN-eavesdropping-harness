"""Huawei R48xx protocol + PSU simulator + `rsh huawei` logic, all over the
virtual CAN bus (no hardware)."""
import time

import pytest

from rs485harness.core import canbus as C
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.protocols.huawei_r48xx import ids as I
from rs485harness.protocols.huawei_r48xx import regs as R
from rs485harness.simulators.huawei_psu_sim import HuaweiPsuSimulator
from rs485harness.analyzer.huawei import HuaweiClient, huawei_config
from rs485harness.analyzer.identify_can import identify_can


# ----------------------------------------------------------------- ids/regs
def test_id_pack_unpack_worked_examples():
    assert I.request_id(I.CMD_DATA, 0) == 0x108040FE
    assert I.request_id(I.CMD_DATA, 1) == 0x108140FE
    assert I.request_id(I.CMD_SET, 1) == 0x108180FE
    assert I.request_id(I.CMD_SET, 0) == 0x108080FE
    assert I.request_id(I.CMD_INFO, 1) == 0x108150FE
    assert I.request_id(I.CMD_ELABEL, 1) == 0x1081D2FE
    assert I.reply_id(I.CMD_DATA, 1, True) == 0x1081407F
    assert I.reply_id(I.CMD_DATA, 1, False) == 0x1081407E
    assert I.reply_id(I.CMD_SET, 1, False) == 0x1081807E
    assert I.ID_UNSOL_PSU1 == 0x1001117E
    h = I.HuaweiId.unpack(0x1082407F)
    assert (h.proto, h.addr, h.cmd, h.to_psu, h.cont) == (0x21, 2, 0x40, False, True)
    assert h.is_huawei and str(h) == "data from psu2 (more)"
    assert I.looks_huawei(0x1001117E, True) and not I.looks_huawei(0x351, False)
    assert not I.looks_huawei(0x18BB0001, True)        # Dyness native frame


def test_set_frame_matches_sources():
    # 52.5 V -> 0x108180FE 01 00 00 00 00 00 D2 00 (craigpeacock / NOTES 3.6)
    msg, raw = R.set_frame("online_voltage", 52.5, addr=1)
    assert msg.can_id == 0x108180FE and msg.ext
    assert msg.data.hex().upper() == "010000000000D200" and raw == 0xD200
    msg, raw = R.set_frame("default_voltage", 48.0)
    assert msg.data[1] == 0x01 and msg.data[6:8] == bytes.fromhex("C000")
    # 10 A on a 35 A unit under ratio1024 = 293 = 01 25 (patagonaa)
    msg, raw = R.set_frame("online_current", 10.0, nominal_a=35.0, scale="ratio1024")
    assert raw == 293 and msg.data[6:8] == bytes.fromhex("0125")
    assert R.set_frame("online_current", 1.0, scale="x20")[1] == 20          # 00 14
    assert R.set_frame("online_current", 50.0, scale="x30")[1] == 1500
    with pytest.raises(ValueError):
        R.set_frame("online_current", 10.0, scale="ratio1024")                 # no nominal
    msg, raw = R.set_frame("standby", 1)
    assert msg.data.hex().upper() == "0132000100000000"
    assert R.voltage_in_range("online_voltage", 41.0) and not R.voltage_in_range("online_voltage", 58.7)
    assert not R.voltage_in_range("default_voltage", 47.9)


def test_decode_status_ack_info_unsolicited():
    # output voltage 53.5 V: 53.5*1024 = 54784 = 0xD600
    m = C.CanMsg(0.0, 0x1081407F, bytes.fromhex("0175000000 00D600".replace(" ", "")), ext=True)
    d = R.decode(m)
    assert d["kind"] == "status" and d["name"] == "out_voltage_v" and d["value"] == 53.5
    assert d["cont"] and d["flow"] == "PSU->CTL" and d["addr"] == 1
    last = C.CanMsg(0.0, 0x1081407E, bytes.fromhex("0183000000000200"), ext=True)
    d = R.decode(last)
    assert d["name"] == "status_bits" and not d["cont"] and "standby-via-pin" in d["flags"]
    # ack ok / error
    ok = R.decode(C.CanMsg(0.0, 0x1081807E, bytes.fromhex("010000000000D200"), ext=True))
    assert ok["kind"] == "ack" and ok["ok"] and ok["value_v"] == 52.5
    bad = R.decode(C.CanMsg(0.0, 0x1081807E, bytes.fromhex("210000000000F200"), ext=True))
    assert not bad["ok"] and "0x21" in bad["error"]
    # info characteristic (patagonaa R4850G2 example) -> 52 A
    info = R.decode(C.CanMsg(0.0, 0x1081507F, bytes.fromhex("0001000040680e27"), ext=True))
    assert info["kind"] == "info" and info["nominal_current_a"] == 52.0
    slot = R.decode(C.CanMsg(0.0, 0x1081507E, bytes.fromhex("0006010100000000"), ext=True))
    assert slot["slot_id"] == "0x0101"
    # unsolicited
    u = R.decode(C.CanMsg(0.0, 0x1001117E, bytes.fromhex("0001000100000200"), ext=True),
                 nominal_a=52)
    assert u["kind"] == "unsolicited" and u["output_disabled"] and u["load_ratio"] == 0.5
    assert u["load_a_ratio1024"] == 26.0 and u["flow"] == "PSU->CTL"
    # requests decode too, and non-Huawei ids give None
    assert R.decode(C.CanMsg(0.0, 0x108040FE, bytes(8), ext=True))["kind"] == "status_request"
    assert R.decode(C.CanMsg(0.0, 0x351, bytes(8))) is None
    assert R.decode(C.CanMsg(0.0, 0x18BB0001, bytes(8), ext=True)) is None


def test_status_burst_and_elabel():
    b = R.StatusBurst()
    frames = [(0x75, 53.5 * 1024), (0x81, 10.25 * 1024), (0x76, 0.5 * 1024)]
    for reg, raw in frames:
        b.feed(C.CanMsg(0.0, 0x1081407F, bytes([1, reg, 0, 0]) + int(raw).to_bytes(4, "big"), ext=True))
    assert not b.complete
    b.feed(C.CanMsg(0.0, 0x1081407E, bytes([1, 0x83, 0, 0, 0, 0, 0, 0]), ext=True))
    snap = b.snapshot(nominal_a=52, scale="x20")
    assert snap["complete"] and snap["out_voltage_v"] == 53.5 and snap["out_current_a"] == 10.25
    assert snap["current_limit_ratio"] == 0.5 and snap["current_limit_a_ratio1024"] == 26.0
    assert snap["current_limit_a_x20"] == 512 / 20
    el = R.ELabel()
    text = b"[Board Properties]\r\nBoardType=R4850G2\r\nBarCode=21023X\r\n"
    chunks = [text[i:i + 6] for i in range(0, len(text), 6)]
    for i, c in enumerate(chunks):
        el.feed(C.CanMsg(0.0, 0x1081D27F if i < len(chunks) - 1 else 0x1081D27E,
                         b"\0\0" + c.ljust(6, b"\0"), ext=True))
    assert el.complete and el.fields()["BoardType"] == "R4850G2"


# ------------------------------------------------------------ sim + client
@pytest.fixture
def bench(tmp_path):
    """PSU sim (52 A nominal, ratio1024, 2 ohm load) and a controller link on
    one virtual bus; 2 s online timeout to test the revert."""
    bus = C.VirtualCanBus(f"huawei-{time.time()}")
    sess = SessionWriter(tmp_path / "s.jsonl")
    psu_link = bus.end("psu", I.BITRATE)
    sim = HuaweiPsuSimulator(psu_link, sess, addr=1, nominal_a=52.0, scale="ratio1024",
                             load_ohm=2.0, timeout_s=2.0, unsol_period_s=0.1)
    sim.start()
    ctl = bus.end("ctl", I.BITRATE)
    yield bus, sess, sim, ctl
    sim.stop()
    sess.close()


def test_client_status_and_info(bench):
    bus, sess, sim, ctl = bench
    cl = HuaweiClient(ctl, sess, "can", 1, huawei_config({}))
    snap = cl.status()
    assert snap["complete"] and snap["frames"] == 14
    assert snap["out_voltage_v"] == 53.5
    assert snap["out_current_a"] == pytest.approx(53.5 / 2, abs=0.01)
    assert snap["ac_in_voltage_v"] == 230.0 and snap["status_flags"] == []
    assert snap["current_limit_ratio"] == pytest.approx(1.0, abs=0.01)
    info = cl.info()
    assert info["nominal_current_a"] == 52.0 and cl.nominal_a == 52.0
    assert info["slot_id"] == "0x0101" and info["elabel"]["BoardType"] == "R4850G2"
    assert info["elabel_complete"]
    assert cl.unsolicited >= 1          # the 0x1001117E heartbeat was seen meanwhile
    assert cl.other_controller_frames == 0


def test_set_refusals_and_dry_run(bench):
    bus, sess, sim, ctl = bench
    cl = HuaweiClient(ctl, sess, "can", 1, huawei_config({}))     # caps 55.2 V / 0 A, no scale
    rep = cl.set_points(56.0, None)
    assert rep["result"] == "refused" and "exceeds cap" in rep["refusals"][0]
    rep = cl.set_points(52.0, 10.0)
    assert rep["result"] == "refused"
    assert any("current 10.0 A exceeds cap 0" in r for r in rep["refusals"])
    assert any("scale" in r for r in rep["refusals"])
    rep = cl.set_points(52.5, None)                               # dry run
    assert rep["result"] == "dry-run" and rep["plan"][0]["hex"] == "01 00 00 00 00 00 D2 00"
    assert sim.stats["sets"] == 0 and sim.online_v == 53.5     # nothing was sent
    # out of the range real units accept -> refused before sending
    cl2 = HuaweiClient(ctl, sess, "can", 1, huawei_config({"huawei": {"caps": {"voltage_max": 60}}}))
    rep = cl2.set_points(59.0, None, apply=True)
    assert rep["result"] == "refused" and sim.stats["sets"] == 0


def test_set_apply_ack_and_verify_by_measurement(bench):
    bus, sess, sim, ctl = bench
    cl = HuaweiClient(ctl, sess, "can", 1, huawei_config({}))
    rep = cl.set_points(52.5, None, apply=True)
    assert rep["result"] == "applied", rep
    assert rep["sent"][0]["acked"] and rep["sent"][0]["ack"]["ok"]
    assert rep["verify"]["verified"] and rep["verify"]["measured_v"] == 52.5
    assert sim.online_v == 52.5 and sim.online
    # persist writes the non-volatile register and is NOT verified by 0x75
    rep = cl.set_points(50.0, None, apply=True, persist=True)
    assert rep["result"] == "applied" and "verify" not in rep
    assert sim.default_v == 50.0 and rep["sent"][0]["name"] == "default_voltage"
    # audit trail: every tx record carries acked + cap
    recs = [r for r in read_session(sess.path) if r.get("dir") == "tx" and r["chan"] == "can"]
    sets = [r for r in recs if r["decoded"]["kind"] == "set"]
    assert sets and all("acked" in r and "cap" in r for r in sets)
    assert sets[0]["id"] == "0x108180FE" and sets[0]["hex"] == "010000000000D200"


def test_set_error_ack_is_reported_not_hidden(bench):
    bus, sess, sim, ctl = bench
    hc = huawei_config({"huawei": {"caps": {"voltage_max": 60}}})
    cl = HuaweiClient(ctl, sess, "can", 1, hc)
    # 58.6 is inside the documented range but make the sim stricter to force 0x21
    sim_range = R.VOLTAGE_RANGES["online_voltage"]
    R.VOLTAGE_RANGES["online_voltage"] = (41.0, 58.0)
    try:
        rep = cl.set_points(58.5, None, apply=True)
    finally:
        R.VOLTAGE_RANGES["online_voltage"] = sim_range
    # client range check uses the same table, so it refused up front:
    assert rep["result"] == "refused"
    # now a genuine 0x21 from the sim: ask the sim for a value its (default) table rejects
    sim.online = True
    bad, raw = R.set_frame("online_voltage", 58.5)
    R.VOLTAGE_RANGES["online_voltage"] = (41.0, 58.0)   # sim side only
    try:
        res = cl.send_set({"name": "online_voltage", "value": 58.5, "unit": "V", "raw": raw,
                           "id": bad.id_str, "hex": bad.data.hex(" ").upper(), "msg": bad})
    finally:
        R.VOLTAGE_RANGES["online_voltage"] = sim_range
    assert res["acked"] and res["ok"] is False and res["ack"]["status_byte"] == 0x21
    assert sim.stats["set_errors"] == 1 and sim.online_v == 53.5


def test_current_setpoint_with_scale_and_cap(bench):
    bus, sess, sim, ctl = bench
    hc = huawei_config({"huawei": {"caps": {"voltage_max": 55.2, "current_max": 30},
                                   "units": {"0000000000A1": {"addr": 1, "nominal_a": 52.0,
                                                              "current_scale": "ratio1024"}},
                                   "addr_map": {"1": "0000000000A1"}}})
    cl = HuaweiClient(ctl, sess, "can", 1, hc)
    rep = cl.set_points(None, 10.0, apply=True)
    assert rep["result"] == "applied" and rep["plan"][0]["raw"] == round(10 / 52 * 1024)
    assert rep["identity"] == []                     # serial on addr 1 checked before sending
    time.sleep(0.05)
    snap = cl.status()
    assert snap["out_current_a"] == pytest.approx(10.0, abs=0.05)     # limited
    assert snap["out_voltage_v"] == pytest.approx(20.0, abs=0.1)      # 10 A into 2 ohm
    assert snap["current_limit_ratio"] == pytest.approx(10 / 52, abs=0.01)
    assert cl.set_points(None, 31.0, apply=True)["result"] == "refused"


@pytest.mark.parametrize("unit_scale", ["ratio1024", "x20", "x30"])
def test_calibrate_current_finds_the_units_scale(tmp_path, unit_scale):
    bus = C.VirtualCanBus(f"cal-{unit_scale}-{time.time()}")
    sess = SessionWriter(tmp_path / "c.jsonl")
    sim = HuaweiPsuSimulator(bus.end("psu", I.BITRATE), sess, nominal_a=52.0, scale=unit_scale,
                             load_ohm=2.0, timeout_s=5.0, unsol_period_s=0.2)
    sim.start()
    try:
        hc = huawei_config({"huawei": {"caps": {"voltage_max": 55.2, "current_max": 40}}})
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, hc)
        rep = cl.calibrate_current(test_a=5.0, settle_s=0.05)
        # on a 52 A unit ratio1024 (x19.7) and x20 cannot be told apart (and need
        # not be); x30 is 1.5x off and must be distinguished
        assert unit_scale in rep["equivalent"], rep
        assert rep["result"] in rep["equivalent"] and cl.scale == rep["result"]
        if unit_scale == "x30":
            assert rep["result"] == "x30" and rep["equivalent"] == ["x30"]
        else:
            assert rep["equivalent"] == ["ratio1024", "x20"] and "agree within" in rep["note"]
        assert rep["nominal_a"] == 52.0
        # a candidate that a different scale would push over the cap is skipped
        assert all(c.get("worst_case_a", 0) <= 40 or "skipped" in c for c in rep["candidates"])
        # limit released again afterwards
        time.sleep(0.05)
        assert cl.status()["out_current_a"] > 5.5
    finally:
        sim.stop()
        sess.close()


def test_calibrate_refuses_without_load(bench):
    bus, sess, sim, ctl = bench
    sim.load_ohm = 100.0                                   # 0.5 A only
    cl = HuaweiClient(ctl, sess, "can", 1, huawei_config({"huawei": {"caps": {"current_max": 40}}}))
    rep = cl.calibrate_current(test_a=5.0, settle_s=0.0)
    assert rep["result"] is None and "load draws only" in rep["error"]


def test_hold_keeps_psu_online_and_revert_without_it(bench):
    bus, sess, sim, ctl = bench
    cl = HuaweiClient(ctl, sess, "can", 1, huawei_config({}))
    events = []
    cl.on_event = events.append
    stats = cl.hold(52.0, None, duration=1.2, status_period=0.2, resend_period=0.5)
    assert stats["resends"] >= 2 and stats["status_polls"] >= 4 and not stats.get("stop_reason")
    assert sim.online and sim.online_v == 52.0 and sim.stats["reverts"] == 0
    assert any("CHANGE out_voltage_v" in e for e in events)   # 53.5 -> 52.0 seen once
    # stop commanding: the sim reverts after its 2 s timeout like the real unit
    time.sleep(2.6)
    assert sim.stats["reverts"] == 1 and not sim.online
    assert sim.measure()[0] == 53.5
    # hold refuses what set refuses
    assert cl.hold(56.0, None, duration=0.1)["refusals"]


def test_no_ack_when_psu_absent(tmp_path):
    bus = C.VirtualCanBus(f"empty-{time.time()}")
    sess = SessionWriter(tmp_path / "e.jsonl")
    cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, huawei_config({}))
    snap = cl.status(timeout=0.1)
    assert snap["error"].startswith("no ACK") and not snap["complete"]
    rec = [r for r in read_session(sess.path) if r.get("dir") == "tx"][0]
    assert rec["acked"] is False and rec["errors"]
    sess.close()


# ---------------------------------------------------------------- identify
def test_identify_can_finds_huawei_psu_passively_then_asks_once(bench):
    bus, sess, sim, ctl = bench
    tap = bus.end("tap", 500_000, listen_only=True)          # starts at the wrong rate
    events = []
    rep = identify_can(tap, sess, "id", listen_s=0.4, bitrates=(500_000, 250_000, 125_000),
                       on_event=events.append)
    assert rep["facts"]["bitrate"] == 125_000
    assert rep["facts"]["huawei_psu_addrs"] == [1]
    assert rep["facts"]["huawei_controller_present"] is False
    assert any(v.startswith("HUAWEI R48xx PSU @ 125k") for v in rep["verdicts"])
    assert any(v.startswith("RESPONDER: Huawei PSU addr 1") for v in rep["verdicts"])
    assert rep["active"][0]["status"]["complete"] and rep["active"][0]["info"]["nominal_current_a"] == 52.0
    assert tap.listen_only                                   # back to listen-only afterwards
    # the wrong-rate windows produced error-frame verdicts, not garbage
    assert "500k" in rep["passive"] and rep["passive"]["500k"]["frames"] == 0


def test_identify_can_does_not_send_when_another_controller_is_active(bench):
    bus, sess, sim, ctl = bench
    other = HuaweiClient(ctl, sess, "other", 1, huawei_config({}))
    stop = False

    import threading

    def poll():
        while not stop:
            other.status(timeout=0.1)
            time.sleep(0.1)
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    try:
        tap = bus.end("tap2", I.BITRATE, listen_only=True)
        rep = identify_can(tap, sess, "id", listen_s=0.5, bitrates=(125_000,))
    finally:
        stop = True
        t.join()
    assert rep["facts"]["huawei_controller_present"] is True
    assert not rep["active"]
    assert any("SKIPPED" in v for v in rep["verdicts"])


def test_identify_can_silence_verdict(tmp_path):
    bus = C.VirtualCanBus(f"silent-{time.time()}")
    sess = SessionWriter(tmp_path / "q.jsonl")
    rep = identify_can(bus.end("tap", 500_000, listen_only=True), sess, listen_s=0.1,
                       bitrates=(500_000, 125_000))
    assert rep["verdicts"][0].startswith("SILENCE")
    sess.close()


def test_rsh_identify_routes_can_ports(tmp_path, monkeypatch):
    """`rsh identify --port can:virtual:...` goes through the CAN path."""
    from rs485harness.cli import main
    name = f"cli-{int(time.time() * 1000)}"
    bus = C.VirtualCanBus.get(name)
    sess = SessionWriter(tmp_path / "sim.jsonl")
    sim = HuaweiPsuSimulator(bus.end("psu", I.BITRATE), sess, unsol_period_s=0.1)
    sim.start()
    try:
        rc = main(["identify", "--port", f"can:virtual:{name}", "--listen", "0.3",
                   "--bitrates", "125000", "--session", str(tmp_path / "id.jsonl"), "--json"])
    finally:
        sim.stop()
        sess.close()
    assert rc == 0
    recs = list(read_session(tmp_path / "id.jsonl"))
    assert any(r.get("kind") == "can" and r.get("id") == "0x1001117E" for r in recs)


def test_rsh_huawei_cli_caps_status_set(tmp_path, monkeypatch, capsys):
    """caps edits the config (never the repo's), status/set go over the bus,
    set is a dry run unless --apply, refusals come back as exit 1."""
    import json
    from rs485harness import runner, cli
    monkeypatch.setattr(runner, "CONFIG_PATH", tmp_path / "cfg.json")
    assert cli.main(["huawei", "caps", "--voltage-max", "54.0", "--current-max", "20",
                     "--current-scale", "ratio1024", "--nominal-a", "52",
                     "--serial", "0000000000A1"]) == 0
    cfg = json.loads((tmp_path / "cfg.json").read_text())
    assert cfg["huawei"]["caps"] == {"voltage_max": 54.0, "current_max": 20}
    assert cfg["huawei"]["units"]["0000000000A1"]["current_scale"] == "ratio1024"
    assert cli.main(["huawei", "caps", "--current-scale", "bogus", "--serial", "0000000000A1"]) == 2
    assert cli.main(["huawei", "caps", "--current-scale", "x20"]) == 2      # no serial bound yet
    name = f"cli2-{int(time.time() * 1000)}"
    bus = C.VirtualCanBus.get(name)
    sess = SessionWriter(tmp_path / "sim.jsonl")
    sim = HuaweiPsuSimulator(bus.end("psu", I.BITRATE), sess, load_ohm=10.0, unsol_period_s=0.1)
    sim.start()                                            # 10 ohm: 5.25 A at 52.5 V
    try:
        port = f"can:virtual:{name}"
        capsys.readouterr()
        assert cli.main(["huawei", "status", "--port", port, "--json",
                         "--session", str(tmp_path / "st.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"]["complete"] and out["status"]["out_voltage_v"] == 53.5
        assert cli.main(["huawei", "info", "--port", port, "--json", "--label", "B1.1/x",
                         "--session", str(tmp_path / "info.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["info"]["bound"] == {"serial_hex": "0000000000A1", "addr": 1}
        cfg = json.loads((tmp_path / "cfg.json").read_text())
        assert cfg["huawei"]["addr_map"] == {"1": "0000000000A1"}
        assert "_migrated" not in cfg["huawei"]
        sm = [r for r in read_session(tmp_path / "info.jsonl") if r.get("kind") == "summary"]
        assert sm and sm[0]["summary"]["label"] == "B1.1/x" and sm[0]["summary"]["sub"] == "info"
        # dry run: nothing reaches the sim
        assert cli.main(["huawei", "set", "--port", port, "--voltage", "52.5", "--json",
                         "--session", str(tmp_path / "set.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["result"] == "dry-run" and sim.stats["sets"] == 0
        # above the cap -> refused, exit 1, still nothing sent
        assert cli.main(["huawei", "set", "--port", port, "--voltage", "54.5", "--apply",
                         "--json", "--session", str(tmp_path / "set2.jsonl")]) == 1
        assert sim.stats["sets"] == 0
        capsys.readouterr()
        # inside the cap with --apply: sent, acked, verified
        assert cli.main(["huawei", "set", "--port", port, "--voltage", "52.5", "--current",
                         "10", "--apply", "--json",
                         "--session", str(tmp_path / "set3.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["result"] == "applied" and out["verify"]["verified"]
        assert sim.online_v == 52.5 and sim.online_i_raw == round(10 / 52 * 1024)
    finally:
        sim.stop()
        sess.close()


# ======================================================= Phase B additions
# (Harness_Additions_rsh_huawei.md A1-A5). Fake clock: the loops in the
# client and the simulator run on `clk`, reply windows stay real, so a
# "2 s" timeout costs ~0.1 s of wall time.
class FakeClock:
    def __init__(self, speed: float = 20.0, start: float = 1000.0):
        self.now = start
        self.speed = speed

    def __call__(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.now += dt
        time.sleep(dt / self.speed)

    def advance_until(self, t: float, step: float = 0.05) -> None:
        while self.now < t:
            self.sleep(step)


SERIAL_A, SERIAL_B = "0000000000A1", "0000000000B2"


def bound_cfg(serial=SERIAL_A, addr=1, scale="ratio1024", nominal=52.0, current_max=30.0):
    """A config where `info` + `calibrate-current` already ran for one unit."""
    return huawei_config({"huawei": {
        "caps": {"voltage_max": 55.2, "current_max": current_max},
        "units": {serial: {"addr": addr, "nominal_a": nominal, "current_scale": scale}},
        "addr_map": {str(addr): serial}}})


def fake_bench(tmp_path, clk, **sim_kw):
    bus = C.VirtualCanBus(f"fb-{int(time.time() * 1000)}-{id(clk)}")
    sess = SessionWriter(tmp_path / "s.jsonl")
    kw = dict(addr=1, nominal_a=52.0, scale="ratio1024", load_ohm=2.0, timeout_s=2.0,
              unsol_period_s=0.1, clock=clk, tick_s=0.005)
    kw.update(sim_kw)
    sim = HuaweiPsuSimulator(bus.end("psu", I.BITRATE), sess, **kw)
    sim.start()
    return bus, sess, sim


# ---------------------------------------------------------------- A5
def test_persist_zero_current_refused_is_reported_as_fail(tmp_path):
    """B3.0 FAIL criterion: a unit that acks `default_current = 0` with 0x21
    must surface exactly that, not an 'applied'."""
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, reject_zero_default_current=True)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, bound_cfg(),
                          clock=clk, sleep=clk.sleep)
        rep = cl.set_points(None, 0.0, apply=True, persist=True)
        assert rep["result"] == "error"
        s = rep["sent"][0]
        assert s["name"] == "default_current" and s["raw"] == 0 and s["acked"]
        assert s["ok"] is False and s["acks"][1]["status_byte"] == 0x21
        assert sim.default_i_raw != 0 and sim.stats["set_errors"] == 1
        # the ordinary unit accepts it
        sim.reject_zero_default_current = False
        rep = cl.set_points(None, 0.0, apply=True, persist=True)
        assert rep["result"] == "applied" and sim.default_i_raw == 0
        summaries = [r for r in read_session(sess.path) if r.get("kind") == "summary"]
        assert not summaries                     # set writes none itself; the CLI does
    finally:
        sim.stop(); sess.close()


def test_defaults_survive_power_cycle_and_apply_before_any_write(tmp_path):
    """B3.2 / B3.5: after a power cycle the non-volatile defaults (52.4 V,
    0 A) are what the unit runs, with CAN silent, until a live write."""
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, load_ohm=None, battery_v=48.0)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, bound_cfg(),
                          clock=clk, sleep=clk.sleep)
        assert cl.set_points(52.4, 0.0, apply=True, persist=True)["result"] == "applied"
        assert cl.set_points(52.0, 10.0, apply=True, verify=False)["result"] == "applied"
        assert sim.online and sim.measure()[1] == pytest.approx(10.0, abs=0.01)
        sim.power_cycle()
        assert not sim.online and sim.default_v == pytest.approx(52.4, abs=0.001)
        assert sim.default_i_raw == 0
        v, i = sim.measure()
        assert i == 0.0 and v == pytest.approx(48.0, abs=0.01)   # battery voltage, no charge
        snap = cl.status()                         # a status poll does not resume the old limit
        assert snap["out_current_a"] == 0.0 and snap["current_limit_ratio"] == 0.0
        assert cl.set_points(None, 10.0, apply=True)["result"] == "applied"
        assert sim.measure()[1] == pytest.approx(10.0, abs=0.01)
        notes = [r for r in read_session(sess.path) if r.get("kind") == "note"
                 and r.get("serial_hex") == SERIAL_A]
        assert any("power cycle" in r["note"] for r in notes)
    finally:
        sim.stop(); sess.close()


@pytest.mark.parametrize("mode", ["reset", "keep"])
def test_ac_loss_reading_is_selectable(tmp_path, mode):
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, on_ac_loss=mode)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, bound_cfg(),
                          clock=clk, sleep=clk.sleep)
        assert cl.set_points(52.0, None, apply=True, verify=False)["result"] == "applied"
        sim.set_ac(False)
        for _ in range(20):                       # until the sim thread emitted a heartbeat
            clk.sleep(0.2)
            snap = cl.status()
            if cl.last_unsol[1][0]["output_disabled"]:
                break
        assert snap["out_voltage_v"] == 0.0 and "input-power-failure" in snap["status_flags"]
        assert cl.last_unsol[1][0]["output_disabled"]
        sim.set_ac(True)
        assert sim.setpoint_v() == (53.5 if mode == "reset" else 52.0)
        assert sim.stats["ac_losses"] == 1
    finally:
        sim.stop(); sess.close()


# ---------------------------------------------------------------- A1
def test_hold_poll_only_keeps_online_when_polls_refresh(tmp_path):
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, default_current_a=0.0, polls_refresh_timeout=True)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, bound_cfg(),
                          clock=clk, sleep=clk.sleep)
        assert cl.set_points(None, 5.0, apply=True)["result"] == "applied"
        events = []
        cl.on_event = events.append
        stats = cl.hold(None, None, duration=3.5, status_period=0.25, poll_only=True)
        assert stats["poll_only"] and stats["resends"] == 0 and stats["status_polls"] >= 12
        assert stats["first_offline_s"] is None and not stats.get("stop_reason")
        assert sim.online and sim.stats["reverts"] == 0 and sim.stats["sets"] == 1
        assert sim.measure()[1] == pytest.approx(5.0, abs=0.05)     # raw 98/1024*52
        assert not any("OFFLINE-TRANSITION" in e for e in events)
        # poll-only refuses setpoints; the summary record carries the result
        assert cl.hold(52.0, None, duration=0.1, poll_only=True)["refusals"]
        sm = [r for r in read_session(sess.path) if r.get("kind") == "summary"]
        assert sm and sm[0]["summary"]["first_offline_s"] is None and sm[0]["summary"]["poll_only"]
    finally:
        sim.stop(); sess.close()


def test_hold_poll_only_reports_offline_when_only_sets_refresh(tmp_path):
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, default_current_a=0.0, polls_refresh_timeout=False)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, bound_cfg(),
                          clock=clk, sleep=clk.sleep)
        assert cl.set_points(None, 5.0, apply=True)["result"] == "applied"
        t_set = clk.now
        events = []
        cl.on_event = events.append
        stats = cl.hold(None, None, duration=3.5, status_period=0.25, poll_only=True)
        assert stats["first_offline_s"] is not None
        # the sim's 2 s timeout counts from the set frame; the hold started right after it
        t_off = stats["first_offline_s"] + (clk.now - 3.5 - t_set)
        assert 1.9 <= stats["first_offline_s"] <= 2.6, (stats, t_off)
        assert "0x81" in stats["offline_evidence"]
        assert sim.stats["reverts"] == 1 and not sim.online
        assert sum("OFFLINE-TRANSITION" in e for e in events) == 1
        assert any("CHANGE out_current_a" in e for e in events)
        notes = [r for r in read_session(sess.path) if r.get("first_offline_s") is not None]
        assert notes and notes[0]["first_offline_s"] == stats["first_offline_s"]
    finally:
        sim.stop(); sess.close()


# ---------------------------------------------------------------- A2
def test_silence_after_measures_fallback_time(tmp_path):
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, default_current_a=0.0)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, bound_cfg(),
                          clock=clk, sleep=clk.sleep)
        events = []
        cl.on_event = events.append
        stats = cl.hold(52.0, 5.0, duration=None, status_period=0.25, resend_period=1.0,
                        silence_after=1.0, observe_s=4.0)
        assert stats["resends"] >= 1 and stats["status_polls"] >= 3
        assert stats["reminder"].startswith("reminder")
        sl = stats["silence"]
        assert sl["measured_on"] == "can" and sl["load_raw_before_silence"] > 0
        # fallback 2 s after the last frame addressed to the unit (+ tick granularity)
        assert sl["t_zero_current"] is not None and 1.9 <= sl["t_zero_current"] <= 2.6, sl
        assert sl["residual_current_a"] == 0.0 and sl["residual_load_raw"] == 0
        assert sl["t_first_output_disabled"] is None     # the sim never claims that byte - null, not guessed
        assert sl["unsolicited_period_s"] is not None and 0.05 <= sl["unsolicited_period_s"] <= 0.3
        assert sl["frames"] >= 20 and sl["other_controller_frames"] == 0
        assert sim.stats["reverts"] == 1
        recs = [r for r in read_session(sess.path) if r.get("mode") == "silence" and r.get("kind") == "can"]
        assert recs and all(r["dir"] == "rx" and "t_since_last_tx" in r for r in recs)
        assert all(r["chan"] == "can" for r in recs)
        tx_after = [r for r in read_session(sess.path) if r.get("dir") == "tx"
                    and r["chan"] == "can" and r["t"] > recs[0]["t"]]
        assert not tx_after                                # nothing transmitted during silence
        sm = [r for r in read_session(sess.path) if r.get("kind") == "summary"]
        assert sm[-1]["summary"]["silence"]["t_zero_current"] == sl["t_zero_current"]
        assert any(e.startswith("SILENCE result") for e in events)
    finally:
        sim.stop(); sess.close()


def test_silence_after_uses_tap_channel_timestamps(tmp_path):
    import threading
    clk = FakeClock()
    bus, sess, sim = fake_bench(tmp_path, clk, default_current_a=0.0, timeout_s=10.0)
    try:
        ctl = bus.end("ctl", I.BITRATE)
        tap = bus.end("tap", I.BITRATE, listen_only=True)
        cl = HuaweiClient(ctl, sess, "can", 1, bound_cfg(), clock=clk, sleep=clk.sleep)
        t0 = clk.now

        def ac_off():                     # AC drops 1.5 s into the silence
            clk_target = t0 + 1.0 + 1.5
            while clk.now < clk_target:
                time.sleep(0.002)
            sim.set_ac(False)
        th = threading.Thread(target=ac_off, daemon=True)
        th.start()
        stats = cl.hold(52.0, 5.0, duration=None, status_period=0.25, resend_period=1.0,
                        silence_after=1.0, observe_s=3.0, tap_link=tap, tap_chan="tap")
        th.join(timeout=2)
        sl = stats["silence"]
        assert sl["measured_on"] == "tap"
        assert sl["t_first_output_disabled"] is not None and 1.2 <= sl["t_first_output_disabled"] <= 2.2, sl
        assert sl["t_zero_current"] is not None and sl["t_zero_current"] >= sl["t_first_output_disabled"] - 0.2
        assert sl["residual_current_a"] == 0.0
        recs = list(read_session(sess.path))
        sil = [r for r in recs if r.get("mode") == "silence" and r.get("kind") == "can"]
        assert sil and {r["chan"] for r in sil} == {"tap"}
        # the controller link was drained into the same session but not measured
        assert any(r["chan"] == "can" and r["dir"] == "rx" and r["t"] >= sil[0]["t"] for r in recs)
        assert tap.listen_only
    finally:
        sim.stop(); sess.close()


# ---------------------------------------------------------------- A3
def two_units(tmp_path, clk):
    bus = C.VirtualCanBus(f"two-{int(time.time() * 1000)}")
    sess = SessionWriter(tmp_path / "two.jsonl")
    kw = dict(nominal_a=52.0, scale="ratio1024", load_ohm=2.0, timeout_s=5.0,
              unsol_period_s=0.2, clock=clk, tick_s=0.005)
    s1 = HuaweiPsuSimulator(bus.end("psu1", I.BITRATE), sess, addr=1, serial_hex=SERIAL_A,
                            chan="psu1", **kw)
    s2 = HuaweiPsuSimulator(bus.end("psu2", I.BITRATE), sess, addr=2, serial_hex=SERIAL_B,
                            chan="psu2", **kw)
    s1.start(); s2.start()
    return bus, sess, s1, s2


def test_broadcast_set_waits_for_every_expected_ack(tmp_path):
    clk = FakeClock()
    bus, sess, s1, s2 = two_units(tmp_path, clk)
    try:
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, huawei_config({}),
                          target_addr=0, expect_acks=[1, 2], clock=clk, sleep=clk.sleep)
        assert cl.broadcast and cl.target_label() == "broadcast"
        rep = cl.set_points(52.0, None, apply=True)
        assert rep["result"] == "applied" and "verify" not in rep, rep
        s = rep["sent"][0]
        assert s["id"] == "0x108080FE" and s["target"] == "broadcast"
        assert sorted(s["acks"]) == [1, 2] and s["missing_acks"] == []
        assert s1.online_v == 52.0 and s2.online_v == 52.0
        tx = [r for r in read_session(sess.path) if r.get("dir") == "tx" and r["chan"] == "can"]
        assert tx[-1]["target"] == "broadcast" and tx[-1]["expect_acks"] == [1, 2]
        # a unit that does not answer is an error, like a rejected setpoint
        cl3 = HuaweiClient(bus.end("ctl3", I.BITRATE), sess, "can", 1, huawei_config({}),
                           target_addr=0, expect_acks=[1, 2, 3], clock=clk, sleep=clk.sleep)
        rep = cl3.set_points(51.0, None, apply=True)
        assert rep["result"] == "error" and rep["sent"][0]["missing_acks"] == [3]
        assert rep["sent"][0]["ok"] is False and "addr [3]" in rep["sent"][0]["error"]
        assert s1.online_v == 51.0                    # the frame itself did go out
        # broadcast current: both units must be bound and share (scale, nominal)
        hc = bound_cfg()
        from rs485harness.analyzer.huawei import bind_unit
        bind_unit(hc, SERIAL_B, 2, 35.0, "ratio1024")
        clb = HuaweiClient(bus.end("ctlb", I.BITRATE), sess, "can", 1, hc,
                           target_addr=0, expect_acks=[1, 2], clock=clk, sleep=clk.sleep)
        rep = clb.set_points(None, 5.0, apply=True)
        assert rep["result"] == "refused" and "do not share" in rep["refusals"][0]
        bind_unit(hc, SERIAL_B, 2, 52.0, "ratio1024")
        rep = clb.set_points(None, 5.0, apply=True)
        assert rep["result"] == "applied" and s1.online_i_raw == s2.online_i_raw == round(5 / 52 * 1024)
    finally:
        s1.stop(); s2.stop(); sess.close()


def test_per_unit_set_reaches_only_its_addr(tmp_path):
    clk = FakeClock()
    bus, sess, s1, s2 = two_units(tmp_path, clk)
    try:
        cl2 = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 2, huawei_config({}),
                           clock=clk, sleep=clk.sleep)
        rep = cl2.set_points(51.0, None, apply=True)
        assert rep["result"] == "applied" and rep["target"] == "addr:2"
        assert rep["sent"][0]["id"] == "0x108280FE" and list(rep["sent"][0]["acks"]) == [2]
        assert s2.online_v == 51.0 and s1.online_v == 53.5 and not s1.online
        assert rep["verify"]["verified"] and rep["verify"]["measured_v"] == 51.0
        assert cl2.status()["out_voltage_v"] == 51.0
        info = cl2.info()
        assert info["serial_hex"] == SERIAL_B and info["bound"] == {"serial_hex": SERIAL_B, "addr": 2}
        assert cl2.hcfg["addr_map"] == {"2": SERIAL_B}
        tx = [r for r in read_session(sess.path) if r.get("dir") == "tx" and r["chan"] == "can"]
        assert tx and all(r["target"] == "addr:2" for r in tx)
    finally:
        s1.stop(); s2.stop(); sess.close()


def test_scale_is_bound_to_serial_not_addr(tmp_path):
    clk = FakeClock()
    bus, sess, s1, s2 = two_units(tmp_path, clk)
    try:
        hc = bound_cfg()                               # serial A calibrated, on addr 1
        cl = HuaweiClient(bus.end("ctl", I.BITRATE), sess, "can", 1, hc, clock=clk, sleep=clk.sleep)
        assert cl.set_points(None, 5.0, apply=True)["result"] == "applied"
        assert s1.online_i_raw == round(5 / 52 * 1024)
        # the units swap addresses (renegotiation): B now answers on 1
        s1.set_addr(3); s2.set_addr(1); s1.set_addr(2)
        rep = cl.set_points(None, 5.0, apply=True)
        assert rep["result"] == "refused" and "serial changed" in rep["refusals"][0], rep
        assert f"{SERIAL_A} -> {SERIAL_B}" in rep["refusals"][0]
        assert s2.online_i_raw == s2.default_i_raw        # nothing was sent
        # after info: addr 1 = serial B, which has no scale -> still refused, different reason
        info = cl.info()
        assert info["addr_change"] == {"addr": 1, "was": SERIAL_A, "now": SERIAL_B}
        assert hc["addr_map"] == {"1": SERIAL_B} and hc["units"][SERIAL_A]["current_scale"] == "ratio1024"
        rep = cl.set_points(None, 5.0, apply=True)
        assert rep["result"] == "refused" and "not established" in rep["refusals"][0]
        # serial A, now on addr 2, keeps its scale: a client for addr 2 can use it after info
        cl2 = HuaweiClient(bus.end("ctl2", I.BITRATE), sess, "can", 2, hc, clock=clk, sleep=clk.sleep)
        assert cl2.info()["serial_hex"] == SERIAL_A and hc["addr_map"] == {"1": SERIAL_B, "2": SERIAL_A}
        assert cl2.set_points(None, 5.0, apply=True)["result"] == "applied"
        assert s1.online_i_raw == round(5 / 52 * 1024)
    finally:
        s1.stop(); s2.stop(); sess.close()


def test_config_migration_of_flat_scale():
    hc = huawei_config({"huawei": {"current_scale": "x20", "nominal_a": 52.0,
                                   "caps": {"current_max": 10}}})
    assert hc["legacy"] == {"current_scale": "x20", "nominal_a": 52.0} and hc["_migrated"]
    assert "current_scale" not in hc and hc["units"] == {} and hc["addr_map"] == {}
    assert "_migrated" not in huawei_config({"huawei": {k: v for k, v in hc.items()
                                                        if not k.startswith("_")}})
