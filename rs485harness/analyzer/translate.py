"""Frame rewrite rules for bridge modify mode.

Two layers, both re-checksummed and audited:
- header rules (force VER / force ADR / fix CID2-echo) per direction;
- an optional payload PolicyEngine applied to BMS->INV responses
  (see policy.py for the invariants it enforces).

`bms_to_inv` is the direction string ("ch2->ch1") on which responses from
the battery flow toward the inverter; the bridge sets it from the channel
roles. Requests seen in the opposite direction provide the request CID2
that a following response must be decoded against.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..protocols.pylontech import frames as F
from .policy import PolicyEngine


@dataclass
class Translator:
    force_ver: dict = field(default_factory=dict)        # dir -> ver int
    force_adr: dict = field(default_factory=dict)        # dir -> adr int
    fix_cid2_echo: set = field(default_factory=set)      # dirs where response
    #   headers that echo the request CID2 get rewritten to RTN=00
    engine: PolicyEngine | None = None                   # payload policy
    bms_to_inv: str | None = None                        # e.g. "ch2->ch1"
    recent_request_cid2: int | None = None
    last_audit: dict | None = None

    def describe(self) -> str:
        parts = []
        if self.force_ver:
            parts.append(f"force_ver={ {k: f'{v:02X}' for k, v in self.force_ver.items()} }")
        if self.force_adr:
            parts.append(f"force_adr={ {k: f'{v:02X}' for k, v in self.force_adr.items()} }")
        if self.fix_cid2_echo:
            parts.append(f"fix_cid2_echo={sorted(self.fix_cid2_echo)}")
        if self.engine is not None:
            parts.append(f"policy={self.engine.policy.describe()} "
                         f"(loss_timeout={self.engine.loss_timeout_s}s, "
                         f"cmds={[f'{c:02X}' for c in sorted(self.engine.commands)]})")
        return ", ".join(parts) or "none"

    def _is_request(self, pf: F.ParsedFrame) -> bool:
        return pf.cid2 in F.CID2_NAMES and len(pf.info) <= 4

    def apply(self, pf: F.ParsedFrame, direction: str) -> tuple[bytes, list[str]]:
        """Returns (frame bytes to forward, list of rewrites applied)."""
        self.last_audit = None
        if pf.ver is None:  # unparseable - forward untouched
            return pf.raw, []
        changes: list[str] = []
        ver, adr, cid1, cid2, info = pf.ver, pf.adr, pf.cid1, pf.cid2, pf.info

        is_response_dir = self.bms_to_inv is not None and direction == self.bms_to_inv
        if not is_response_dir or self._is_request(pf):
            if self._is_request(pf):
                self.recent_request_cid2 = pf.cid2

        # ---- payload policy (BMS -> INV responses only)
        if self.engine is not None and is_response_dir and pf.ok:
            new_info, audit = self.engine.process(pf, self.recent_request_cid2)
            self.last_audit = audit
            if new_info is not None:
                desc = ", ".join(f"{k} {audit['original'][k]:g}->{v:g}"
                                 for k, v in audit["modified"].items())
                changes.append(f"policy[{self.engine.policy.name}] {desc}")
                info = new_info

        # ---- header rules
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

        if not changes:
            return pf.raw, []   # untouched bytes, even if the frame had errors
        return F.encode_frame(ver, adr, cid1, cid2, info), changes
