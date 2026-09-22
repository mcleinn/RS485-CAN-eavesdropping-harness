"""Scenario engine: send, reactive responder, master send/branch."""
import threading
from pathlib import Path

from rs485harness.core.session import SessionWriter
from rs485harness.core.transport import loopback_pair
from rs485harness.protocols.pylontech import frames as F
from rs485harness.scripting import (build_frame, match_frame, load_scenario,
                                    run_responder, run_master, ScenarioError)
from rs485harness.protocols.pylontech.dialects import (BATTERY_PROFILES,
                                                       INVERTER_PROFILES,
                                                       DialectProfile)
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.battery_sim import BatterySimulator
from rs485harness.simulators.inverter_sim import InverterSimulator

EX = Path(__file__).resolve().parents[1] / "docs" / "examples"


def test_build_frame_fields_and_echo():
    req = F.parse_frame(F.encode_frame(0x20, 0x07, 0x46, 0x61, ""))
    out = build_frame({"adr": "$req.adr", "cid2": "00", "info": "AABB"}, req=req)
    pf = F.parse_frame(out)
    assert pf.ok and pf.adr == 0x07 and pf.cid2 == 0x00 and pf.info == "AABB"


def test_build_frame_raw_and_shutdown_guard():
    # ascii form (starts with ~) is encoded to real bytes + CR
    raw = build_frame({"hex": "~20014642E00201FD35"})
    assert F.parse_frame(raw).cid2 == 0x42
    # true raw bytes verbatim (may be malformed)
    raw2 = build_frame({"raw_hex": F.encode_frame(0x20, 1, 0x46, 0x42, "01").hex()})
    assert F.parse_frame(raw2).ok
    try:
        build_frame({"cid2": "64"})
        assert False, "should refuse 0x64"
    except ScenarioError:
        pass
    assert build_frame({"cid2": "64"}, allow_shutdown=True)  # allowed


def test_match_frame_conditions():
    pf = F.parse_frame(F.encode_frame(0x25, 0x02, 0x46, 0x61, ""))
    assert match_frame(pf, {"cid2": "61", "ver": "25"})
    assert match_frame(pf, {"cid2": ["60", "61"], "family": "6x"})
    assert not match_frame(pf, {"cid2": "42"})
    assert match_frame(None, {"no_reply": True})


def test_example_scenarios_load():
    for f in ("responder-echo-adr.json", "master-address-scan.json"):
        sc = load_scenario(str(EX / f))
        assert sc["mode"] in ("responder", "master")


def test_responder_scenario(tmp_path):
    """Run the example responder on one end; poll it from the other and
    check it answers 0x61/0x63 and RTN04s an unknown command."""
    sess = SessionWriter(tmp_path / "resp.jsonl")
    end_resp, end_inv = loopback_pair("resp", "inv")
    scenario = load_scenario(str(EX / "responder-echo-adr.json"))
    stop = threading.Event()

    def run():
        run_responder(end_resp, scenario, sess, chan="resp")
    # run responder in a thread with a duration
    t = threading.Thread(target=lambda: run_responder(
        end_resp, scenario, sess, chan="resp", duration=2.0), daemon=True)
    t.start()

    inv = InverterSimulator(end_inv, type(INVERTER_PROFILES["voltronic-6x"])(
        **{**INVERTER_PROFILES["voltronic-6x"].to_dict(),
           "response_timeout_s": 0.5}), sess, chan="inv")
    r61 = inv.poll_once(0x61)
    r63 = inv.poll_once(0x63)
    r42 = inv.poll_once(0x42)  # not in rules -> RTN04
    t.join(timeout=3)
    sess.close()

    assert r61["status"] == "response" and r61["payload"]["soc_percent"] == 81
    assert r63["status"] == "response"
    assert r63["payload"]["charge_voltage_limit_v"] == 57.6
    assert r42["status"] == "response" and r42["rtn"] == 0x04


def test_responder_silence_rule(tmp_path):
    sess = SessionWriter(tmp_path / "silent.jsonl")
    end_resp, end_inv = loopback_pair("resp", "inv")
    scenario = {"mode": "responder", "unmatched": "silence",
                "rules": [{"when": {"cid2": "61"}, "respond": None,
                           "note": "play dead on 61"}]}
    t = threading.Thread(target=lambda: run_responder(
        end_resp, scenario, sess, chan="resp", duration=1.0), daemon=True)
    t.start()
    inv = InverterSimulator(end_inv, type(INVERTER_PROFILES["voltronic-6x"])(
        **{**INVERTER_PROFILES["voltronic-6x"].to_dict(),
           "response_timeout_s": 0.3}), sess, chan="inv")
    r = inv.poll_once(0x61)
    t.join(timeout=2)
    sess.close()
    assert r["status"] == "timeout"


def test_master_scenario_finds_battery_address(tmp_path):
    """Master address-scan should stop at the address the battery answers.
    Battery-sim (ideal) responds on ADR 02 -> scan stops at a02."""
    sess = SessionWriter(tmp_path / "master.jsonl")
    end_master, end_bat = loopback_pair("master", "bat")
    # make the battery answer ONLY on ADR 02 so the scan's branching is
    # exercised (otherwise a sim that answers every address stops at 00)
    profile = DialectProfile(**{**BATTERY_PROFILES["ideal-pylontech"].to_dict(),
                                "response_delay_s": 0.0, "listen_adrs": (0x02,)})
    bat = BatterySimulator(end_bat, preset("ideal"), profile, sess, chan="bat")
    bat.start()
    scenario = load_scenario(str(EX / "master-address-scan.json"))
    for s in scenario["steps"]:
        s["wait_ms"] = 300
    stats = run_master(end_master, scenario, sess, chan="master")
    bat.stop()
    sess.close()
    assert stats["responses"] >= 1
    last = stats["transcript"][-1]
    assert "ADR 02" in (last["note"] or ""), stats["transcript"]
    assert last["goto"] == "stop"
