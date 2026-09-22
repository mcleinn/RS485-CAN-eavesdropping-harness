"""Full-suite simulation over the virtual 2-channel analyzer:
selftest, bridge-in-the-middle, baud-mismatch garbling, echo handling,
and passive identify - all without hardware."""
import time

from rs485harness.core.session import SessionWriter
from rs485harness.core.virtual import VirtualAnalyzer, VirtualBus
from rs485harness.analyzer.selftest import run_selftest
from rs485harness.analyzer.bridge import Bridge
from rs485harness.analyzer.classify import classify_raw
from rs485harness.analyzer.identify import identify
from rs485harness.protocols.pylontech import frames as F
from rs485harness.protocols.pylontech.dialects import (BATTERY_PROFILES,
                                                       INVERTER_PROFILES,
                                                       DialectProfile,
                                                       InverterPollProfile)
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.battery_sim import BatterySimulator
from rs485harness.simulators.inverter_sim import InverterSimulator


def fast_battery_profile(**over) -> DialectProfile:
    base = BATTERY_PROFILES["ideal-pylontech"].to_dict()
    base.update(response_delay_s=0.0, **over)
    return DialectProfile(**base)


def fast_inverter_profile(**over) -> InverterPollProfile:
    base = INVERTER_PROFILES["voltronic-6x"].to_dict()
    base.update({"period_s": 0.05, "response_timeout_s": 0.5, **over})
    return InverterPollProfile(**base)


def test_selftest_on_virtual_analyzer():
    va = VirtualAnalyzer()
    va.cross_wire()
    report = run_selftest(links=(va.ch1, va.ch2))
    assert report["passed"], [c for c in report["checks"] if not c["ok"]]


def test_selftest_fails_without_crosswire():
    va = VirtualAnalyzer()  # buses NOT joined = cable missing
    report = run_selftest(links=(va.ch1, va.ch2), bauds=(9600,))
    assert not report["passed"]


def test_echo_detection():
    va = VirtualAnalyzer(echo=True)
    va.cross_wire()
    assert va.ch1.detect_echo() is True
    va2 = VirtualAnalyzer(echo=False)
    va2.cross_wire()
    assert va2.ch1.detect_echo() is False


def test_bridge_in_the_middle(tmp_path):
    """Inverter on bus1, battery on bus2, harness bridges ch1<->ch2."""
    va = VirtualAnalyzer()
    inv_end = va.bus1.end("inverter")
    bat_end = va.bus2.end("battery")
    sess = SessionWriter(tmp_path / "bridge.jsonl")
    bat = BatterySimulator(bat_end, preset("ideal"), fast_battery_profile(),
                           sess, chan="bat")
    inv = InverterSimulator(inv_end, fast_inverter_profile(), sess, chan="inv")
    br = Bridge(va.ch1, va.ch2, sess)
    bt = bat.start()
    import threading
    bridge_thread = threading.Thread(target=br.run, daemon=True)
    bridge_thread.start()
    stats = inv.run(duration=1.5)
    br.stop()
    bridge_thread.join(timeout=3)
    bat.stop()
    sess.close()
    assert stats["responses"] > 0
    assert stats["per_cid2"]["61"]["ok"] > 0
    assert br.stats["ch1"]["frames"] > 0 and br.stats["ch2"]["frames"] > 0


def test_baud_mismatch_garbles():
    bus = VirtualBus()
    tx = bus.end("tx", baud=9600)
    rx_ok = bus.end("rx-ok", baud=9600)
    rx_bad = bus.end("rx-bad", baud=19200)
    frame = F.encode_frame(0x20, 0x01, 0x46, 0x61, "")
    tx.write(frame)
    assert rx_ok.read(0.2).data == frame
    garbled = rx_bad.read(0.2).data
    assert garbled != frame
    verdict = classify_raw(garbled)
    assert verdict["verdict"] in ("line-noise", "binary-unknown")


def test_passive_identify_hears_inverter(tmp_path):
    """User scenario: inverter-sim on one bus, identify listening on the
    analyzer channel of that same bus."""
    va = VirtualAnalyzer()
    inv_end = va.bus1.end("inverter")
    sess = SessionWriter(tmp_path / "id.jsonl")
    inv = InverterSimulator(inv_end, fast_inverter_profile(response_timeout_s=0.1),
                            sess, chan="inv")
    inv.start()
    report = identify(va.ch1, sess, chan="id", listen_s=1.0, active=False)
    inv.stop()
    sess.close()
    verdicts = " ".join(report["verdicts"])
    assert "ACTIVE TALKER" in verdicts and "INVERTER" in verdicts


def test_identify_recognizes_modbus_polling_inverter(tmp_path):
    """Inverter whose 'PYL' is actually the Voltronic-LIB Modbus register
    protocol: identify must say so, name the register map, and advise a
    settings change; diagnose must raise H9 with the same advice."""
    from rs485harness.analyzer.classify import crc16_modbus
    from rs485harness.analyzer.diagnose import analyze

    va = VirtualAnalyzer()
    inv_end = va.bus1.end("modbus-inverter")
    # Voltronic-LIB style reads: slave 1, func 0x03, registers 0x0000/0x0030
    for start in (0x0000, 0x0030, 0x0070):
        pdu = bytes([0x01, 0x03, start >> 8, start & 0xFF, 0x00, 0x04])
        crc = crc16_modbus(pdu)
        inv_end.write(pdu + bytes([crc & 0xFF, crc >> 8]))

    sess = SessionWriter(tmp_path / "modbus-id.jsonl")
    report = identify(va.ch1, sess, chan="id", listen_s=0.6, active=False)
    sess.close()

    verdicts = " ".join(report["verdicts"])
    assert "Modbus RTU polling" in verdicts
    assert "voltronic-lib" in verdicts
    assert "switch the battery" in verdicts or "LIB" in verdicts

    diag = analyze(tmp_path / "modbus-id.jsonl")
    h9 = [f for f in diag["findings"] if f["hypothesis"] == "H9-modbus-detected"]
    assert h9 and "Voltronic 'LIB' map" in h9[0]["detail"]


def test_active_identify_finds_battery_on_bus(tmp_path):
    va = VirtualAnalyzer()
    bat_end = va.bus2.end("battery")
    sess = SessionWriter(tmp_path / "id2.jsonl")
    bat = BatterySimulator(bat_end, preset("seplos-280l"),
                           fast_battery_profile(), sess, chan="bat")
    bat.start()
    report = identify(va.ch2, sess, chan="id", listen_s=0.2)
    bat.stop()
    sess.close()
    assert any("RESPONDER: Pylontech-ASCII battery-like" in v
               for v in report["verdicts"])


def start_lib_battery(bat_end):
    """Modbus-slave battery in Voltronic/LIB mode on node 0x01/0x0F.
    Register 0x0010 block = cell count 16 + first cell voltages (mV);
    0x0033 (SOC) = 42 within the status block. Returns (thread, stop)."""
    import threading
    from rs485harness.analyzer.classify import crc16_modbus, find_modbus_frames

    stop = threading.Event()

    def regs_for(start, count):
        base = {0x0010: 16, 0x0011: 3350, 0x0012: 3351, 0x0013: 3352,
                0x0025: 6, 0x0026: 2981, 0x0032: 512, 0x0033: 42,
                0x0036: 1, 0x0070: 562, 0x0071: 460, 0x0072: 1000,
                0x0073: 1000, 0x0074: 0xC0}
        return [base.get(start + i, 0) for i in range(count)]

    def responder():
        buf = b""
        while not stop.is_set():
            chunk = bat_end.read(timeout=0.05)
            if not chunk:
                buf = b""  # bus went quiet - resync like a real slave
                continue
            buf += chunk.data
            hits = find_modbus_frames(buf)
            for hit in hits:
                if hit["slave"] not in (0x01, 0x0F) or hit["function"] != 0x03:
                    continue  # not addressed to us -> stay silent
                if "start" not in hit:
                    continue
                words = regs_for(int(hit["start"], 16), hit["count"])
                body = bytes([hit["slave"], 0x03, 2 * len(words)]) + b"".join(
                    w.to_bytes(2, "big") for w in words)
                rcrc = crc16_modbus(body)
                bat_end.write(body + bytes([rcrc & 0xFF, rcrc >> 8]))
            if hits:
                last = hits[-1]
                buf = buf[last["offset"] + last["length"]:]
            elif len(buf) > 256:
                buf = buf[-16:]

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    return t, stop


def test_identify_finds_voltronic_lib_battery(tmp_path):
    """Battery switched to its Voltronic/LIB profile = a Modbus slave on
    node 0x01. identify must find it, read the cell count, and say which
    setting change follows."""
    va = VirtualAnalyzer()
    bat_end = va.bus2.end("lib-battery")
    t, stop = start_lib_battery(bat_end)
    sess = SessionWriter(tmp_path / "lib.jsonl")
    report = identify(va.ch2, sess, chan="id", listen_s=0.2)
    stop.set()
    t.join(timeout=2)
    sess.close()

    verdicts = " ".join(report["verdicts"])
    assert "Modbus RTU device" in verdicts
    assert "16 cells" in verdicts
    assert "Voltronic/LIB mode confirmed" in verdicts
    # and no false Pylontech-responder claim
    assert "Pylontech-ASCII battery-like" not in verdicts


def test_discover_full_inventory_of_battery_sim(tmp_path):
    """discover must enumerate supported commands, decode state, and flag
    what is unsupported."""
    from rs485harness.analyzer.discover import discover

    va = VirtualAnalyzer()
    bat_end = va.bus2.end("battery")
    sess = SessionWriter(tmp_path / "disc.jsonl")
    state = preset("seplos-280l")
    state.soc_percent = 55
    bat = BatterySimulator(bat_end, state, fast_battery_profile(), sess,
                           chan="bat")
    bat.start()
    report = discover(va.ch2, sess, chan="disc")
    bat.stop()
    sess.close()

    assert report["responder"]["protocol"] == "pylontech"
    for cid2 in ("42", "47", "51", "61", "62", "63", "92", "93"):
        assert cid2 in report["supported"], (cid2, report["commands"][cid2])
    assert "60" in report["unsupported"]  # sim answers RTN=04 there
    st = report["state"]
    assert st["soc_percent"] == 55
    assert st["identity"]["device_name"] == "MASON280L"
    assert st["cells"] == 16
    assert st["limits"]["charge_voltage_limit_v"] == 56.8
    assert report["silent"] == []


def test_discover_flags_cid2_echo_quirk(tmp_path):
    from rs485harness.analyzer.discover import discover

    va = VirtualAnalyzer()
    bat_end = va.bus2.end("battery")
    sess = SessionWriter(tmp_path / "disc2.jsonl")
    bat = BatterySimulator(bat_end, preset("ideal"),
                           fast_battery_profile(response_cid2="echo",
                                                name="buggy"),
                           sess, chan="bat")
    bat.start()
    report = discover(va.ch2, sess, chan="disc")
    bat.stop()
    sess.close()
    assert any("echo CID2" in q for q in report["quirks"]), report["quirks"]
    assert "61" in report["supported"]  # still decoded despite the quirk


def test_discover_voltronic_lib_battery(tmp_path):
    from rs485harness.analyzer.discover import discover

    va = VirtualAnalyzer()
    bat_end = va.bus2.end("lib-battery")
    t, stop = start_lib_battery(bat_end)
    sess = SessionWriter(tmp_path / "disc3.jsonl")
    report = discover(va.ch2, sess, chan="disc")
    stop.set()
    t.join(timeout=2)
    sess.close()

    assert report["responder"]["protocol"] == "modbus"
    d = report["decoded"]
    assert d["cell_count"] == 16
    assert d["soc_percent"] == 42
    assert d["module_voltage_v"] == 51.2
    assert d["charge_discharge_status"]["charge_enable"] is True
    assert d["charge_voltage_limit_v"] == 56.2
    assert d["any_alarm_or_protect"] is False


def test_identify_session_logs_everything(tmp_path):
    """Completeness: identify's probes (tx), answers (rx), phase/baud notes
    and final verdicts must all be recoverable from the session file."""
    from rs485harness.core.session import read_session

    va = VirtualAnalyzer()
    bat_end = va.bus2.end("battery")
    sess = SessionWriter(tmp_path / "log.jsonl")
    bat = BatterySimulator(bat_end, preset("ideal"), fast_battery_profile(),
                           sess, chan="bat")
    bat.start()
    identify(va.ch2, sess, chan="id", listen_s=0.2)
    bat.stop()
    sess.close()

    recs = list(read_session(tmp_path / "log.jsonl"))
    id_recs = [r for r in recs if r["chan"] == "id"]
    assert any(r["dir"] == "tx" and r["kind"] == "frame" for r in id_recs), \
        "pylontech probe requests not logged"
    assert any(r["dir"] == "rx" and r["kind"] == "frame" for r in id_recs), \
        "probe responses not logged"
    assert any(r["dir"] == "tx" and r["kind"] == "raw"
               and "modbus probe" in (r.get("note") or "") for r in id_recs), \
        "modbus probes not logged"
    notes = [r.get("note", "") for r in recs if r["kind"] == "note"]
    assert any("phase 1" in n for n in notes), "passive-phase note missing"
    assert any("phase 2" in n for n in notes), "active-phase note missing"
    assert any("identify done" in n for n in notes), "verdict note missing"


def test_selftest_session_and_report(tmp_path):
    from rs485harness.core.session import read_session, save_report

    va = VirtualAnalyzer()
    va.cross_wire()
    sess = SessionWriter(tmp_path / "st.jsonl")
    report = run_selftest(links=(va.ch1, va.ch2), bauds=(9600,), session=sess)
    sess.close()
    report_path = save_report(tmp_path / "st.jsonl", report, "selftest")

    notes = [r.get("note", "") for r in read_session(tmp_path / "st.jsonl")
             if r["kind"] == "note"]
    assert any("selftest start" in n for n in notes)
    assert any("[PASS]" in n for n in notes)
    assert any("selftest done" in n for n in notes)
    assert report_path.exists()
    import json
    assert json.loads(report_path.read_text())["passed"] is True
