"""A simulated Dyness DL-series pack (0x42 family only, echo address,
silent on 0x6x) must be fully readable by `dump` and `discover`: the
pre-purchase check relies on cycles / SOH / serial / cells coming through."""
from rs485harness.core.transport import loopback_pair
from rs485harness.core.session import SessionWriter
from rs485harness.protocols.pylontech.dialects import BATTERY_PROFILES, DialectProfile
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.battery_sim import BatterySimulator
from rs485harness.analyzer.dump import dump_stack, detect_addresses
from rs485harness.analyzer.discover import discover


def _sim(tmp_path):
    sess = SessionWriter(str(tmp_path / "dyness.jsonl"))
    end_pc, end_bat = loopback_pair("pc", "bat")
    profile = DialectProfile(**{**BATTERY_PROFILES["dyness-dl"].to_dict(),
                                "response_delay_s": 0.0})
    state = preset("dyness-dl5")
    bat = BatterySimulator(end_bat, state, profile, sess, chan="bat")
    bat.start()
    return sess, end_pc, bat, state


def test_dump_reads_health_fields_from_dyness_sim(tmp_path):
    sess, end_pc, bat, state = _sim(tmp_path)
    try:
        adrs = detect_addresses(end_pc, 0x20, sess, "pc")
        assert 0x02 in adrs
        report = dump_stack(end_pc, sess, adrs=[0x02], chan="pc")
    finally:
        bat.stop(); sess.close()
    assert report["n_units"] == 1
    u = report["units"][0]
    assert u["serial"]["serial_number"] == "DYNESSDL5C000001"
    assert u["identity"]["device_name"].startswith("DL5.0C")
    mod = u["analog"]["modules"][0]
    assert len(mod["cell_v"]) == 16
    assert mod["cycles"] == 812
    assert mod["total_ah"] == 100.0
    assert bat.stats["cid2_seen"].get("61", 0) == 0  # never asked the 6x family here


def test_discover_classifies_dyness_sim(tmp_path):
    sess, end_pc, bat, state = _sim(tmp_path)
    try:
        report = discover(end_pc, sess, chan="pc")
    finally:
        bat.stop(); sess.close()
    assert report["responder"]["protocol"] == "pylontech"
    assert report["responder"]["adr"] == 2
    assert {"42", "44", "47", "4F", "51", "93"} <= set(report["supported"])
    # 6x family: silent, not an error (the inverter family this pack lacks)
    assert {"60", "61", "62", "63"} <= set(report["silent"])
    assert "DYNESSDL5C000001" in str(report["state"])
    assert report["state"]["identity"]["device_name"].startswith("DL5.0C")
