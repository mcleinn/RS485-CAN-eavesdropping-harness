"""Continuous Seplos service-socket monitor and its merge into the timeline.
Hardware-free: simulated service socket."""
import csv

from rs485harness.core.transport import loopback_pair
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.analyzer import seplos_native as SN
from rs485harness.analyzer.seplos_monitor import ServiceMonitor
from rs485harness.analyzer.timeline import build_timeline, export, format_timeline
from rs485harness.simulators.seplos_native_sim import SeplosNativeSim

from test_phase2 import _synthetic_capture


def _setup(tmp_path, **kw):
    a, b = loopback_pair("pc", "bms")
    sim = SeplosNativeSim(b).start()
    sess = SessionWriter(tmp_path / "svc.jsonl")
    lines: list[str] = []
    mon = ServiceMonitor(a, sess, on_event=lines.append, **kw)
    return sim, sess, mon, lines


def _end_of_charge(sim):
    """Cell 12 runs ahead (cell 7 is the top cell in the example data), the battery raises its alarm, ends the charge and
    opens its charge switch."""
    sim.regs[0x110B] = 3560                 # cell 12
    sim.regs[0x1010] = 0                    # BMS allowed charge current
    sim.coils[0x121B] = True                # cell 12 high-voltage alarm
    sim.coils[0x1248] = True                # Cell high voltage alarm
    sim.coils[0x1241] = False               # Charge
    sim.coils[0x1243] = True                # Full charge
    sim.coils[0x1279] = False               # Charge FET on
    sim.coils[0x1232] = True                # cell 3 balancing


def test_decode_pic_groups():
    bits = [False] * 144
    for i in (0x16, 0x32, 0x41, 0x48, 0x78, 0x79):
        bits[i] = True
    d = SN.decode_pic(bits)
    assert d["cells_high_alarm"] == [7] and d["balancing"] == [3]
    assert d["mode"] == ["Charge"] and d["flags"] == ["Cell high voltage alarm"]
    assert d["fets"]["Charge FET on"] and not d["fets"]["Current limiting FET on"]


def test_monitor_logs_samples_and_reports_changes(tmp_path):
    sim, sess, mon, lines = _setup(tmp_path)
    try:
        first = mon.poll_once()
        _end_of_charge(sim)
        second = mon.poll_once()
    finally:
        sim.stop(); sess.close()
    assert first["cells"][6] == 3.275 and len(first["cells"]) == 16
    assert first["mode"] == ["Charge"] and first["fets"]["Charge FET on"]
    assert first["max_cell_no"] == 7
    assert second["max_cell_no"] == 12 and second["spread_mv"] == 292
    text = "\n".join(lines)
    assert "SVC STATE" in lines[0]
    for needle in ("battery mode Charge -> Full charge", "Charge FET on: OFF",
                   "flag SET: Cell high voltage alarm",
                   "cells in high-voltage alarm: [12]", "top cell is now #12",
                   "BMS allowed charge current 180 -> 0 A"):
        assert needle in text, needle
    recs = [r for r in read_session(tmp_path / "svc.jsonl") if r["kind"] == "service"]
    assert len(recs) == 2
    assert recs[1]["decoded"]["cells_high_alarm"] == [12]
    assert len(recs[1]["raw"]["pia"]) == 18 and len(recs[1]["raw"]["pib"]) == 26
    assert sim.writes == []                 # read-only


def test_monitor_reports_an_outage_once_and_the_recovery(tmp_path):
    sim, sess, mon, lines = _setup(tmp_path)
    try:
        mon.slave = 5                       # nobody answers
        assert mon.poll_once() is None and mon.poll_once() is None
        mon.slave = 0
        assert mon.poll_once() is not None
    finally:
        sim.stop(); sess.close()
    assert sum("not answering" in l for l in lines) == 1
    assert any("answering again" in l for l in lines)
    assert mon.stats["failed"] == 2 and mon.stats["ok"] == 1


def test_monitor_notices_a_changed_setting(tmp_path):
    sim, sess, mon, lines = _setup(tmp_path, settings_every_s=0.0)
    try:
        mon.poll_once()
        sim.regs[0x1365] = 5520             # someone lowers setting 102
        mon.poll_once()
    finally:
        sim.stop(); sess.close()
    text = "\n".join(lines)
    assert "102 charging request voltage 57.6 V" in text
    assert "setting 102 Charging request voltage changed 57.6 -> 55.2 V" in text
    changed = [r for r in read_session(tmp_path / "svc.jsonl")
               if r["kind"] == "service-settings" and r.get("changed")]
    assert len(changed) == 1


def test_timeline_uses_service_side(tmp_path):
    sim, sess, mon, lines = _setup(tmp_path)
    try:
        mon.poll_once()
        mon.poll_once()
        _end_of_charge(sim)
        mon.poll_once()
    finally:
        sim.stop(); sess.close()
    res = build_timeline(tmp_path / "svc.jsonl")
    sv = res["stats"]["service"]
    assert sv["samples"] == 3 and sv["highest_cell"]["cell"] == 12
    assert sv["cell_peak_v"]["12"] == 3.56 and sv["worst_spread"]["spread_mv"] == 292
    assert "Cell high voltage alarm" in sv["flags_seen"]
    assert sv["bms_charge_a_range"] == [0, 180]
    kinds = {f["kind"]: f for f in res["findings"]}
    assert kinds["battery-alarm"]["severity"] == "info"
    opened = kinds["battery-opened-charge-switch"]
    assert opened["severity"] == "warn" and opened["pack_i_before"] == 13.01
    assert any(e["kind"] == "svc" and "top cell is now #12" in e["text"]
               for e in res["events"])
    text = format_timeline(res)
    assert "SERVICE SIDE" in text and "highest cell: #12 at 3.56 V" in text
    paths = export(res, tmp_path / "svc.jsonl")
    rows = list(csv.DictReader(open(paths["service_csv"], encoding="utf-8")))
    assert len(rows) == 3 and rows[2]["cell12"] == "3.56"
    assert rows[2]["charge_fet"] == "False" and rows[2]["cells_high_alarm"] == "12"


def test_timeline_merges_a_separately_recorded_service_file(tmp_path):
    main = tmp_path / "tap.jsonl"
    _synthetic_capture(main)                # inverter-link capture
    sim, sess, mon, lines = _setup(tmp_path)
    try:
        mon.poll_once()
        _end_of_charge(sim)
        mon.poll_once()
    finally:
        sim.stop(); sess.close()
    alone = build_timeline(main)
    merged = build_timeline(main, service_path=tmp_path / "svc.jsonl")
    assert "service" not in alone["stats"] and alone["service_series"] == []
    assert merged["stats"]["requests"] == alone["stats"]["requests"] == 18
    assert merged["stats"]["service"]["samples"] == 2
    assert {f["kind"] for f in alone["findings"]} <= {f["kind"] for f in merged["findings"]}
    ts = [e["t"] for e in merged["events"]]
    assert ts == sorted(ts)


def test_tap_and_service_monitor_share_one_session(tmp_path):
    """What `rsh sniff --service` does: a passive tap on the inverter link
    and the service monitor write into the same session on one clock."""
    import time
    from rs485harness.core.virtual import VirtualBus
    from rs485harness.analyzer.sniff import Sniffer
    from rs485harness.simulators.battery_sim import BatterySimulator
    from rs485harness.simulators.inverter_sim import InverterSimulator
    from rs485harness.simulators.battery_state import preset
    from test_virtual_analyzer import fast_battery_profile, fast_inverter_profile

    bus = VirtualBus("inverter-link")
    sess = SessionWriter(tmp_path / "both.jsonl")
    quiet = SessionWriter(tmp_path / "sims.jsonl")      # the sims' own logs
    bat = BatterySimulator(bus.end("battery"), preset("ideal"),
                           fast_battery_profile(), quiet, chan="bat")
    inv = InverterSimulator(bus.end("inverter"), fast_inverter_profile(),
                            quiet, chan="inv")
    a, b = loopback_pair("pc", "bms")
    sim = SeplosNativeSim(b).start()
    lines: list[str] = []
    sn = Sniffer({"ch1": bus.end("tap")}, sess, on_event=lines.append,
                 markers=False, quiet=True)
    mon = ServiceMonitor(a, sess, interval_s=0.3, on_event=lines.append,
                         print_limits=False)
    bat.start(); inv.start()
    assert mon.poll_once() is not None
    mon.start(duration=2.0)
    try:
        time.sleep(0.8)
        _end_of_charge(sim)
        sn.run(duration=1.6)
    finally:
        mon.stop(); inv.stop(); bat.stop(); sim.stop()
        sess.close(); quiet.close()
    res = build_timeline(tmp_path / "both.jsonl")
    assert res["stats"]["requests"] > 3 and res["stats"]["frame_errors"] == 0
    assert res["stats"]["service"]["samples"] >= 3
    assert "battery-opened-charge-switch" in {f["kind"] for f in res["findings"]}
    assert any("SVC CHANGE" in l and "Charge FET on: OFF" in l for l in lines)
