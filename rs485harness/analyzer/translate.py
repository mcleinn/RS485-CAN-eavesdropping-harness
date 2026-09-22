"""Frame rewrite rules for bridge/translate mode.

A Translator takes a parsed Pylontech frame plus its direction and either
passes it through or re-emits a rewritten (re-checksummed) frame. Rules are
small and composable so a translation profile is readable in the session
log ("rewrote VER 25->20; forced response ADR 02").
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..protocols.pylontech import frames as F


@dataclass
class Translator:
    """direction-aware rewrite; 'a2b' = frames flowing chan A -> chan B."""
    force_ver: dict = field(default_factory=dict)        # dir -> ver int
    force_adr: dict = field(default_factory=dict)        # dir -> adr int
    fix_cid2_echo: set = field(default_factory=set)      # dirs where response
    #   headers that echo the request CID2 get rewritten to RTN=00
    recent_request_cid2: int | None = None

    def apply(self, pf: F.ParsedFrame, direction: str) -> tuple[bytes, list[str]]:
        """Returns (frame bytes to forward, list of rewrites applied)."""
        if pf.ver is None:  # unparseable - forward untouched
            return pf.raw, []
        changes: list[str] = []
        ver, adr, cid1, cid2 = pf.ver, pf.adr, pf.cid1, pf.cid2

        if F.cid2_family(pf.cid2) != "?" and pf.lenid == len(pf.info):
            # request heuristic: known command + short info
            if len(pf.info) <= 2:
                self.recent_request_cid2 = pf.cid2

        if direction in self.force_ver and ver != self.force_ver[direction]:
            changes.append(f"ver {ver:02X}->{self.force_ver[direction]:02X}")
            ver = self.force_ver[direction]
        if direction in self.force_adr and adr != self.force_adr[direction]:
            changes.append(f"adr {adr:02X}->{self.force_adr[direction]:02X}")
            adr = self.force_adr[direction]
        if (direction in self.fix_cid2_echo
                and self.recent_request_cid2 is not None
                and cid2 == self.recent_request_cid2
                and len(pf.info) > 2):
            changes.append(f"cid2-echo {cid2:02X}->RTN 00")
            cid2 = 0x00

        if not changes and pf.ok:
            return pf.raw, []
        out = F.encode_frame(ver, adr, cid1, cid2, pf.info)
        if not pf.ok:
            changes.append("re-checksummed (had errors: " + ";".join(pf.errors) + ")")
        return out, changes
