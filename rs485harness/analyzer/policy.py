"""Generic payload-modification infrastructure for the inline bridge (Phase 2).

This is the *framework* only: how a response payload (BMS -> inverter) is
decoded into fields, how a pluggable policy may propose new values, how the
framework enforces safety invariants regardless of what the policy says,
how the payload is rebuilt byte-exactly with a fresh checksum, and what is
audited. The experimental charge-shaping policy itself is a separate class
written later; here only a trivial static-cap policy exists as the test
fixture for the pipeline.

Invariants enforced here, not by policies:
- Only commands with a known layout are ever touched (0x63, 0x92).
- Only fields marked `conservative="le"` may change, and only downwards
  (modified <= original, never below 0). Everything else is `locked`:
  status bits (charge/discharge enable, protections), discharge limits,
  command byte - a policy cannot touch them even if it tries.
- A frame that failed checksum/structure is never rebuilt.
- Communication loss (no valid frame for loss_timeout_s) resets the policy;
  the framework never synthesizes a frame - it only ever modifies a frame
  the BMS actually sent, so no permissive state can outlive real data.
- Every modification yields an audit record (original + modified fields,
  reason); the bridge logs both raw frames.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..protocols.pylontech import frames as F


# --------------------------------------------------------------- layouts

@dataclass(frozen=True)
class FieldSpec:
    name: str
    start: int            # hex-char offset in INFO
    length: int           # hex chars
    unit: str             # "a" | "v" | "raw"
    conservative: str | None = None   # "le": modified must be <= original
    locked: bool = True   # policies may never change locked fields
    signed: bool = False


# 0x63 system charge/discharge info: CVL(mV) DVL(mV) CCL(dA) DCL(dA) STATUS
LAYOUT_63 = (
    FieldSpec("cvl", 0, 4, "v", conservative="le", locked=False),
    FieldSpec("dvl", 4, 4, "v"),
    FieldSpec("ccl", 8, 4, "a", conservative="le", locked=False, signed=True),
    FieldSpec("dcl", 12, 4, "a", signed=True),
    FieldSpec("status", 16, 2, "raw"),
)
# 0x92 per-pack management: CMD CVL DVL CCL DCL STATUS
LAYOUT_92 = (
    FieldSpec("cmd", 0, 2, "raw"),
    FieldSpec("cvl", 2, 4, "v", conservative="le", locked=False),
    FieldSpec("dvl", 6, 4, "v"),
    FieldSpec("ccl", 10, 4, "a", conservative="le", locked=False, signed=True),
    FieldSpec("dcl", 14, 4, "a", signed=True),
    FieldSpec("status", 18, 2, "raw"),
)
LAYOUTS = {0x63: LAYOUT_63, 0x92: LAYOUT_92}

DEFAULT_SCALES = {"a": 10, "v": 1000}   # raw units per ampere / volt


def _dec(spec: FieldSpec, info: str, scales: dict) -> float | int:
    raw = int(info[spec.start:spec.start + spec.length], 16)
    if spec.signed and spec.length == 4 and raw >= 0x8000:
        raw -= 0x10000
    if spec.unit == "raw":
        return raw
    return raw / scales[spec.unit]


def _enc(spec: FieldSpec, value: float, scales: dict) -> str:
    raw = int(round(value * scales[spec.unit])) if spec.unit != "raw" else int(value)
    if spec.length == 4:
        return f"{raw & 0xFFFF:04X}"
    return f"{raw & 0xFF:02X}"


def decode_fields(cid2: int, info: str, scales: dict | None = None) -> dict:
    scales = scales or DEFAULT_SCALES
    layout = LAYOUTS[cid2]
    need = max(f.start + f.length for f in layout)
    if len(info) < need:
        raise ValueError(f"0x{cid2:02X} payload too short ({len(info)} < {need})")
    return {f.name: _dec(f, info, scales) for f in layout}


def rebuild_info(cid2: int, info: str, overrides: dict, scales: dict | None = None) -> str:
    """Return INFO with the given fields replaced, everything else byte-identical."""
    scales = scales or DEFAULT_SCALES
    out = info
    for spec in LAYOUTS[cid2]:
        if spec.name in overrides:
            out = (out[:spec.start] + _enc(spec, overrides[spec.name], scales)
                   + out[spec.start + spec.length:])
    return out


# --------------------------------------------------------------- policies

@dataclass
class PolicyContext:
    now: float                 # perf_counter seconds
    wall: float                # epoch seconds
    request_cid2: int
    fields: dict               # decoded (amps/volts/raw), e.g. ccl, cvl, dcl, status
    status_bits: dict          # charge_enable, discharge_enable, ... (from status byte)
    info: str                  # original INFO hex
    dt: float | None           # seconds since previous processed frame (None = first)


class PayloadPolicy:
    """Base class. apply() returns {field: new_value} for *allowed* fields
    (ccl, cvl); return {} to leave the frame untouched. Values above the
    original are clamped by the framework; locked fields are ignored."""
    name = "noop"

    def apply(self, ctx: PolicyContext) -> dict:
        return {}

    def reset(self, reason: str) -> None:
        """Called on communication loss / restart. Forget any state that
        could otherwise carry a permissive value across a gap."""

    def describe(self) -> str:
        return self.name


class CapPolicy(PayloadPolicy):
    """Trivial static caps - the pipeline's test fixture, not an experiment.
    Params: ccl_max (A), cvl_max (V)."""
    name = "cap"

    def __init__(self, ccl_max: float | None = None, cvl_max: float | None = None):
        self.ccl_max = ccl_max
        self.cvl_max = cvl_max

    def apply(self, ctx: PolicyContext) -> dict:
        out = {}
        if self.ccl_max is not None and ctx.fields.get("ccl", 0) > self.ccl_max:
            out["ccl"] = self.ccl_max
        if self.cvl_max is not None and ctx.fields.get("cvl", 0) > self.cvl_max:
            out["cvl"] = self.cvl_max
        return out

    def describe(self) -> str:
        return f"cap(ccl_max={self.ccl_max}, cvl_max={self.cvl_max})"


REGISTRY: dict[str, type] = {"noop": PayloadPolicy, "cap": CapPolicy}


def make_policy(spec: str) -> PayloadPolicy:
    """'name' or 'name:k=v,k=v' -> policy instance (numeric values parsed)."""
    name, _, params = spec.partition(":")
    cls = REGISTRY.get(name.strip())
    if cls is None:
        raise ValueError(f"unknown policy '{name}' (have: {sorted(REGISTRY)})")
    kwargs = {}
    for kv in filter(None, (p.strip() for p in params.split(","))):
        k, _, v = kv.partition("=")
        try:
            kwargs[k.strip()] = float(v)
        except ValueError:
            kwargs[k.strip()] = v.strip()
    return cls(**kwargs)


# --------------------------------------------------------------- engine

STATUS_BITS = {7: "charge_enable", 6: "discharge_enable", 5: "charge_immediately",
               4: "charge_immediately2", 3: "full_charge_request"}


class PolicyEngine:
    """Applies a policy to BMS->INV responses with safety invariants."""

    def __init__(self, policy: PayloadPolicy, scales: dict | None = None,
                 loss_timeout_s: float = 10.0, commands=(0x63, 0x92)):
        self.policy = policy
        self.scales = dict(DEFAULT_SCALES, **(scales or {}))
        self.loss_timeout_s = loss_timeout_s
        self.commands = set(commands)
        self.last_time: float | None = None
        self.stats = {"seen": 0, "modified": 0, "resets": 0, "skipped": 0,
                      "clamped": 0}

    def reset(self, reason: str) -> None:
        self.policy.reset(reason)
        self.last_time = None
        self.stats["resets"] += 1

    def process(self, pf: F.ParsedFrame, request_cid2: int | None,
                now: float | None = None) -> tuple[str | None, dict]:
        """Returns (new_info or None, audit). Never raises for structural
        reasons; policy exceptions propagate so the caller can fail safe."""
        now = time.perf_counter() if now is None else now
        audit: dict = {"policy": self.policy.describe(), "request_cid2": request_cid2}
        if request_cid2 not in self.commands or not pf.ok or pf.cid2 != 0x00:
            audit["skipped"] = ("not a clean RTN=00 response to a modifiable "
                                "command")
            self.stats["skipped"] += 1
            return None, audit
        try:
            fields = decode_fields(request_cid2, pf.info, self.scales)
        except ValueError as e:
            audit["skipped"] = str(e)
            self.stats["skipped"] += 1
            return None, audit

        dt = None
        if self.last_time is not None:
            dt = now - self.last_time
            if dt > self.loss_timeout_s:
                self.reset(f"comm-loss {dt:.1f}s")
                audit["reset"] = f"policy reset after {dt:.1f}s without a valid frame"
                dt = None
        self.last_time = now
        self.stats["seen"] += 1

        status = int(fields.get("status", 0))
        bits = {n: bool(status & (1 << b)) for b, n in STATUS_BITS.items()}
        ctx = PolicyContext(now=now, wall=time.time(), request_cid2=request_cid2,
                            fields=dict(fields), status_bits=bits, info=pf.info, dt=dt)
        proposed = self.policy.apply(ctx) or {}

        # ---- invariants (independent of the policy)
        layout = {f.name: f for f in LAYOUTS[request_cid2]}
        overrides: dict = {}
        notes: list[str] = []
        for name, value in proposed.items():
            spec = layout.get(name)
            if spec is None or spec.locked:
                notes.append(f"ignored '{name}' (locked/unknown)")
                continue
            orig = fields[name]
            value = float(value)
            if spec.conservative == "le" and value > orig:
                notes.append(f"clamped {name} {value:g}->{orig:g} (must be <= BMS)")
                value = orig
                self.stats["clamped"] += 1
            if value < 0:
                value = 0.0
            if abs(value - orig) < 1e-9:
                continue
            overrides[name] = value
        audit["original"] = fields
        audit["notes"] = notes
        if not overrides:
            audit["modified"] = {}
            return None, audit
        new_info = rebuild_info(request_cid2, pf.info, overrides, self.scales)
        # re-decode to prove the rebuild is consistent
        check = decode_fields(request_cid2, new_info, self.scales)
        for name, value in overrides.items():
            if abs(check[name] - value) > 1 / self.scales[layout[name].unit]:
                raise RuntimeError(f"rebuild mismatch for {name}: {check[name]} != {value}")
        audit["modified"] = overrides
        self.stats["modified"] += 1
        return new_info, audit
