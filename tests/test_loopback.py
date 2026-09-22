"""Integration tests over the in-process loopback pair: simulator vs
simulator, identify vs simulator, and fault-injection -> diagnose."""
import time

from rs485harness.core.session import SessionWriter, new_session_path
from rs485harness.core.transport import loopback_pair
from rs485harness.protocols.pylontech.dialects import (BATTERY_PROFILES,
                                                       INVERTER_PROFILES,
                                                       DialectProfile)
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.battery_sim import BatterySimulator
from rs485harness.simulators.inverter_sim import InverterSimulator
from rs485harness.analyzer.diagnose import analyze
from rs485harness.analyzer.identify import identify


def make_session(tmp_path, name):
    return SessionWriter(tmp_path / f"{name}.jsonl")


def run_pair(tmp_path, battery_profile, inverter_profile="voltronic-6x",
             cycles=2, state=None):
    sess = make_session(tmp_path, "pair")
    end_inv, end_bat = loopback_pair("inv", "bat")
    profile = (battery_profile if isinstance(battery_profile, DialectProfile)
               else BATTERY_PROFILES[battery_profile])
    # fast responses for test speed
    profile = DialectProfile(**{**profile.to_dict(), "response_delay_s": 0.0,
                                "name": profile.name})
    inv_profile = INVERTER_PROFILES[inverter_profile]
    inv_profile = type(inv_profile)(**{**inv_profile.to_dict(),
                                       "period_s": 0.05,
                                       "response_timeout_s": 0.5})
    bat = BatterySimulator(end_bat, state or preset("ideal"), profile, sess,
                           chan="bat")
    inv = InverterSimulator(end_inv, inv_profile, sess, chan="inv")
    bat.start()
    stats = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        stats = inv.run(duration=0.4)
        if stats["polls"] >= cycles * len(inv_profile.commands):
            break
    bat.stop()
    sess.close()
    return stats, bat.stats, sess.path


def test_ideal_session_all_answered(tmp_path):
    inv_stats, bat_stats, path = run_pair(tmp_path, "ideal-pylontech")
    assert inv_stats["responses"] > 0
    assert inv_stats["timeouts"] == 0
    for cid2 in ("61", "62", "63"):
        assert inv_stats["per_cid2"][cid2]["ok"] > 0
    report = analyze(path)
    severities = {f["severity"] for f in report["findings"]}
    assert "fail" not in severities, report["findings"]


def test_monitoring_4x_family(tmp_path):
    inv_stats, _, path = run_pair(tmp_path, "ideal-pylontech", "monitoring-4x")
    assert inv_stats["timeouts"] == 0
    for cid2 in ("42", "51", "47", "92"):
        assert inv_stats["per_cid2"][cid2]["ok"] > 0


def test_family_mismatch_detected(tmp_path):
    # battery answers only 4x; voltronic-style inverter polls 6x -> RTN=04
    inv_stats, _, path = run_pair(tmp_path, "pylontech-4x-only")
    assert inv_stats["responses"] > 0
    assert all(v["ok"] == 0 for v in inv_stats["per_cid2"].values())
    report = analyze(path)
    assert any(f["hypothesis"] == "H4-command-family" and f["severity"] == "fail"
               for f in report["findings"]), report["findings"]


def test_silent_battery_detected(tmp_path):
    inv_stats, _, path = run_pair(tmp_path, DialectProfile(
        name="mute", families=(), unknown_cmd="silence"))
    assert inv_stats["responses"] == 0
    report = analyze(path)
    assert any("unanswered" in f["hypothesis"] for f in report["findings"])


def test_cid2_echo_quirk_detected(tmp_path):
    inv_stats, _, path = run_pair(tmp_path, "buggy-cid2-echo")
    report = analyze(path)
    assert any(f["hypothesis"] == "H7-cid2-echo" for f in report["findings"]), \
        report["findings"]


def test_identify_against_battery_sim(tmp_path):
    """The user scenario: battery-sim on one channel, identify on the other."""
    sess = make_session(tmp_path, "identify")
    end_id, end_bat = loopback_pair("id", "bat")
    profile = DialectProfile(**{**BATTERY_PROFILES["ideal-pylontech"].to_dict(),
                                "response_delay_s": 0.0})
    bat = BatterySimulator(end_bat, preset("seplos-280l"), profile, sess,
                           chan="bat")
    bat.start()
    report = identify(end_id, sess, chan="id", listen_s=0.3)
    bat.stop()
    sess.close()
    verdicts = " ".join(report["verdicts"])
    assert "RESPONDER: Pylontech-ASCII battery-like device" in verdicts
    assert report.get("identity", {}).get("device_name") == "MASON280L"


def test_state_reflected_in_responses(tmp_path):
    state = preset("ideal")
    state.soc_percent = 17
    state.current_a = -20.0
    sess = make_session(tmp_path, "soc")
    end_inv, end_bat = loopback_pair("inv", "bat")
    profile = DialectProfile(**{**BATTERY_PROFILES["ideal-pylontech"].to_dict(),
                                "response_delay_s": 0.0})
    bat = BatterySimulator(end_bat, state, profile, sess, chan="bat")
    bat.start()
    inv = InverterSimulator(end_inv, INVERTER_PROFILES["voltronic-6x"], sess)
    res = inv.poll_once(0x61)
    bat.stop()
    sess.close()
    assert res["status"] == "response"
    assert res["payload"]["soc_percent"] == 17
    assert res["payload"]["current_a"] == -20.0
