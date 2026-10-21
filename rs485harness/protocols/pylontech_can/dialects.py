"""Declarative CAN dialect profiles (same idea as protocols/pylontech/
dialects.py for RS485): which frames a battery of each family sends, with
which DLCs, what it expects from the inverter, and what the harness may
conclude from what it hears. Facts per docs/refs/*/NOTES.md; anything not
established by a source is marked in the description as unverified.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .frames import EXTENSION_IDS


@dataclass
class CanDialectProfile:
    name: str
    description: str = ""
    bitrate: int = 500_000
    brand: str = "PYLON"                   # 0x35E text
    mandatory_ids: tuple = (0x351, 0x355, 0x356, 0x359, 0x35C, 0x35E)
    optional_ids: tuple = ()
    extension_ids: tuple = tuple(sorted(EXTENSION_IDS))
    extensions_need_unlock: bool = True     # only after inverter 0x305 + 0x307
    keepalive_expected: str | None = "0x305"   # what the inverter should send
    keepalive_loss_s: float | None = None   # battery-side consequence timeout
    inverter_loss_s: float | None = None    # inverter-side: stop when 0x351 missing
    period_s: float = 1.0
    extension_period_s: float = 2.0
    dlcs: dict = field(default_factory=lambda: {0x351: 8, 0x355: 4, 0x356: 6, 0x359: 7,
                                                0x35C: 2, 0x35E: 8})
    sends_35a: bool = False                 # always (not only unlocked)
    sends_359: bool = True
    capacity_frame: str | None = None       # "u32" | "u16" (0x379 width) | None
    native_frames: bool = False             # Dyness 0x18BB/0x18AA
    extended_ids: bool = False              # 11-bit set sent as 29-bit (Dyness JB, unverified)
    module_count: int = 1
    installed_ah: int = 100
    model_frame: bool = False               # 0x35F
    online_bitmap: bool = False             # Seplos: 0x359 byte 7 = online packs
    names: tuple = ()                       # 0x370/0x371 texts

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dlcs"] = {f"0x{k:03X}": v for k, v in self.dlcs.items()}
        return d


CAN_BATTERY_PROFILES: dict[str, CanDialectProfile] = {
    "pylontech-lv": CanDialectProfile(
        name="pylontech-lv",
        description="Pylontech US2000/US3000C/US5000 per spec V2.0 (2021) and real "
                    "captures: 0x351 [8] 0x355 [4] 0x356 [6] 0x359 [7] 0x35C [2] 0x35E "
                    "'PYLON   ' every 1 s; 0x35A/0x370-0x379 only after 0x305 + 0x307, "
                    "then every 2 s; 0x379 u32 Ah.",
        capacity_frame="u32",
    ),
    "victron": CanDialectProfile(
        name="victron",
        description="Battery built for Victron GX (REC, Batrium, diyBMS, BYD): 0x351/"
                    "0x355/0x356/0x35A always (0x35A is how the GX detects it), 0x35E/"
                    "0x35F, 0x370-0x379 unsolicited; GX sends 0x305 + 0x307 'VIC' every "
                    "1 s and stops charge/discharge when 0x351 is missing for 3 s; 500 k "
                    "('BMS-Can') or 250 k profile; 0x379 u16 Ah.",
        brand="HARNESS",
        mandatory_ids=(0x351, 0x355, 0x356, 0x35A),
        optional_ids=(0x35E, 0x35F, 0x359, 0x35C),
        extensions_need_unlock=False,
        keepalive_expected="0x305+0x307",
        inverter_loss_s=3.0,
        dlcs={0x351: 8, 0x355: 6, 0x356: 6, 0x359: 8, 0x35C: 2, 0x35E: 8},
        sends_35a=True,
        capacity_frame="u16",
        model_frame=True,
        names=("HARNESS ", "BATTERY "),
    ),
    "dyness-l": CanDialectProfile(
        name="dyness-l",
        description="Dyness DL5.0F / B-series: Pylontech set with 0x35E 'DYNESS-L' plus "
                    "native 0x18BB000x / 0x18AA01xx frames (cells, limits, temps); DIP #4 "
                    "OFF = 500 k, ON = 250 k. Whether its 0x35x frames are 29-bit on the "
                    "wire is unverified (a Junior Box capture says so) - profile sends "
                    "11-bit; use dyness-l-ext for the 29-bit variant.",
        brand="DYNESS-L",
        optional_ids=(0x35F,),
        native_frames=True,
        installed_ah=100,
    ),
    "dyness-l-ext": CanDialectProfile(
        name="dyness-l-ext",
        description="Dyness variant with the whole 0x35x set sent as 29-bit identifiers "
                    "(as declared by the grericht ESPHome decoder) - for testing the "
                    "receiver's tolerance; unverified on a real DL5.0F.",
        brand="DYNESS-L",
        native_frames=True,
        extended_ids=True,
    ),
    "luxpower": CanDialectProfile(
        name="luxpower",
        description="'Luxpower protocol' (SNA brand code 6) as reconstructed from open "
                    "converters: Pylontech set plus 0x379 capacity frame (bytes 0-1 Ah, "
                    "sent unsolicited); no public LUX spec exists, 500 k assumed.",
        brand="PYLON",
        optional_ids=(0x379,),
        extensions_need_unlock=False,
        extension_ids=(0x379,),
        capacity_frame="u16",
    ),
    "seplos-can": CanDialectProfile(
        name="seplos-can",
        description="Seplos CANBUS V1.0 ('compatible with Pylontech V1.3 and Goodwe V1.5'): "
                    "same IDs, byte 7 of 0x359 = online-pack bitmap, plus 0x370/0x371 cell "
                    "extremes (Seplos definition, not the Victron names).",
        brand="PYLON",
        optional_ids=(0x370, 0x371),
        extension_ids=(),
        dlcs={0x351: 8, 0x355: 4, 0x356: 6, 0x359: 8, 0x35C: 2, 0x35E: 8},
        online_bitmap=True,
    ),
}


@dataclass
class CanInverterProfile:
    """What a simulated inverter / GX sends on the bus."""
    name: str
    description: str = ""
    bitrate: int = 500_000
    keepalive: bool = True              # 0x305 all zero every period
    ident: str | None = None            # None | "zero" | "vic"  (0x307)
    heartbeat: bool = False             # Pylontech 0x1001 (5-min shutdown timer)
    period_s: float = 1.0
    stop_without_351_s: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


CAN_INVERTER_PROFILES: dict[str, CanInverterProfile] = {
    "pylon-inverter": CanInverterProfile(
        name="pylon-inverter", description="Generic inverter on the Pylontech CAN profile: "
                                           "0x305 00..00 every second, nothing else."),
    "victron-gx": CanInverterProfile(
        name="victron-gx", description="Victron GX: 0x305 + 0x307 '12 34 56 78 VIC 00' every "
                                       "second; drops charge/discharge without 0x351 for 3 s.",
        ident="vic", stop_without_351_s=3.0),
    "pylon-unlock": CanInverterProfile(
        name="pylon-unlock", description="Inverter that sends 0x305 + 0x307 all-zero to unlock "
                                         "the Pylontech extension frames.",
        ident="zero"),
    "silent-inverter": CanInverterProfile(
        name="silent-inverter", description="Listens only (no 0x305): older Pylontech packs "
                                            "then slow their cadence to 1-10 s.",
        keepalive=False),
}


def guess_dialect(ids_seen, brand: str | None, ext_seen: bool = False,
                  native_seen: bool = False, passive_ids=None,
                  bitmap_seen: bool = False) -> list[dict]:
    """Rank the profiles against what was heard. Returns a list of
    {name, score, reasons} sorted best first; the caller decides what to
    print. Pure function, unit-tested."""
    ids = set(ids_seen)
    passive = set(passive_ids) if passive_ids is not None else ids
    out = []
    b = (brand or "").upper()
    for name, p in CAN_BATTERY_PROFILES.items():
        score, reasons = 0, []
        have = ids & set(p.mandatory_ids)
        score += 2 * len(have)
        miss = set(p.mandatory_ids) - ids
        if miss:
            reasons.append("missing " + ",".join(f"0x{i:03X}" for i in sorted(miss)))
            score -= len(miss)
        if brand:
            if b.startswith(p.brand.upper()[:5]):
                score += 4
                reasons.append(f"brand {brand!r} matches")
            else:
                score -= 3
                reasons.append(f"brand {brand!r} is not {p.brand!r}")
        if native_seen:
            if p.native_frames:
                score += 4
                reasons.append("Dyness native 0x18BB/0x18AA frames present")
            else:
                score -= 2
        if ext_seen and p.extended_ids:
            score += 2
            reasons.append("29-bit 0x35x frames")
        if 0x35A in ids and p.sends_35a:
            score += 2
            reasons.append("0x35A without unlock")
        if 0x379 in ids and p.capacity_frame:
            score += 1
        # extension frames heard WITHOUT an unlock contradict profiles that
        # need the 0x305+0x307 trigger and confirm those that do not
        ext_passive = passive & (set(p.extension_ids) | set(p.optional_ids)) - set(p.mandatory_ids)
        if ext_passive:
            if p.extensions_need_unlock and ext_passive & EXTENSION_IDS:
                score -= 2
                reasons.append("extension ids unsolicited: " +
                               ",".join(f"0x{i:03X}" for i in sorted(ext_passive)))
            else:
                score += 2
                reasons.append("unsolicited " + ",".join(f"0x{i:03X}" for i in sorted(ext_passive)))
        elif not p.extensions_need_unlock and set(p.extension_ids) and passive:
            score -= 1
        unexpected = ids - set(p.mandatory_ids) - set(p.optional_ids) - set(p.extension_ids) - {0x305, 0x307, 0x1001}
        if unexpected:
            score -= len(unexpected)
            reasons.append("unexpected " + ",".join(f"0x{i:03X}" for i in sorted(unexpected)))
        if bitmap_seen:
            if p.online_bitmap:
                score += 3
                reasons.append("0x359 byte 7 online bitmap")
            else:
                score -= 1
        out.append({"name": name, "score": score, "reasons": reasons})
    out.sort(key=lambda e: -e["score"])
    return out
