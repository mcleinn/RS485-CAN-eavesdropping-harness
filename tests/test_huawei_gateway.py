"""Huawei gateway: harness between a charger's add-on control board (front-end
simulator) and the rectifier (PSU simulator), two virtual buses, fake clock."""
import json
import threading
import time

import pytest

from rs485harness.core import canbus as C
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.protocols.huawei_r48xx import ids as I
from rs485harness.simulators.huawei_psu_sim import HuaweiPsuSimulator
from rs485harness.simulators.huawei_frontend_sim import HuaweiFrontEndSimulator
from rs485harness.analyzer.huawei import huawei_config
from rs485harness.analyzer.huawei_gateway import HuaweiGateway

SERIAL = "0000000000A1"


class FakeClock:
    def __init__(self, speed=20.0):
        self.now, self.speed = 1000.0, speed

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt
        time.sleep(dt / self.speed)


def bound_cfg(current_max=30.0):
    return huawei_config({"huawei": {
        "caps": {"voltage_max": 55.2, "current_max": current_max},
        "units": {SERIAL: {"addr": 1, "nominal_a": 52.0, "current_scale": "ratio1024"}},
        "addr_map": {"1": SERIAL}}})


@pytest.fixture
def rig(tmp_path):
    clk = FakeClock()
    tag = int(time.time() * 1000)
    bus_psu = C.VirtualCanBus(f"gw-psu-{tag}")
    bus_fe = C.VirtualCanBus(f"gw-fe-{tag}")
    sess = SessionWriter(tmp_path / "sims.jsonl")
    psu = HuaweiPsuSimulator(bus_psu.end("psu", I.BITRATE), sess, addr=1, nominal_a=52.0,
                             scale="ratio1024", load_ohm=10.0, timeout_s=2.0,
                             unsol_period_s=0.377, clock=clk, tick_s=0.005)
    fe = HuaweiFrontEndSimulator(bus_fe.end("board", I.BITRATE), sess, addr=1, period_s=0.62,
                                 cfg_period_s=3.0, clock=clk, tick_s=0.005)
    psu.start(); fe.start()
    yield clk, bus_psu, bus_fe, sess, psu, fe
    fe.stop(); psu.stop(); sess.close()


def run_gateway(gw, duration):
    t = threading.Thread(target=gw.run, kwargs={"duration": duration}, daemon=True)
    t.start(); t.join(timeout=30)
    assert not t.is_alive()


def test_gateway_is_sole_master_and_keeps_the_front_end_content(rig, tmp_path):
    clk, bus_psu, bus_fe, sess, psu, fe = rig
    cap = SessionWriter(tmp_path / "gw.jsonl")
    events = []
    gw = HuaweiGateway(bus_psu.end("gw-psu", I.BITRATE), bus_fe.end("gw-fe", I.BITRATE), cap,
                       bound_cfg(), addr=1, voltage=52.0, current=10.0, resend_period=2.0,
                       status_period=1.0, on_event=events.append, clock=clk, sleep=clk.sleep)
    assert gw.prepare() == []
    run_gateway(gw, 8.0)
    cap.close()
    st = gw.stats
    # module side: only OUR setpoints, every write acked, nothing from the board
    assert psu.online and psu.online_v == 52.0 and psu.online_i_raw == round(10 / 52 * 1024)
    assert psu.default_v == 53.5 and psu.default_i_raw != 210          # board defaults never arrived
    assert psu.stats["sets"] == st["our_sends"] >= 6 and st["our_acks_ok"] == st["our_sends"]
    assert st["our_acks_error"] == 0 and st["our_acks_missing"] == 0 and st["psu_undelivered"] == 0
    # front-end side: its requests answered with real data, its writes acked but blocked
    assert fe.stats["cycles"] >= 10 and fe.stats["bursts_complete"] >= 8
    assert fe.stats["acks_ok"] == fe.stats["writes"] and fe.stats["acks_missing"] == 0
    assert fe.last_status["out_voltage_v"] == 52.0                      # the screen shows OUR voltage
    assert fe.last_status["out_current_a"] == pytest.approx(5.2, abs=0.05)
    assert fe.stats["unsolicited"] >= 10                                  # heartbeats forwarded
    assert st["fe_writes_blocked"] == fe.stats["writes"] >= 60
    assert st["acks_synthesized"] == st["fe_writes_blocked"]
    assert st["fe_cfg_blocked"] >= 2
    assert fe.stats["requests"] - 1 <= st["fe_requests_forwarded"] <= fe.stats["requests"]
    assert st["fe_setpoints"]["online_voltage"]["value_v"] == pytest.approx(48.0, abs=0.05)
    assert st["fe_setpoints"]["online_current"]["raw"] == 70
    assert st["fe_setpoints"]["default_current"]["raw"] == 210
    # the board polls, so the gateway did not add its own status polls
    assert st["our_status_polls"] <= 1
    # audit trail
    recs = [r for r in read_session(tmp_path / "gw.jsonl") if r.get("kind") == "can"]
    blocked = [r for r in recs if r.get("blocked")]
    assert blocked and all(r["chan"] == "fe" and r["dir"] == "rx" for r in blocked)
    synth = [r for r in recs if r.get("synthesized")]
    assert synth and all(r["chan"] == "fe" and r["dir"] == "tx" and r["acked"] for r in synth)
    ours = [r for r in recs if r["chan"] == "psu" and r["dir"] == "tx" and r.get("policy") == "gateway"
            and r["decoded"]["kind"] == "set"]
    assert ours and all(r["acked"] and "cap" in r and r["target"] == "addr:1" for r in ours)
    assert not any(r["chan"] == "psu" and r["dir"] == "tx" and r["decoded"].get("name") == "default_voltage"
                   for r in recs)
    fwd_status = [r for r in recs if r["chan"] == "fe" and r["dir"] == "tx" and r.get("forwarded")]
    assert fwd_status and any(r["decoded"]["kind"] == "status" for r in fwd_status)
    # module acks are consumed, never forwarded to the board
    assert not any(r["chan"] == "fe" and r["dir"] == "tx" and r["decoded"]["kind"] == "ack"
                   and not r.get("synthesized") for r in recs)
    summ = [r for r in read_session(tmp_path / "gw.jsonl") if r.get("kind") == "summary"]
    assert summ and summ[-1]["summary"]["sub"] == "gateway" and summ[-1]["summary"]["our_sends"] >= 6
    # the PSU session never saw a frame from the board's address space except via us
    psu_rx = [r for r in read_session(tmp_path / "sims.jsonl") if r.get("chan") == "psu" and r.get("dir") == "rx"]
    assert not any(r["id"] == "0x108081FE" for r in psu_rx)             # cfg broadcast blocked


def test_gateway_refuses_what_set_refuses_and_sends_nothing(rig, tmp_path):
    clk, bus_psu, bus_fe, sess, psu, fe = rig
    cap = SessionWriter(tmp_path / "gw.jsonl")
    gw = HuaweiGateway(bus_psu.end("gw-psu", I.BITRATE), bus_fe.end("gw-fe", I.BITRATE), cap,
                       bound_cfg(current_max=5.0), voltage=52.0, current=10.0, clock=clk, sleep=clk.sleep)
    ref = gw.prepare()
    assert ref and "exceeds cap" in ref[0]
    st = gw.run(duration=0.5)
    assert st["refusals"] == ref and st["our_sends"] == 0 and psu.stats["sets"] == 0
    cap.close()
    # listen-only links are refused up front
    with pytest.raises(C.CanError):
        HuaweiGateway(bus_psu.end("x", I.BITRATE, listen_only=True), bus_fe.end("y", I.BITRATE), cap,
                      bound_cfg())


def test_gateway_polls_itself_when_the_front_end_is_quiet(tmp_path):
    clk = FakeClock()
    tag = int(time.time() * 1000)
    bus_psu = C.VirtualCanBus(f"gwq-psu-{tag}")
    bus_fe = C.VirtualCanBus(f"gwq-fe-{tag}")
    sess = SessionWriter(tmp_path / "s.jsonl")
    psu = HuaweiPsuSimulator(bus_psu.end("psu", I.BITRATE), sess, load_ohm=10.0, timeout_s=2.0,
                             unsol_period_s=0.377, clock=clk, tick_s=0.005)
    psu.start()
    bus_fe.end("silent-board", I.BITRATE)                  # ACKs, never talks
    events = []
    gw = HuaweiGateway(bus_psu.end("gw-psu", I.BITRATE), bus_fe.end("gw-fe", I.BITRATE), sess,
                       bound_cfg(), voltage=52.0, resend_period=5.0, status_period=1.0,
                       on_event=events.append, clock=clk, sleep=clk.sleep)
    try:
        assert gw.prepare() == []
        run_gateway(gw, 6.0)
    finally:
        psu.stop(); sess.close()
    assert gw.stats["our_status_polls"] >= 4 and psu.stats["reverts"] == 0 and psu.online
    assert any("CHANGE out_voltage_v" in e for e in events)        # 53.5 -> 52.0 seen


def test_rsh_huawei_gateway_and_frontend_sim_cli(tmp_path, monkeypatch, capsys):
    from rs485harness import runner, cli
    monkeypatch.setattr(runner, "CONFIG_PATH", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({"huawei": bound_cfg()}))
    tag = int(time.time() * 1000)
    a, b = f"gwcli-psu-{tag}", f"gwcli-fe-{tag}"
    sess = SessionWriter(tmp_path / "sims.jsonl")
    psu = HuaweiPsuSimulator(C.VirtualCanBus.get(a).end("psu", I.BITRATE), sess, load_ohm=10.0,
                             unsol_period_s=0.377)
    fe = HuaweiFrontEndSimulator(C.VirtualCanBus.get(b).end("board", I.BITRATE), sess, period_s=0.62)
    psu.start(); fe.start()
    try:
        rc = cli.main(["huawei", "gateway", "--port", f"can:virtual:{a}", "--front-end",
                       f"can:virtual:{b}", "--voltage", "52", "--current", "10", "--duration", "2.5",
                       "--json", "--label", "gw-test", "--session", str(tmp_path / "gw.jsonl")])
    finally:
        fe.stop(); psu.stop(); sess.close()
    out = capsys.readouterr().out
    rep = json.loads(out[out.index("{"):])
    assert rc == 0, rep
    assert rep["stats"]["fe_writes_blocked"] >= 12 and rep["stats"]["our_acks_ok"] >= 2
    assert psu.online_v == 52.0
    assert fe.stats["acks_missing"] <= 6                 # at most the cycle after the gateway ended
    assert rep["label"] == "gw-test"
    # the front-end simulator has its own CLI entry for bench use
    rc = cli.main(["huawei", "frontend-sim", "--port", f"can:virtual:gwcli-x-{tag}", "--duration",
                   "0.3", "--json", "--session", str(tmp_path / "fe.jsonl")])
    assert rc == 0


def test_gateway_rewrites_the_address_for_a_board_built_for_address_1(tmp_path):
    """Module 2 on the module bus, a board that only knows address 1: the
    gateway relabels identifiers both ways, the board sees module 2's data
    as 'address 1', our setpoints go to address 2."""
    clk = FakeClock()
    tag = int(time.time() * 1000)
    bus_psu = C.VirtualCanBus(f"gwa-psu-{tag}")
    bus_fe = C.VirtualCanBus(f"gwa-fe-{tag}")
    sess = SessionWriter(tmp_path / "s.jsonl")
    psu2 = HuaweiPsuSimulator(bus_psu.end("psu2", I.BITRATE), sess, addr=2, load_ohm=10.0,
                              timeout_s=2.0, unsol_period_s=0.377, clock=clk, tick_s=0.005,
                              serial_hex="0000000000B2")
    fe = HuaweiFrontEndSimulator(bus_fe.end("board", I.BITRATE), sess, addr=1, period_s=0.62,
                                 clock=clk, tick_s=0.005)
    psu2.start(); fe.start()
    hc = huawei_config({"huawei": {"caps": {"voltage_max": 55.2, "current_max": 30},
                                   "units": {"0000000000B2": {"addr": 2, "nominal_a": 52.0,
                                                              "current_scale": "ratio1024"}},
                                   "addr_map": {"2": "0000000000B2"}}})
    cap = SessionWriter(tmp_path / "gw.jsonl")
    gw = HuaweiGateway(bus_psu.end("gw-psu", I.BITRATE), bus_fe.end("gw-fe", I.BITRATE), cap, hc,
                       addr=2, fe_addr=1, voltage=52.0, current=10.0, resend_period=2.0,
                       clock=clk, sleep=clk.sleep)
    try:
        assert gw.prepare() == []
        run_gateway(gw, 5.0)
    finally:
        fe.stop(); psu2.stop(); sess.close(); cap.close()
    assert psu2.online_v == 52.0 and psu2.online_i_raw == round(10 / 52 * 1024)
    assert fe.stats["bursts_complete"] >= 4 and fe.last_status["out_voltage_v"] == 52.0
    assert fe.stats["acks_ok"] == fe.stats["writes"] and fe.stats["acks_missing"] == 0
    assert gw.stats["addr_rewritten"] > 0
    recs = [r for r in read_session(tmp_path / "gw.jsonl") if r.get("kind") == "can"]
    # board side only ever sees address-1 identifiers, module side only address 2
    assert all(r["decoded"]["addr"] in (0, 1) for r in recs if r["chan"] == "fe" and r.get("decoded"))
    assert all(r["decoded"]["addr"] in (0, 2) for r in recs if r["chan"] == "psu" and r.get("decoded"))


def test_gateway_blocks_commands_disguised_as_requests(tmp_path):
    """Measured 2026-10-25 on the real board: it sent its standby command
    under the INFO command id. Classifying by command id alone forwarded it
    and the module went to standby. A request is an all-zero payload."""
    from rs485harness.analyzer.huawei_gateway import request_payload_ok
    assert request_payload_ok("info_request", bytes(8))
    assert not request_payload_ok("info_request", bytes.fromhex("0132000100000000"))
    assert request_payload_ok("get", bytes.fromhex("0175000000000000"))
    assert not request_payload_ok("get", bytes.fromhex("0132000100000000"))
    clk = FakeClock()
    tag = int(time.time() * 1000)
    bus_psu = C.VirtualCanBus(f"gwd-psu-{tag}")
    bus_fe = C.VirtualCanBus(f"gwd-fe-{tag}")
    sess = SessionWriter(tmp_path / "s.jsonl")
    psu = HuaweiPsuSimulator(bus_psu.end("psu", I.BITRATE), sess, load_ohm=10.0, timeout_s=5.0,
                             unsol_period_s=0.377, clock=clk, tick_s=0.005)
    fe = HuaweiFrontEndSimulator(bus_fe.end("board", I.BITRATE), sess, period_s=0.62, standby=1,
                                 clock=clk, tick_s=0.005, disguised_standby=True)
    cap = SessionWriter(tmp_path / "gw.jsonl")
    events = []
    gw = HuaweiGateway(bus_psu.end("gw-psu", I.BITRATE), bus_fe.end("gw-fe", I.BITRATE), cap,
                       bound_cfg(), voltage=52.0, resend_period=2.0, on_event=events.append,
                       clock=clk, sleep=clk.sleep)
    psu.start()
    try:
        assert gw.prepare() == []
        fe.start()                               # the gateway's bus end exists: its first frame is seen
        run_gateway(gw, 3.0)
    finally:
        fe.stop(); psu.stop(); sess.close(); cap.close()
    assert not psu.standby                                   # the disguised command never arrived
    assert gw.stats["fe_disguised_blocked"] == 1
    assert any("under the info_request id" in e for e in events)
    recs = [r for r in read_session(tmp_path / "gw.jsonl") if r.get("kind") == "can"]
    sent_to_psu = [r for r in recs if r["chan"] == "psu" and r["dir"] == "tx"]
    assert all(r["hex"] == "00" * 8 or r["decoded"]["kind"] == "set" and r.get("policy") == "gateway"
               for r in sent_to_psu), [r["hex"] for r in sent_to_psu]
    blocked = [r for r in recs if r.get("blocked") and "non-zero payload" in (r.get("note") or "")]
    assert len(blocked) == 1 and blocked[0]["hex"] == "0132000100000000"
    # the module's own 0x81 announcement is relayed to the board
    psu_rx = [r for r in read_session(tmp_path / "s.jsonl") if r.get("chan") == "psu"]
    assert psu.online_v == 52.0


def test_gateway_display_voltage_from_0x85(tmp_path):
    """R4875G5: 0x75 = 0 in the burst, the voltage is in 0x85. With the option
    the board's screen gets the fresh 0x85 value in the 0x75 frame, logged as a
    modification; without it the zero goes through."""
    for enabled in (True, False):
        clk = FakeClock()
        tag = int(time.time() * 1000)
        bus_psu = C.VirtualCanBus(f"gwv-psu-{tag}-{enabled}")
        bus_fe = C.VirtualCanBus(f"gwv-fe-{tag}-{enabled}")
        sess = SessionWriter(tmp_path / f"s{enabled}.jsonl")
        psu = HuaweiPsuSimulator(bus_psu.end("psu", I.BITRATE), sess, load_ohm=10.0, timeout_s=5.0,
                                 unsol_period_s=0.377, clock=clk, tick_s=0.005, voltage_in_0x85=True)
        fe = HuaweiFrontEndSimulator(bus_fe.end("board", I.BITRATE), sess, period_s=0.62,
                                     clock=clk, tick_s=0.005)
        cap = SessionWriter(tmp_path / f"gw{enabled}.jsonl")
        gw = HuaweiGateway(bus_psu.end("gw-psu", I.BITRATE), bus_fe.end("gw-fe", I.BITRATE), cap,
                           bound_cfg(), voltage=52.0, resend_period=2.0, clock=clk, sleep=clk.sleep,
                           display_voltage_0x85=enabled)
        psu.start()
        try:
            assert gw.prepare() == []
            fe.start()
            run_gateway(gw, 4.0)
        finally:
            fe.stop(); psu.stop(); sess.close(); cap.close()
        if enabled:
            assert fe.last_status["out_voltage_v"] == 52.0, fe.last_status
            assert gw.stats["display_voltage_substituted"] >= 3 and gw.stats["get85_sent"] >= 3
            recs = [r for r in read_session(tmp_path / "gwTrue.jsonl") if r.get("kind") == "can"]
            mod = [r for r in recs if r.get("modified")]
            orig = [r for r in recs if (r.get("note") or "").startswith("ORIGINAL 0x75")]
            assert mod and len(mod) == len(orig) and mod[0]["modified"]["from"] == "0x85"
            assert mod[0]["decoded"]["value"] == 52.0 and orig[0]["decoded"]["value"] == 0.0
        else:
            assert fe.last_status["out_voltage_v"] == 0.0
            assert gw.stats["display_voltage_substituted"] == 0

