"""Declarative dialect profiles: per-device deviations of the Pylontech
RS485 protocol, expressed as data so that simulators, the translator and
the diagnoser all share them.

Background (from field reports / reference code):
- Response address: Fahmula's emulator (tested vs Growatt/Deye/SRNE)
  hardcodes ADR "02" in responses; other firmwares answer with ADR 01 or
  echo the request's ADR. Inverters differ in what they accept.
- Response CID2 position: per spec it carries the RTN return code ("4600"),
  but several implementations (and possibly some BMS firmwares) echo the
  command ("4661") - Battery-Emulator shipped exactly that bug.
- Command family: monitoring 4x vs inverter 6x; some inverters fall back to
  the other family when they get RTN=04.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class DialectProfile:
    name: str
    description: str = ""
    ver: int = 0x20                     # protocol version byte in responses
    accept_vers: tuple = (0x20, 0x25, 0x21, 0x22, 0x26)  # request VERs answered
    response_adr: str = "fixed02"       # fixed02 | fixed01 | echo
    response_cid2: str = "rtn"          # rtn (correct) | echo (buggy firmwares)
    families: tuple = ("4x", "6x")      # request families answered
    listen_adrs: tuple | None = None    # None = answer any ADR; else whitelist
    unknown_cmd: str = "rtn04"          # rtn04 | silence
    response_delay_s: float = 0.02      # simulated BMS processing delay
    baud: int = 9600

    def response_adr_for(self, request_adr: int) -> int:
        if self.response_adr == "fixed02":
            return 0x02
        if self.response_adr == "fixed01":
            return 0x01
        return request_adr

    def to_dict(self) -> dict:
        return asdict(self)


BATTERY_PROFILES: dict[str, DialectProfile] = {
    "ideal-pylontech": DialectProfile(
        name="ideal-pylontech",
        description="Ideal Pylontech US-series pack: both families, RTN "
                    "semantics, responds on ADR 02 (Fahmula-compatible, "
                    "proven against Growatt/Deye/SRNE).",
    ),
    "pylontech-echo-adr": DialectProfile(
        name="pylontech-echo-adr",
        description="Same but echoes the request address in responses.",
        response_adr="echo",
    ),
    "pylontech-6x-only": DialectProfile(
        name="pylontech-6x-only",
        description="Answers only the system-level 0x60-0x63 family "
                    "(minimal Voltronic-style BMS).",
        families=("6x",),
    ),
    "pylontech-4x-only": DialectProfile(
        name="pylontech-4x-only",
        description="Answers only the monitoring 0x42-family (old packs / "
                    "PC-software oriented firmwares).",
        families=("4x",),
    ),
    "buggy-cid2-echo": DialectProfile(
        name="buggy-cid2-echo",
        description="Echoes CID2 in the response header instead of RTN "
                    "(reproduces the Battery-Emulator bug, for testing the "
                    "diagnoser).",
        response_cid2="echo",
    ),
    "silent-on-unknown": DialectProfile(
        name="silent-on-unknown",
        description="Stays silent on unsupported commands instead of "
                    "replying RTN=04.",
        unknown_cmd="silence",
    ),
    "slow-responder": DialectProfile(
        name="slow-responder",
        description="Responds correctly but slowly (500 ms) - for testing "
                    "inverter timeout behaviour.",
        response_delay_s=0.5,
    ),
}


@dataclass
class InverterPollProfile:
    """What a simulated inverter sends."""
    name: str
    description: str = ""
    ver: int = 0x20
    adr: int = 0x01
    cid1: int = 0x46
    commands: tuple = (0x61, 0x62, 0x63)   # poll cycle
    info_for: dict = field(default_factory=dict)  # cid2 -> INFO string
    period_s: float = 1.0
    response_timeout_s: float = 1.0
    baud: int = 9600

    def to_dict(self) -> dict:
        return asdict(self)


INVERTER_PROFILES: dict[str, InverterPollProfile] = {
    "voltronic-6x": InverterPollProfile(
        name="voltronic-6x",
        description="Voltronic/Axpert-clone style: polls 0x61/0x62/0x63 on "
                    "ADR 01 @ 9600 every second (what SAKO 'PYL' most "
                    "likely does).",
    ),
    "voltronic-6x-adr2": InverterPollProfile(
        name="voltronic-6x-adr2",
        description="Same poll cycle but on ADR 02 (master-pack convention).",
        adr=0x02,
    ),
    "monitoring-4x": InverterPollProfile(
        name="monitoring-4x",
        description="Monitoring-software style: 0x42(FF)/0x51/0x47/0x92 on "
                    "ADR 02 (python-pylontech convention).",
        adr=0x02,
        commands=(0x42, 0x51, 0x47, 0x92),
        info_for={0x42: "FF", 0x47: "", 0x92: "02"},
        period_s=2.0,
    ),
}
