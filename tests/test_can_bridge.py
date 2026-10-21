"""CAN gateway (two segments, policy on 0x351) and CAN selftest over the
virtual bus."""
import time
from dataclasses import replace

import pytest

from rs485harness.core import canbus as C
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.protocols.pylontech_can import frames as PF
from rs485harness.protocols.pylontech_can.dialects import CAN_BATTERY_PROFILES, CAN_INVERTER_PROFILES
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.can_battery_sim import CanBatterySimulator, CanInverterSimulator
from rs485harness.analyzer.can_bridge import CanBridge, CanPolicyEngine, run_can_selftest
from rs485harness.analyzer.policy import make_policy, CapPolicy, PayloadPolicy


def test_can_policy_engine_invariants():
    eng = CanPolicyEngine(CapPolicy(ccl_max=40, cvl_max=55.0), loss_timeout_s=1.0)
    m = PF.encode_limits(56.0, 100.0, 80.0, 48.0)
    out, audit = eng.process(m, PF.decode(m), now=10.0)
    d = PF.decode(C.CanMsg(0.0, 0x351, out))
    assert (d["cvl_v"], d["ccl_a"], d["dcl_a"], d["dvl_v"]) == (55.0, 40.0, 80.0, 48.0)
    assert audit["modified"] == {"cvl": 55.0, "ccl": 40.0}
    # below the cap: untouched
    m2 = PF.encode_limits(54.0, 30.0, 80.0, 48.0)
    out, audit = eng.process(m2, PF.decode(m2), now=10.5)
    assert out is None and audit["modified"] == {}
    # a policy proposing an increase or a locked field is clamped / ignored

    class Bad(PayloadPolicy):
        name = "bad"

        def apply(self, ctx):
            return {"ccl": 500, "dcl": 1, "cvl": 60}
    eng2 = CanPolicyEngine(Bad())
    out, audit = eng2.process(m2, PF.decode(m2), now=1.0)
    assert out is None and any("clamped ccl" in n for n in audit["notes"])
    assert any("ignored 'dcl'" in n for n in audit["notes"]) and eng2.stats["clamped"] == 2
    # other ids are skipped; comm loss resets
    out, audit = eng.process(PF.encode_soc(50, 100), PF.decode(PF.encode_soc(50, 100)), now=11.0)
    assert out is None and "skipped" in audit
    out, audit = eng.process(m, PF.decode(m), now=20.0)
    assert "reset" in audit and eng.stats["resets"] == 1


@pytest.fixture
def two_segments(tmp_path):
    tag = int(time.time() * 1000)
    seg_bat = C.VirtualCanBus(f"seg-bat-{tag}")
    seg_inv = C.VirtualCanBus(f"seg-inv-{tag}")
    sess = SessionWriter(tmp_path / "gw.jsonl")
    prof = replace(CAN_BATTERY_PROFILES["pylontech-lv"], period_s=0.1, extension_period_s=0.2)
    state = preset("ideal")
    bat = CanBatterySimulator(seg_bat.end("bat", 500_000), state, prof, sess, chan="bat")
    inv = CanInverterSimulator(seg_inv.end("inv", 500_000),
                               replace(CAN_INVERTER_PROFILES["pylon-unlock"], period_s=0.1), sess,
                               chan="inv")
    bat.start(); inv.start()
    yield seg_bat, seg_inv, sess, bat, inv, state
    bat.stop(); inv.stop(); sess.close()


def test_gateway_transparent_relays_both_ways_and_unlocks(two_segments, tmp_path):
    seg_bat, seg_inv, sess, bat, inv, state = two_segments
    cap = SessionWriter(tmp_path / "bridge.jsonl")
    br = CanBridge(seg_inv.end("gw-inv", 500_000), seg_bat.end("gw-bat", 500_000), cap,
                   chan_a="ch1", chan_b="ch2", role_a="inverter", role_b="battery", markers=False)
    import threading
    t = threading.Thread(target=br.run, kwargs={"duration": 1.0}, daemon=True)
    t.start(); t.join(timeout=4)
    cap.close()
    assert br.mode == "transparent"
    assert inv.snapshot.get("cvl_v") == 56.0 and inv.snapshot.get("soc_percent") == 75
    assert bat.unlocked and bat.stats["keepalives"] >= 3            # 0x305/0x307 crossed over
    assert "0x373" in inv.ids_seen                                   # extension frames crossed back
    recs = [r for r in read_session(tmp_path / "bridge.jsonl") if r.get("kind") == "can"]
    assert recs and all(r["dir"] == "fwd" for r in recs)
    assert {r["flow"] for r in recs} == {"BMS->INV", "INV->BMS"}
    assert br.stats["ch2"]["rewritten"] == 0 and br.stats["ch2"]["undelivered"] == 0


def test_gateway_policy_caps_351_downwards_only(two_segments, tmp_path):
    seg_bat, seg_inv, sess, bat, inv, state = two_segments
    cap = SessionWriter(tmp_path / "bridge.jsonl")
    eng = CanPolicyEngine(make_policy("cap:ccl_max=40,cvl_max=55.2"), loss_timeout_s=0.5)
    events = []
    br = CanBridge(seg_inv.end("gw-inv", 500_000), seg_bat.end("gw-bat", 500_000), cap,
                   engine=eng, role_a="inverter", role_b="battery", markers=False, quiet=True,
                   on_event=events.append)
    import threading
    t = threading.Thread(target=br.run, kwargs={"duration": 1.0}, daemon=True)
    t.start()
    time.sleep(0.5)
    state.charge_current_limit_da = 300          # BMS lowers to 30 A: passes through unchanged
    t.join(timeout=4)
    cap.close()
    assert inv.snapshot["ccl_a"] == 30.0 and inv.snapshot["cvl_v"] == 55.2
    assert inv.snapshot["dcl_a"] == 100.0        # locked field untouched
    assert inv.snapshot["soc_percent"] == 75     # other ids untouched
    recs = [r for r in read_session(tmp_path / "bridge.jsonl") if r.get("kind") == "can"]
    orig = [r for r in recs if r.get("note", "").startswith("ORIGINAL")]
    mod = [r for r in recs if r.get("modified")]
    assert orig and mod and len(orig) == len(mod)
    assert orig[0]["hex"] == PF.encode_limits(56.0, 100.0, 100.0, 46.0).data.hex().upper()
    assert mod[0]["decoded"]["ccl_a"] == 40.0 and mod[0]["decoded"]["cvl_v"] == 55.2
    assert mod[0]["acked"] is True and orig[0]["audit"]["modified"] == {"cvl": 55.2, "ccl": 40.0}
    # after the BMS went to 30 A the frame is below the cap: forwarded, cvl still capped
    last = [r for r in recs if r["id"] == "0x351"][-1]
    assert last["decoded"]["ccl_a"] == 30.0
    assert eng.stats["modified"] >= 1 and any("MODIFIED" in e for e in events)
    assert not any(r["id"] in ("0x359", "0x35C") and r.get("modified") for r in recs)


def test_gateway_reports_undelivered_when_far_segment_empty(tmp_path):
    tag = int(time.time() * 1000)
    seg_bat = C.VirtualCanBus(f"e-bat-{tag}")
    seg_inv = C.VirtualCanBus(f"e-inv-{tag}")
    sess = SessionWriter(tmp_path / "s.jsonl")
    prof = replace(CAN_BATTERY_PROFILES["pylontech-lv"], period_s=0.1)
    bat = CanBatterySimulator(seg_bat.end("bat", 500_000), preset("ideal"), prof, sess, chan="bat")
    bat.start()
    events = []
    br = CanBridge(seg_inv.end("gw-inv", 500_000), seg_bat.end("gw-bat", 500_000), sess,
                   role_a="inverter", role_b="battery", markers=False, quiet=True,
                   on_event=events.append)
    import threading
    t = threading.Thread(target=br.run, kwargs={"duration": 0.6}, daemon=True)
    t.start(); t.join(timeout=3)
    bat.stop(); sess.close()
    assert br.stats["ch2"]["undelivered"] >= 3
    assert any("not acknowledged" in e for e in events)
    recs = [r for r in read_session(tmp_path / "s.jsonl") if r.get("chan") == "ch2"]
    assert recs and recs[0]["acked"] is False


def test_gateway_refuses_listen_only_links(tmp_path):
    bus = C.VirtualCanBus("lo")
    sess = SessionWriter(tmp_path / "s.jsonl")
    with pytest.raises(C.CanError):
        CanBridge(bus.end("a", 500_000, listen_only=True), bus.end("b", 500_000), sess)
    sess.close()


def test_can_selftest_passes_on_one_bus_and_fails_when_split(tmp_path):
    bus = C.VirtualCanBus(f"st-{int(time.time() * 1000)}")
    l1, l2 = bus.end("ch0", 500_000), bus.end("ch1", 500_000)
    sess = SessionWriter(tmp_path / "st.jsonl")
    rep = run_can_selftest(l1, l2, session=sess, bitrates=(500_000, 125_000))
    assert rep["passed"], [c for c in rep["checks"] if not c["ok"]]
    assert sum(1 for c in rep["checks"] if "29-bit" in c["check"]) == 4
    assert rep["checks"][-1]["check"] == "listen-only peer does not ACK" and rep["checks"][-1]["ok"]
    # two separate buses: nothing crosses, nothing is acked
    other = C.VirtualCanBus(f"st2-{int(time.time() * 1000)}")
    rep = run_can_selftest(l1, other.end("x", 500_000), bitrates=(500_000,))
    assert not rep["passed"] and all(not c["ok"] for c in rep["checks"][:-1])
    sess.close()


def test_rsh_bridge_and_selftest_route_can(tmp_path, capsys):
    import json, threading
    from rs485harness.cli import main
    tag = int(time.time() * 1000)
    a, b = f"can:virtual:cli-a-{tag}", f"can:virtual:cli-b-{tag}"
    rc = main(["selftest", a, a, "--json", "--session", str(tmp_path / "st.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out[out.index("{"):])["passed"]
    sess = SessionWriter(tmp_path / "sims.jsonl")
    prof = replace(CAN_BATTERY_PROFILES["victron"], period_s=0.1)
    bat = CanBatterySimulator(C.VirtualCanBus.get(f"cli-b-{tag}").end("bat", 500_000),
                              preset("ideal"), prof, sess, chan="bat")
    bat.start()
    try:
        rc = main(["bridge", "--ch1", a, "--ch2", b, "--baud", "500000", "--policy",
                   "cap:ccl_max=20", "--duration", "0.6", "--no-markers", "--json",
                   "--session", str(tmp_path / "br.jsonl")])
    finally:
        bat.stop(); sess.close()
    assert rc == 0
    out = capsys.readouterr().out
    out = json.loads(out[out.index("{"):])
    assert out["mode"] == "modify" and out["stats"]["policy_stats"]["modified"] >= 1
