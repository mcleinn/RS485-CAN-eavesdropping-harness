"""Payload encode/decode round-trips and scaling checks."""
from rs485harness.protocols.pylontech import payloads as P
from rs485harness.simulators.battery_state import BatteryState, preset, apply_overrides


def state():
    s = preset("ideal")
    s.current_a = -12.5
    s.soc_percent = 42
    return s


def test_deci_kelvin_roundtrip():
    # +-0.1 degC is the best possible at deci-Kelvin resolution w/ 273.15 offset
    assert abs(P.dk_to_c(P.c_to_dk(25.0)) - 25.0) <= 0.1
    assert P.c_to_dk(25.0) == 2981  # (25 + 273.15) * 10, C-style truncation
    assert abs(P.k10_to_c(P.c_to_k10(-10.0)) - -10.0) <= 0.1


def test_61_roundtrip():
    s = state()
    info = P.build_61(s)
    assert len(info) == 98  # 49 bytes
    d = P.decode_61(info)
    assert d["voltage_v"] == s.voltage_mv / 1000
    assert d["current_a"] == -12.5
    assert d["soc_percent"] == 42
    assert abs(d["avg_cell_temp_c"] - s.temperature_c) <= 0.1
    assert d["max_cell_module"] == "0101" and d["min_cell_module"] == "0108"


def test_60_roundtrip():
    s = state()
    d = P.decode_60(P.build_60(s))
    assert d["battery_name"] == "US5000"
    assert d["manufacturer"] == "Pylon"
    assert d["battery_count"] == 1
    assert d["barcodes"] == [s.serial_number]


def test_62_roundtrip():
    s = state()
    s.alarms = {"cell_voltage_low_alarm": True, "discharge_overcurrent_protect": True}
    d = P.decode_62(P.build_62(s))
    assert set(d["active"]) == {"cell_voltage_low_alarm",
                                "discharge_overcurrent_protect"}
    s.alarms = {}
    assert P.decode_62(P.build_62(s))["active"] == []


def test_63_roundtrip():
    s = state()
    d = P.decode_63(P.build_63(s))
    assert d["charge_voltage_limit_v"] == 56.0
    assert d["discharge_voltage_min_v"] == 46.0
    assert d["charge_current_limit_a"] == 100.0
    assert d["status"]["charge_enable"] is True
    assert d["status"]["charge_immediately"] is False
    s.force_charge = True
    assert P.decode_63(P.build_63(s))["status"]["charge_immediately"] is True


def test_42_roundtrip():
    s = state()
    d = P.decode_42(P.build_42_single(s))
    assert len(d["modules"]) == 1
    m = d["modules"][0]
    assert m["n_cells"] == 16
    assert m["cell_v"][0] == 3.35
    assert m["current_a"] == -12.5
    assert m["voltage_v"] == s.voltage_mv / 1000
    assert m["cycles"] == s.cycles


def test_47_roundtrip():
    s = state()
    d = P.decode_47(P.build_47(s))
    assert d["cell_high_limit_v"] == 3.65
    assert d["charge_high_temp_c"] == 55.0
    assert d["discharge_low_temp_c"] == -20.0


def test_51_roundtrip():
    s = state()
    d = P.decode_51(P.build_51(s))
    assert d["device_name"] == "US5000"
    assert d["manufacturer"] == "Pylon"
    assert d["software_version"] == [1, 5]


def test_92_93_roundtrip():
    s = state()
    d = P.decode_92(P.build_92(s))
    assert d["charge_voltage_limit_v"] == 56.0
    assert d["status"]["discharge_enable"] is True
    d = P.decode_93(P.build_93(s))
    assert d["serial_number"] == s.serial_number


def test_overrides():
    s = preset("seplos-280l")
    apply_overrides(s, {"soc": "20", "current_a": "-15",
                        "charge_enable": "false", "voltage_v": "51.2"})
    assert s.soc_percent == 20
    assert s.current_a == -15.0
    assert s.charge_enable is False
    assert abs(s.voltage_mv - 51200) < 16
