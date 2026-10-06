"""Seplos BMS 3.0 service interface (native Modbus): discovery finds it by
itself, names the settings, and the guarded write only does what it says.
Hardware-free: a simulated service socket loaded with the documented
example read-out of a real pack."""
from rs485harness.core.transport import loopback_pair
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.analyzer.discover import discover, format_discovery
from rs485harness.analyzer.identify import identify
from rs485harness.analyzer import seplos_native as SN
from rs485harness.simulators.seplos_native_sim import SeplosNativeSim, EXAMPLE_SPA
from rs485harness.simulators.battery_sim import BatterySimulator
from rs485harness.simulators.battery_state import preset
from rs485harness.protocols.pylontech.dialects import BATTERY_PROFILES


def _service_socket(tmp_path, **kw):
    a, b = loopback_pair("pc", "bms")
    sim = SeplosNativeSim(b, **kw).start()
    sess = SessionWriter(tmp_path / "s.jsonl")
    return a, sim, sess


def test_decode_documented_example():
    spa = {e["no"]: e for e in SN.decode_block("SPA", EXAMPLE_SPA)}
    assert spa[102]["name"] == "Charging request voltage" and spa[102]["value"] == 57.6
    assert spa[103]["value"] == 180 and spa[104]["value"] == -180
    assert (spa[11]["value"], spa[12]["value"], spa[13]["value"], spa[14]["value"]) == \
        (3.4, 3.5, 3.4, 3.65)
    assert (spa[4]["value"], spa[6]["value"]) == (56.0, 57.6)
    assert spa[105]["text"] == "Victron_CAN"
    assert spa[49]["value"] == 50.0            # 3231 * 0.1 K -> degC
    assert SN._sanity({"SPA": list(spa.values())}) == []


def test_discover_recognises_service_socket_and_names_settings(tmp_path):
    link, sim, sess = _service_socket(tmp_path)
    try:
        rep = discover(link, sess)
    finally:
        sim.stop(); sess.close()
    assert rep["responder"] == {"protocol": "seplos-native", "slave": 0}
    assert rep["sanity"] == []
    spa = {e["no"]: e["value"] for e in rep["seplos_native"]["SPA"]}
    assert len(spa) == 106 and spa[102] == 57.6 and spa[2] == 16
    text = format_discovery(rep)
    assert "SERVICE socket" in text
    assert "102  Charging request voltage" in text and "57.6 V" in text
    assert "cell spread: 7 mV" in text
    # read-only: no write function code ever left the PC
    sent = [bytes.fromhex(r["hex"]) for r in read_session(tmp_path / "s.jsonl")
            if r.get("dir") == "tx"]
    assert sent and all(f[1] in (0x04,) or f[:1] == b"~" for f in sent)
    assert sim.writes == []


def test_discover_on_inverter_socket_says_no_settings_here(tmp_path):
    a, b = loopback_pair("pc", "bat")
    sess = SessionWriter(tmp_path / "s.jsonl")
    profile = BATTERY_PROFILES["ideal-pylontech"]
    bat = BatterySimulator(b, preset("ideal"), profile, sess)
    bat.start()
    try:
        rep = discover(a, sess)
    finally:
        bat.stop(); sess.close()
    assert rep["responder"]["protocol"] == "pylontech"
    assert "seplos_native" not in rep


def test_identify_names_the_service_socket(tmp_path):
    link, sim, sess = _service_socket(tmp_path)
    try:
        rep = identify(link, sess, listen_s=0.2, bauds=(19200,), adrs=(1,),
                       vers=(0x20,))
    finally:
        sim.stop(); sess.close()
    assert any("SERVICE socket" in v for v in rep["verdicts"]), rep["verdicts"]


def test_write_is_dry_run_by_default(tmp_path):
    link, sim, sess = _service_socket(tmp_path)
    try:
        rep = SN.write_setting(link, sess, "svc", 0, 102, 55.2)
    finally:
        sim.stop(); sess.close()
    assert rep["result"] == "dry-run" and rep["before"] == 57.6
    assert sim.writes == [] and sim.regs[0x1365] == 5760
    assert rep["frame_hex"].startswith("00101365000102159")   # 5520 = 0x1590


def test_write_applies_and_reads_back(tmp_path):
    link, sim, sess = _service_socket(tmp_path)
    try:
        rep = SN.write_setting(link, sess, "svc", 0, 102, 55.2, apply=True)
        back = SN.write_setting(link, sess, "svc", 0, 102, 57.6, apply=True,
                                allow_increase=True)
    finally:
        sim.stop(); sess.close()
    assert rep["result"] == "written" and rep["after"] == 55.2
    assert rep["write_ack"] == "ok" and "--allow-increase" in rep["restore"]
    assert back["result"] == "written" and sim.regs[0x1365] == 5760
    assert sim.writes == [(0x1365, 5520), (0x1365, 5760)]


def test_write_refusals(tmp_path):
    link, sim, sess = _service_socket(tmp_path)
    try:
        not_listed = SN.write_setting(link, sess, "svc", 0, 14, 3.9, apply=True)
        too_high = SN.write_setting(link, sess, "svc", 0, 102, 58.4, apply=True)
        too_low = SN.write_setting(link, sess, "svc", 0, 102, 40.0, apply=True)
        sim.regs[0x1365] = 5520
        increase = SN.write_setting(link, sess, "svc", 0, 102, 56.0, apply=True)
        same = SN.write_setting(link, sess, "svc", 0, 102, 55.2, apply=True)
    finally:
        sim.stop(); sess.close()
    assert not_listed["result"].startswith("refused: setting 14")
    assert too_high["result"].startswith("refused") and too_low["result"].startswith("refused")
    assert "needs --allow-increase" in increase["result"]
    assert same["result"] == "unchanged"
    assert sim.writes == []


def test_write_reports_a_bms_that_does_not_take_the_value(tmp_path):
    link, sim, sess = _service_socket(tmp_path, accept_writes=False)
    try:
        rep = SN.write_setting(link, sess, "svc", 0, 103, 40, apply=True)
    finally:
        sim.stop(); sess.close()
    assert rep["result"].startswith("failed: read-back shows 180")
    assert "exception" in rep["write_ack"]


def test_implausible_readout_blocks_writing(tmp_path):
    link, sim, sess = _service_socket(tmp_path)
    sim.regs[0x130D] = 9000            # 'cell over-voltage protection' 9 V
    try:
        rep = SN.write_setting(link, sess, "svc", 0, 102, 55.2, apply=True)
        disc = discover(link, sess)
    finally:
        sim.stop(); sess.close()
    assert rep["result"].startswith("refused: settings read-out is implausible")
    assert sim.writes == []
    assert disc["sanity"] and "!! implausible" in format_discovery(disc)
