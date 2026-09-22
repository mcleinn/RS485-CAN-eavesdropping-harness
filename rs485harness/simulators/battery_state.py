"""Battery state model feeding the battery simulator's responses."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class BatteryState:
    # identity
    device_name: str = "US5000"
    manufacturer: str = "Pylon"
    serial_number: str = "HARNESS000000001"  # exactly 16 chars (0x93 field)
    software_version: tuple = (1, 5)

    # pack analog values
    cell_mv: list[int] = field(default_factory=lambda: [3350] * 16)
    current_a: float = 0.0          # + charge / - discharge
    soc_percent: int = 75
    soh_percent: int = 100
    cycles: int = 120
    temperature_c: float = 25.0
    max_temp_c: float = 26.0
    min_temp_c: float = 24.0
    mosfet_temp_c: float = 28.0
    bms_temp_c: float = 27.0
    group_temps_c: list[float] = field(default_factory=lambda: [25.0, 25.0, 25.0, 25.0])
    total_mah: int = 314000         # 314 Ah (Seplos Mason 280L class)
    remaining_mah: int = 235000

    # charge/discharge management
    charge_voltage_limit_mv: int = 56000     # 3.5 V/cell * 16
    discharge_voltage_min_mv: int = 46000
    charge_current_limit_da: int = 1000      # 100.0 A
    discharge_current_limit_da: int = 1000
    charge_enable: bool = True
    discharge_enable: bool = True
    force_charge: bool = False

    # fixed system parameters (0x47)
    cell_high_limit_mv: int = 3650
    cell_low_limit_mv: int = 2900
    cell_under_limit_mv: int = 2500
    charge_high_temp_c: float = 55.0
    charge_low_temp_c: float = 0.0
    module_high_limit_mv: int = 58400
    module_low_limit_mv: int = 46400
    module_under_limit_mv: int = 40000
    discharge_high_temp_c: float = 60.0
    discharge_low_temp_c: float = -20.0

    # alarms/protections for 0x62 (all False = healthy)
    alarms: dict = field(default_factory=dict)

    # optional time-varying behaviour
    drift: bool = False
    _t0: float = field(default_factory=time.monotonic)

    @property
    def voltage_mv(self) -> int:
        return sum(self.cell_mv)

    @property
    def max_cell_mv(self) -> int:
        return max(self.cell_mv)

    @property
    def min_cell_mv(self) -> int:
        return min(self.cell_mv)

    @property
    def current_ca(self) -> int:
        return int(round(self.current_a * 100))

    @property
    def current_da(self) -> int:
        return int(round(self.current_a * 10))

    def tick(self) -> None:
        """Optional gentle sine charge/discharge drift so live views move."""
        if not self.drift:
            return
        t = time.monotonic() - self._t0
        self.current_a = round(20.0 * math.sin(t / 60.0), 1)
        base = 3350 + int(10 * math.sin(t / 120.0))
        self.cell_mv = [base + (i % 5) for i in range(len(self.cell_mv))]


def apply_overrides(state: BatteryState, overrides: dict[str, str]) -> BatteryState:
    """Set state fields from 'key=value' strings, coercing to the field's
    type. Lists accept comma-separated values (e.g. cell_mv=3350,3351,...).
    Also accepts convenience volts/amps: 'voltage_v=51.2' spreads over cells,
    'soc=75' aliases soc_percent."""
    alias = {"soc": "soc_percent", "current": "current_a", "soh": "soh_percent"}
    for key, val in overrides.items():
        key = alias.get(key, key)
        if key == "voltage_v":
            per_cell = int(round(float(val) * 1000 / len(state.cell_mv)))
            state.cell_mv = [per_cell] * len(state.cell_mv)
            continue
        if not hasattr(state, key):
            raise KeyError(f"BatteryState has no field '{key}'")
        cur = getattr(state, key)
        if isinstance(cur, bool):
            setattr(state, key, val.strip().lower() in ("1", "true", "yes", "on"))
        elif isinstance(cur, int):
            setattr(state, key, int(float(val)))
        elif isinstance(cur, float):
            setattr(state, key, float(val))
        elif isinstance(cur, list):
            items = [v.strip() for v in val.split(",")]
            conv = type(cur[0]) if cur else float
            setattr(state, key, [conv(float(v)) if conv in (int, float) else v
                                 for v in items])
        else:
            setattr(state, key, val)
    return state


def parse_overrides(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--set expects key=value, got '{pair}'")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def preset(name: str) -> BatteryState:
    if name in ("ideal", "pylontech-us5000"):
        return BatteryState()
    if name == "seplos-280l":
        return BatteryState(
            device_name="MASON280L", manufacturer="SEPLOS",
            serial_number="SEPLOS280L300001",
            total_mah=314000, remaining_mah=235000,
            charge_voltage_limit_mv=56800, discharge_voltage_min_mv=46400,
            charge_current_limit_da=2000, discharge_current_limit_da=2000,
        )
    raise KeyError(f"unknown battery preset '{name}' (have: ideal, seplos-280l)")


PRESETS = ("ideal", "seplos-280l")
