"""Pylontech-family CAN: frame decode/encode against the documented sample
bytes, dialect guessing, battery/inverter simulators over the virtual bus,
identify/discover/dump/soc/sniff/timeline round trips per dialect."""
import json
import time

import pytest

from rs485harness.core import canbus as C
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.protocols.pylontech_can import frames as PF
from rs485harness.protocols.pylontech_can.dialects import (CAN_BATTERY_PROFILES,
                                                           CAN_INVERTER_PROFILES, guess_dialect)
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.can_battery_sim import CanBatterySimulator, CanInverterSimulator
from rs485harness.analyzer.can_battery import (discover_can, dump_can, soc_can, decode_any,
                                               format_discovery_can, format_dump_can)
from rs485harness.analyzer.identify_can import identify_can
from rs485harness.analyzer.identify import identify
from rs485harness.analyzer.sniff import Sniffer
from rs485harness.analyzer.timeline import build_timeline, LiveState, _extract_can, TimelineConfig


def msg(can_id, hexdata, ext=False):
    return C.CanMsg(0.0, can_id, bytes.fromhex(hexdata.replace(" ", "")), ext=ext)


# ------------------------------------------------------------------ frames
def test_decode_real_pack_samples():
    # ArminJo / setfirelabs captures of a real Pylontech pack
    d = PF.decode(msg(0x351, "28 02 F4 01 20 03 E0 01"))
    assert (d["cvl_v"], d["ccl_a"], d["dcl_a"], d["dvl_v"]) == (55.2, 50.0, 80.0, 48.0)
    assert d["flow"] == "BMS->INV" and d["name"] == "limits"
    d = PF.decode(msg(0x355, "3C 00 64 00"))
    assert (d["soc_percent"], d["soh_percent"]) == (60, 100) and "soc_hires_percent" not in d
    d = PF.decode(msg(0x356, "6C 14 04 00 DC 00"))
    assert (d["voltage_v"], d["current_a"], d["temperature_c"]) == (52.28, 0.4, 22.0)
    d = PF.decode(msg(0x359, "00 00 00 00 01 50 4E"))
    assert d["protections"] == [] and d["alarms"] == [] and d["module_count"] == 1
    assert d["marker"] == "PN" and "note" not in d
    d = PF.decode(msg(0x35C, "C0 00"))
    assert d["charge_enable"] and d["discharge_enable"] and not d["force_charge_1"]
    d = PF.decode(msg(0x35E, "50 59 4C 4F 4E 20 20 20"))
    assert d["brand"] == "PYLON" and d["brand_padded"] == "50594C4F4E202020"
    # inverter side
    assert PF.decode(msg(0x305, "00 00 00 00 00 00 00 00"))["flow"] == "INV->BMS"
    assert "0x21" in PF.decode(msg(0x305, "21 00 00 00 00 00 00 00"))["note"]
    d = PF.decode(msg(0x307, "12 34 56 78 56 49 43 00"))
    assert d["victron"] and d["ident_ascii"] == "VIC"
    assert PF.decode(msg(0x307, "00 00 00 00 00 00 00 00"))["all_zero"]
    assert PF.decode(msg(0x7FF, "00")) is None
    assert PF.decode(C.CanMsg(0.0, 0x108040FE, bytes(8), ext=True)) is None   # Huawei, not ours


def test_decode_alarm_bits_and_pairs():
    d = PF.decode(msg(0x359, "02 08 02 08 02 50 4E 03"))
    assert d["protections"] == ["cell_or_module_over_voltage", "system_error"]
    assert d["alarms"] == ["cell_or_module_high_voltage", "internal_communication_fail"]
    assert d["module_count"] == 2 and d["online_bitmap(seplos)"] == "0x03"
    # 0x35A: AA.. = all OK (dfch), byte0 0x04 = high-voltage alarm, 0x10 = low voltage
    d = PF.decode(msg(0x35A, "AA AA AA 02 AA AA AA 06"))
    assert d["alarms"] == [] and d["warnings"] == [] and d["system_online"] is True
    d = PF.decode(msg(0x35A, "A4 AA AA 02 AA AA AA 0A"))
    assert d["alarms"] == ["high_voltage"] and d["system_online"] is False
    d = PF.decode(msg(0x35A, "9A AA AA 02 A6 AA AA 0A"))
    assert d["alarms"] == ["low_voltage"] and d["warnings"] == ["high_voltage"]
    # unsupported pairs (00) are listed, not reported as alarms
    d = PF.decode(msg(0x35A, "A8 AA AA 02 AA AA AA 0A"))
    assert "general" in d["not_supported"] and d["alarms"] == []


def test_decode_extensions_and_disputes():
    d = PF.decode(msg(0x373, "E8 0C 10 0E 23 01 26 01"))
    assert (d["min_cell_mv"], d["max_cell_mv"]) == (3304, 3600)
    assert d["min_temp_k"] == 291 and d["min_temp_c"] == 17.9 and d["max_temp_c"] == 20.9
    d = PF.decode(msg(0x379, "12 01 00 00 00 00 00 00"))
    assert d["installed_ah_u16"] == 274 and d["installed_ah_u32"] == 274
    d = PF.decode(msg(0x35F, "4C 69 01 17 69 00 00 00"))      # dfch BYD example
    assert d["model_u16"] == 0x694C and d["firmware_be"] == 0x0117 and d["online_capacity_ah"] == 105
    d = PF.decode(msg(0x372, "02 00 00 00 01 00 FF FF"))
    assert d["modules_ok"] == 2 and d["modules_blocking_discharge"] == 1 and d["modules_offline"] == 0xFFFF
    assert PF.decode(msg(0x374, "30 31 30 35 00 00 00 00"))["text"] == "0105"
    d = PF.decode(msg(0x378, "10 27 00 00 E8 03 00 00"))
    assert d["energy_in_kwh"] == 1000.0 and d["energy_out_kwh"] == 100.0
    # LV-HUB: 0x4355 = string 4 SOC frame (29-bit on the wire)
    d = PF.decode(msg(0x4355, "3C 00 64 00", ext=True))
    assert d["id"] == "0x355" and d["string"] == 4 and d["soc_percent"] == 60
    # 11-bit id sent as 29-bit (Dyness Junior Box claim) is flagged, still decoded
    d = PF.decode(msg(0x351, "28 02 F4 01 20 03 E0 01", ext=True))
    assert d["ext"] is True and d["cvl_v"] == 55.2 and "string" not in d
    # Dyness native
    d = PF.decode(msg(0x18BB0001, "6C 14 F6 FF 3C 00 64 00", ext=True))
    assert d["proto"] == "dyness-native" and d["voltage_v"] == 52.28 and d["current_a"] == -1.0
    assert d["soc_percent"] == 60
    d = PF.decode(msg(0x18AA01A1, "E8 0C E9 0C EA 0C EB 0C", ext=True))
    assert d["cells_mv"] == [3304, 3305, 3306, 3307]
    assert decode_any(msg(0x18BB0002, "00 00 00 00 F4 01 20 03", ext=True))["ccl_a_if_0.1"] == 50.0


def test_encode_roundtrip_and_dlcs():
    f = PF.encode_limits(55.2, 50.0, 80.0, 48.0)
    assert f.data.hex(" ").upper() == "28 02 F4 01 20 03 E0 01"
    assert PF.encode_limits(55.2, 50.0, 80.0, dlc=6).dlc == 6
    assert PF.encode_soc(60, 100).data.hex().upper() == "3C006400"
    assert PF.encode_analog(52.28, 0.4, 22.0).data.hex(" ").upper() == "6C 14 04 00 DC 00"
    f = PF.encode_alarms(module_count=1)
    assert f.data.hex(" ").upper() == "00 00 00 00 01 50 4E"
    f = PF.encode_alarms(["cell_or_module_over_voltage"], ["charge_high_current"], 3, dlc=8)
    d = PF.decode(f)
    assert d["protections"] == ["cell_or_module_over_voltage"] and d["alarms"] == ["charge_high_current"]
    assert PF.encode_requests().data == b"\xC0\x00"
    assert PF.decode(PF.encode_requests(charge_enable=False, force_charge_1=True))["force_charge_1"]
    assert PF.encode_brand("PYLON").data == b"PYLON   "
    assert PF.encode_brand("DYNESS-L").data == b"DYNESS-L"
    d = PF.decode(PF.encode_pairs(alarms=["low_voltage"], warnings=["high_voltage"]))
    assert d["alarms"] == ["low_voltage"] and d["warnings"] == ["high_voltage"] and d["system_online"]
    assert PF.decode(PF.encode_pairs())["alarms"] == []
    d = PF.decode(PF.encode_cell_extremes(3304, 3600, 17.9, 20.9))
    assert d["min_cell_mv"] == 3304 and d["max_temp_c"] == 20.9
    assert PF.decode(PF.encode_installed(274, "u32"))["installed_ah_u32"] == 274
    assert PF.decode(PF.encode_installed(274, "u16"))["installed_ah_u16"] == 274
    assert PF.encode_inverter_ident(True).data == PF.VIC_IDENT
    # LV-HUB string offset makes the id extended
    f = PF.encode_soc(60, 100, string=3)
    assert f.ext and f.can_id == 0x3355 and PF.decode(f)["string"] == 3
    nat = PF.encode_dyness_native(52.28, -1.0, 60, 100, [3304, 3305, 3306, 3307, 3308], 50, 80, 22, 20)
    assert [m.id_str for m in nat][:2] == ["0x18BB0001", "0x18BB0002"]
    assert PF.decode(nat[4])["cells_mv"] == [3304, 3305, 3306, 3307]


def test_guess_dialect():
    pyl = {0x351, 0x355, 0x356, 0x359, 0x35C, 0x35E}
    assert guess_dialect(pyl, "PYLON")[0]["name"] == "pylontech-lv"
    assert guess_dialect(pyl, "DYNESS-L", native_seen=True)[0]["name"] == "dyness-l"
    assert guess_dialect(pyl, "DYNESS-L", ext_seen=True, native_seen=True)[0]["name"] == "dyness-l-ext"
    assert guess_dialect({0x351, 0x355, 0x356, 0x35A, 0x35E, 0x35F, 0x379}, "HARNESS")[0]["name"] == "victron"
    assert guess_dialect(pyl | {0x379}, "PYLON")[0]["name"] in ("luxpower", "pylontech-lv")
    r = guess_dialect({0x351, 0x355}, None)
    assert any("missing" in x for x in r[0]["reasons"])


def test_extract_can_and_livestate():
    cfg = TimelineConfig()
    assert _extract_can(PF.decode(msg(0x351, "28 02 F4 01 20 03 E0 01")), cfg) == \
        {"cvl": 55.2, "ccl": 50.0, "dcl": 80.0}
    e = _extract_can(PF.decode(msg(0x356, "6C 14 04 00 DC 00")), cfg)
    assert e == {"pack_v": 52.28, "temp": 22.0, "pack_i": 0.4}
    e = _extract_can(PF.decode(msg(0x359, "00 00 02 00 01 50 4E")), cfg)
    assert e["ov_warn"] is True and e["alarms"] == "cell_or_module_high_voltage"
    e = _extract_can(PF.decode(msg(0x35C, "A0 00")), cfg)
    assert e == {"charge_enable": True, "discharge_enable": False, "charge_immediately": True,
                 "full_charge_request": False}
    from rs485harness.protocols.huawei_r48xx import regs as HR
    e = _extract_can(HR.decode(C.CanMsg(0.0, 0x1081407F, bytes.fromhex("017500000000D600"), ext=True)), cfg)
    assert e == {"psu_v": 53.5}
    live = LiveState(heartbeat_s=1000)
    live.feed_can(PF.decode(msg(0x351, "28 02 F4 01 20 03 E0 01")), 0.0)
    out = live.feed_can(PF.decode(msg(0x351, "28 02 90 01 20 03 E0 01")), 1.0)
    assert out and out[0].startswith("CHANGE ccl 50.0->40.0")


# -------------------------------------------------------- sims + analyzers
@pytest.fixture
def lab(tmp_path):
    name = f"pylon-{int(time.time() * 1000)}"
    bus = C.VirtualCanBus.get(name)
    sess = SessionWriter(tmp_path / "lab.jsonl")
    yield name, bus, sess
    sess.close()


def start_battery(bus, sess, profile="pylontech-lv", period=0.1, **state_over):
    state = preset("ideal")
    for k, v in state_over.items():
        setattr(state, k, v)
    prof = CAN_BATTERY_PROFILES[profile]
    from dataclasses import replace
    prof = replace(prof, period_s=period, extension_period_s=period * 2)
    sim = CanBatterySimulator(bus.end("bat", prof.bitrate), state, prof, sess, chan="bat")
    sim.start()
    return sim, state


def test_identify_pylontech_battery_alone_unlocks_extensions(lab):
    name, bus, sess = lab
    sim, state = start_battery(bus, sess)
    try:
        tap = bus.end("tap", 500_000, listen_only=True)
        rep = identify_can(tap, sess, "id", listen_s=0.5, bitrates=(125_000, 500_000),
                           unlock_s=0.5)
        assert rep["facts"]["bitrate"] == 500_000
        assert rep["facts"]["brand"] == "PYLON" and rep["facts"]["dialect"] == "pylontech-lv"
        assert rep["facts"]["inverter_keepalive"] is False
        assert any(v.startswith("PYLONTECH-CAN BATTERY @ 500k") for v in rep["verdicts"])
        assert any(v.startswith("UNLOCK: extension frames appeared") for v in rep["verdicts"]), rep["verdicts"]
        assert "0x373" in rep["facts"]["extension_ids"] and "0x379" in rep["facts"]["extension_ids"]
        assert sim.unlocked and sim.stats["keepalives"] >= 1
        assert "125k" in rep["passive"] and rep["passive"]["125k"]["frames"] == 0
        assert rep["passive"]["125k"]["error_frames"] > 0          # wrong rate -> error frames
        assert tap.listen_only
    finally:
        sim.stop()


def test_identify_stays_passive_when_inverter_present(lab):
    name, bus, sess = lab
    sim, state = start_battery(bus, sess)
    inv_prof = CAN_INVERTER_PROFILES["pylon-inverter"]
    from dataclasses import replace
    inv = CanInverterSimulator(bus.end("inv", 500_000), replace(inv_prof, period_s=0.1), sess)
    inv.start()
    try:
        tap = bus.end("tap", 500_000, listen_only=True)
        rep = identify(tap, sess, "id", listen_s=0.5)          # generic identify routes to CAN
        assert rep["bus"] == "can" and rep["facts"]["inverter_keepalive"] is True
        assert any("SKIPPED" in v and "inverter/GX" in v for v in rep["verdicts"])
        assert not sim.unlocked                                  # plain 0x305 does not unlock
        assert not rep["active"]
    finally:
        inv.stop(); sim.stop()


def test_victron_gx_unlock_and_loss_rule(lab):
    name, bus, sess = lab
    sim, state = start_battery(bus, sess)
    from dataclasses import replace
    gx = CanInverterSimulator(bus.end("gx", 500_000),
                              replace(CAN_INVERTER_PROFILES["victron-gx"], period_s=0.1,
                                      stop_without_351_s=0.4), sess)
    gx.start()
    try:
        time.sleep(0.6)
        assert sim.unlocked and sim.stats["idents"] >= 2
        assert gx.snapshot.get("cvl_v") == 56.0 and gx.charging_allowed
        sim.stop()                                               # battery goes silent
        time.sleep(0.8)
        assert gx.charging_allowed is False and gx.stats["loss_events"] == 1
    finally:
        gx.stop(); sim.stop()


@pytest.mark.parametrize("profile", ["pylontech-lv", "victron", "dyness-l", "dyness-l-ext",
                                     "luxpower", "seplos-can"])
def test_discover_and_dump_per_dialect(lab, profile):
    name, bus, sess = lab
    sim, state = start_battery(bus, sess, profile, soc_percent=42, current_a=-12.5)
    try:
        tap = bus.end("tap", 500_000, listen_only=True)
        rep = discover_can(tap, sess, listen_s=0.5, unlock_s=0.5)
        assert rep["result"] == "ok", rep
        assert rep["dialect"] == profile, (profile, rep["dialects"][:2])
        snap = rep["snapshot"]
        assert snap["soc_percent"] == 42 and snap["current_a"] == -12.5
        assert snap["cvl_v"] == 56.0 and snap["ccl_a"] == 100.0
        assert rep["brand"] == CAN_BATTERY_PROFILES[profile].brand
        assert rep["unlock"]["acked"] >= 1
        text = format_discovery_can(rep)
        assert "dialect ranking" in text and profile in text
        if profile.startswith("dyness"):
            assert snap["cells_v"][0] == 3.35 and "dyness_status" not in snap
            if profile == "dyness-l-ext":
                assert any("29-bit" in q for q in rep["quirks"])
        if profile == "victron":
            assert snap["installed_ah_u16"] == 100 and "protections" in snap
        if profile in ("pylontech-lv", "victron"):
            assert snap["max_cell_v"] == 3.35
        d = dump_can(tap, sess, listen_s=0.3, unlock_s=0.3)
        assert d["result"] == "ok" and d["snapshot"]["soc_percent"] == 42
        assert "soc_percent" in format_dump_can(d)
    finally:
        sim.stop()


def test_discover_passive_when_inverter_present_and_soc_table(lab):
    name, bus, sess = lab
    sim, state = start_battery(bus, sess)
    from dataclasses import replace
    inv = CanInverterSimulator(bus.end("inv", 500_000),
                               replace(CAN_INVERTER_PROFILES["pylon-inverter"], period_s=0.1), sess)
    inv.start()
    try:
        tap = bus.end("tap", 500_000, listen_only=True)
        rep = discover_can(tap, sess, listen_s=0.4)
        assert rep["inverter_present"] and rep["unlock"] == {"skipped": "inverter present"}
        assert "0x305" in rep["inverter_ids"]
        soc = soc_can(tap, sess, listen_s=0.3)
        assert soc["rows"][0]["soc_percent"] == 75 and soc["rows"][0]["string"] == 0
    finally:
        inv.stop(); sim.stop()


def test_lvhub_strings_in_soc_table(lab):
    name, bus, sess = lab
    sim1, _ = start_battery(bus, sess, soc_percent=50)
    sim1.string = 1
    sim2, _ = start_battery(bus, sess, soc_percent=70)
    sim2.string = 2
    try:
        tap = bus.end("tap", 500_000, listen_only=True)
        soc = soc_can(tap, sess, listen_s=0.4)
        rows = {r["string"]: r for r in soc["rows"]}
        assert rows[1]["soc_percent"] == 50 and rows[2]["soc_percent"] == 70
        rep = identify_can(tap, sess, listen_s=0.3, bitrates=(500_000,), active=False)
        assert any(v.startswith("LV-HUB strings") for v in rep["verdicts"])
    finally:
        sim1.stop(); sim2.stop()


def test_sniff_and_timeline_over_can(lab, tmp_path):
    name, bus, sess = lab
    sim, state = start_battery(bus, sess)
    from dataclasses import replace
    inv = CanInverterSimulator(bus.end("inv", 500_000),
                               replace(CAN_INVERTER_PROFILES["pylon-inverter"], period_s=0.1), sess)
    inv.start()
    cap = SessionWriter(tmp_path / "cap.jsonl")
    lines = []
    try:
        tap = bus.end("tap", 500_000, listen_only=True)
        sn = Sniffer({"ch1": tap}, cap, on_event=lines.append, markers=False, quiet=True)
        import threading
        t = threading.Thread(target=sn.run, kwargs={"duration": 0.8}, daemon=True)
        t.start()
        time.sleep(0.3)
        state.charge_current_limit_da = 400          # CCL 100 -> 40 A while sniffing
        t.join(timeout=3)
    finally:
        inv.stop(); sim.stop(); cap.close()
    recs = list(read_session(tmp_path / "cap.jsonl"))
    can = [r for r in recs if r.get("kind") == "can"]
    assert can and {r["flow"] for r in can} >= {"BMS->INV", "INV->BMS"}
    assert any("CHANGE ccl 100.0->40.0" in l for l in lines), lines
    tl = build_timeline(tmp_path / "cap.jsonl")
    assert any(e["kind"] == "ccl-change" and e["old"] == 100.0 and e["new"] == 40.0
               for e in tl["events"])
    assert tl["series"] and tl["series"][-1].get("ccl") == 40.0 and tl["series"][-1]["can_id"]


def test_rsh_cli_can_battery_commands(lab, tmp_path, capsys):
    from rs485harness.cli import main
    name, bus, sess = lab
    sim, state = start_battery(bus, sess, "dyness-l")
    try:
        port = f"can:virtual:{name}"
        assert main(["discover", "--port", port, "--listen", "0.4", "--passive-only", "--json",
                     "--session", str(tmp_path / "d.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["dialect"] == "dyness-l" and out["brand"] == "DYNESS-L"
        assert main(["soc", "--port", port, "--timeout", "0.3", "--json",
                     "--session", str(tmp_path / "s.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["rows"][0]["soc_percent"] == 75
        assert main(["dump", "--port", port, "--listen", "0.3", "--passive-only", "--json",
                     "--session", str(tmp_path / "u.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["snapshot"]["cells_v"][0] == 3.35
        assert sim.stats["keepalives"] == 0      # both runs passive: nothing sent
    finally:
        sim.stop()


def test_rsh_cli_can_sims_run(tmp_path):
    from rs485harness.cli import main
    name = f"sims-{int(time.time() * 1000)}"
    port = f"can:virtual:{name}"
    import threading
    t = threading.Thread(target=main, args=(["battery-sim", "--port", port, "--can-profile",
                                             "victron", "--duration", "2.5", "--json",
                                             "--session", str(tmp_path / "b.jsonl")],),
                         daemon=True)
    t.start()
    time.sleep(0.1)
    rc = main(["inverter-sim", "--port", port, "--can-profile", "victron-gx", "--duration",
               "2.2", "--json", "--session", str(tmp_path / "i.jsonl")])
    t.join(timeout=3)
    assert rc == 0
    recs = list(read_session(tmp_path / "i.jsonl"))
    assert any(r.get("kind") == "can" and r.get("id") == "0x35A" for r in recs)
    assert any(r.get("kind") == "can" and r.get("id") == "0x307" and r["dir"] == "tx" for r in recs)
