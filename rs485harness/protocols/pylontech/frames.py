"""Pylontech RS485 ASCII frame codec (YD/T 1363.1 frame family).

Wire format (everything except SOI/EOI is uppercase ASCII hex, big-endian):

    SOI  VER ADR CID1 CID2 LENGTH INFO CHKSUM  EOI
    '~'  2ch 2ch 2ch  2ch  4ch    n*ch 4ch     '\r'

LENGTH = LCHKSUM nibble (4 bit) + LENID (12 bit) where LENID counts the ASCII
characters of INFO. In *responses* the CID2 position carries the RTN return
code, not an echo of the command (the #1 mistake in third-party
implementations; some devices do echo the command there - see
dialects.DialectProfile.response_cid2).

Golden vector (checksums verified by hand and against reference code):
    7E 32 30 30 31 34 36 34 32 45 30 30 32 30 31 46 44 33 35 0D
    = ~2001 4642 E002 01 FD35\r  (VER 20, ADR 01, CID1 46, CID2 42, INFO "01")
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field

SOI = 0x7E  # '~'
EOI = 0x0D  # '\r'

CID1_LFP_BATTERY = 0x46

RTN_CODES = {
    0x00: "OK",
    0x01: "VER error",
    0x02: "CHKSUM error",
    0x03: "LCHKSUM error",
    0x04: "CID2 invalid (command not supported)",
    0x05: "command format error",
    0x06: "invalid data",
    0x90: "ADR error",
    0x91: "communication error",
}

# Request command codes.  "4x" family = per-pack / monitoring software;
# "6x" family = system level, what most inverters (Voltronic clones) poll.
CID2_NAMES = {
    0x42: "GetAnalogValue",
    0x44: "GetAlarmInfo",
    0x47: "GetSystemParameter",
    0x4F: "GetProtocolVersion",
    0x51: "GetManufacturerInfo",
    0x60: "GetSystemBasicInfo",
    0x61: "GetSystemAnalogData",
    0x62: "GetSystemAlarmInfo",
    0x63: "GetSystemChargeDischargeInfo",
    0x64: "SystemShutdown",
    0x92: "GetChargeDischargeManagement",
    0x93: "GetModuleSerialNumber",
    0x94: "SetChargeDischargeManagement",
    0x96: "TurnOff",
    0xC1: "SetSystemTimeParams",
    0xC2: "GetSystemTimeParams",
}

FAMILY_4X = frozenset({0x42, 0x44, 0x47, 0x4F, 0x51, 0x92, 0x93, 0x94, 0x96, 0xC1, 0xC2})
FAMILY_6X = frozenset({0x60, 0x61, 0x62, 0x63, 0x64})

_HEX = set(string.hexdigits.encode())


def cid2_family(cid2: int) -> str:
    if cid2 in FAMILY_6X:
        return "6x"
    if cid2 in FAMILY_4X:
        return "4x"
    return "?"


def lchksum(lenid: int) -> int:
    """4-bit checksum over the three nibbles of LENID."""
    s = ((lenid >> 8) & 0xF) + ((lenid >> 4) & 0xF) + (lenid & 0xF)
    return (((~(s % 16)) & 0xF) + 1) & 0xF


def length_field(info_len: int) -> str:
    """4 hex chars: LCHKSUM nibble + 12-bit LENID (= ASCII char count of INFO).

    Note: a zero-length INFO is encoded as "0000" (matches real devices and
    python-pylontech, which special-cases lenid == 0).
    """
    if info_len == 0:
        return "0000"
    return f"{lchksum(info_len):01X}{info_len:03X}"


def chksum(data: bytes) -> int:
    """Two's-complement 16-bit checksum over the ASCII chars VER..INFO."""
    return (~sum(data) + 1) & 0xFFFF


def encode_frame(ver: int, adr: int, cid1: int, cid2: int, info: str = "") -> bytes:
    """Build a complete wire frame. `info` is the ASCII-hex INFO string."""
    body = f"{ver:02X}{adr:02X}{cid1:02X}{cid2:02X}{length_field(len(info))}{info}"
    b = body.encode("ascii")
    return b"~" + b + f"{chksum(b):04X}".encode("ascii") + b"\r"


@dataclass
class ParsedFrame:
    """Tolerantly parsed frame: fields are filled as far as parsing got,
    `errors` collects every deviation found (empty list = clean frame)."""

    raw: bytes
    ver: int | None = None
    adr: int | None = None
    cid1: int | None = None
    cid2: int | None = None  # command code (request) or RTN (response)
    lenid: int | None = None
    info: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def info_bytes(self) -> bytes:
        try:
            return bytes.fromhex(self.info)
        except ValueError:
            return b""

    def describe(self, role: str = "request") -> str:
        """One-line human summary. role: 'request' | 'response' | 'auto'."""
        if self.ver is None:
            return f"unparseable ({self.errors[0] if self.errors else 'empty'})"
        if role == "response":
            c2 = f"RTN={self.cid2:02X}({RTN_CODES.get(self.cid2, '?')})"
        else:
            c2 = f"CID2={self.cid2:02X}({CID2_NAMES.get(self.cid2, '?')})"
        e = f" ERRORS:{';'.join(self.errors)}" if self.errors else ""
        return (f"VER={self.ver:02X} ADR={self.adr:02X} CID1={self.cid1:02X} "
                f"{c2} INFO[{len(self.info) // 2}B]={self.info}{e}")

    def to_dict(self) -> dict:
        return {
            "ver": self.ver, "adr": self.adr, "cid1": self.cid1, "cid2": self.cid2,
            "lenid": self.lenid, "info": self.info, "errors": self.errors,
        }


def looks_like_pylontech(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == SOI


def parse_frame(raw: bytes) -> ParsedFrame:
    """Tolerant parser: never raises; records every deviation in .errors."""
    pf = ParsedFrame(raw=raw)
    data = raw
    if not data:
        pf.errors.append("empty")
        return pf
    if data[0] == SOI:
        data = data[1:]
    else:
        pf.errors.append("missing-SOI")
    if data and data[-1] == EOI:
        data = data[:-1]
    else:
        pf.errors.append("missing-EOI")

    if len(data) < 16:  # VER+ADR+CID1+CID2+LENGTH+CHKSUM = 16 chars minimum
        pf.errors.append(f"too-short ({len(data)} chars, need >=16)")
        return pf
    if any(c not in _HEX for c in data):
        bad = [c for c in data if c not in _HEX][:4]
        pf.errors.append(f"non-hex-chars {bad}")
        return pf

    body, chk_str = data[:-4], data[-4:]
    got_chk = int(chk_str, 16)
    want_chk = chksum(body)
    if got_chk != want_chk:
        pf.errors.append(f"chksum-mismatch got={got_chk:04X} expected={want_chk:04X}")

    s = body.decode("ascii")
    pf.ver = int(s[0:2], 16)
    pf.adr = int(s[2:4], 16)
    pf.cid1 = int(s[4:6], 16)
    pf.cid2 = int(s[6:8], 16)
    length = int(s[8:12], 16)
    pf.lenid = length & 0x0FFF
    got_lchk = (length >> 12) & 0xF
    pf.info = s[12:]

    if pf.lenid != len(pf.info):
        pf.errors.append(f"lenid-mismatch lenid={pf.lenid} actual-info-chars={len(pf.info)}")
    want_lchk = 0 if pf.lenid == 0 else lchksum(pf.lenid)
    if got_lchk != want_lchk:
        pf.errors.append(f"lchksum-mismatch got={got_lchk:X} expected={want_lchk:X}")
    if len(pf.info) % 2 != 0:
        pf.errors.append("info-odd-length")
    return pf
