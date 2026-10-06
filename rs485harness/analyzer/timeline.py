"""Timeline analysis of a charge-control capture (Phase 1 deliverable).

Input: a session JSONL from `sniff` (single-wire tap) or `bridge`.
Output: a flat per-sample series, a chronological event log, timing
statistics, automatic findings, and CSV/JSON (+ PNG if matplotlib exists).

Pairing: requests (INV->BMS) open a pending slot; the next response
(BMS->INV) within PAIR_WINDOW_S closes it and is decoded with the request's
CID2 - this works on one tap channel and across two bridge channels alike
(records are processed in time order). Direction comes from the record's
`flow` field when present, else from the request heuristic.
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..core.session import read_session
from ..protocols.pylontech import frames as F
from ..protocols.pylontech import payloads as P

PAIR_WINDOW_S = 2.0

SERIES_COLUMNS = [
    "t", "t_rel", "kind", "cid2", "soc", "soh", "pack_v", "pack_i", "max_cell",
    "min_cell", "cvl", "ccl", "dcl", "charge_enable", "discharge_enable",
    "charge_immediately", "full_charge_request", "ov_warn", "alarms",
    "protections", "rtn", "latency_ms", "gap_s", "marker",
]
WATCHED = ["ccl", "cvl", "charge_enable", "discharge_enable",
           "charge_immediately", "full_charge_request", "ov_warn",
           "alarms", "protections"]


@dataclass
class TimelineConfig:
    large_ccl_increase_a: float = 10.0     # or >= 50 % of previous value
    current_tolerance_a: float = 2.0       # pack_i may exceed CCL by this
    response_delay_s: float = 5.0          # after a CCL change, before judging
    gap_window_s: float = 5.0              # gap within +-this of a CCL rise
    gap_factor: float = 3.0                # request gap > factor * median = gap
    current_step_a: float = 2.0            # log pack_i changes >= this
    cell_step_v: float = 0.005             # log max_cell changes >= this
    osc_window_s: float = 300.0
    osc_min_reversals: int = 3
    top_soc: float = 90.0
    top_cell_v: float = 3.45
    ccl_factor: float = 1.0                # scaling override (H5): decoded * f
    current_factor: float = 1.0


def _is_request(dec: dict) -> bool:
    cid2 = dec.get("cid2")
    return cid2 in F.CID2_NAMES and len(dec.get("info", "")) <= 4


def _flow(rec: dict) -> str | None:
    fl = rec.get("flow")
    if fl in ("INV->BMS", "BMS->INV"):
        return fl
    dec = rec.get("decoded") or {}
    if dec.get("cid2") is None:
        return None
    return "INV->BMS" if _is_request(dec) else "BMS->INV"


def _extract(cid2: int, payload: dict, cfg: TimelineConfig) -> dict:
    """Map a decoded payload to series fields (only the ones it carries)."""
    out: dict = {}
    if cid2 == 0x61:
        out["soc"] = payload.get("soc_percent"); out["soh"] = payload.get("soh_percent")
        out["pack_v"] = payload.get("voltage_v")
        if payload.get("current_a") is not None:
            out["pack_i"] = round(payload["current_a"] * cfg.current_factor, 3)
        out["max_cell"] = payload.get("max_cell_v"); out["min_cell"] = payload.get("min_cell_v")
    elif cid2 in (0x63, 0x92):
        out["cvl"] = payload.get("charge_voltage_limit_v")
        if payload.get("charge_current_limit_a") is not None:
            out["ccl"] = round(payload["charge_current_limit_a"] * cfg.ccl_factor, 3)
        if payload.get("discharge_current_limit_a") is not None:
            out["dcl"] = round(payload["discharge_current_limit_a"] * cfg.ccl_factor, 3)
        st = payload.get("status") or {}
        out["charge_enable"] = st.get("charge_enable")
        out["discharge_enable"] = st.get("discharge_enable")
        out["charge_immediately"] = st.get("charge_immediately",
                                           st.get("charge_immediately1"))
        out["full_charge_request"] = st.get("full_charge_request")
    elif cid2 in (0x62, 0x44):
        active = payload.get("active")
        if active is not None:
            out["ov_warn"] = "cell_voltage_high_alarm" in active
            out["alarms"] = "|".join(a for a in active if "protect" not in a)
            out["protections"] = "|".join(a for a in active if "protect" in a)
    elif cid2 == 0x42:
        mods = payload.get("modules") or []
        if mods:
            m = mods[0]
            if m.get("cell_v"):
                out["max_cell"] = max(m["cell_v"]); out["min_cell"] = min(m["cell_v"])
            if m.get("current_a") is not None:
                out["pack_i"] = round(m["current_a"] * cfg.current_factor, 3)
            out["pack_v"] = m.get("voltage_v"); out["soc"] = m.get("soc_percent")
    return {k: v for k, v in out.items() if v is not None}


def build_timeline(path: Path | str, cfg: TimelineConfig | None = None,
                   service_path: Path | str | None = None) -> dict:
    """`service_path`: optional second session recorded by `rsh
    seplos-monitor` at the same time; its records are merged by timestamp.
    (`rsh sniff --service` already writes both into one file.)"""
    cfg = cfg or TimelineConfig()
    recs = list(read_session(path))
    if service_path:
        recs += [r for r in read_session(service_path)
                 if str(r.get("kind", "")).startswith("service")]
    recs.sort(key=lambda r: r.get("t", 0))
    t0 = recs[0]["t"] if recs else 0.0
    state: dict = {}
    series: list[dict] = []
    events: list[dict] = []
    markers: list[dict] = []
    pending: dict | None = None
    req_times: list[float] = []
    latencies: list[float] = []
    unanswered: list[dict] = []
    last_t_by_flow: dict[str, float] = {}
    errors: list[dict] = []
    ccl_hist: list[tuple[float, float]] = []   # (t, ccl) at each change
    ccl_change_events: list[dict] = []
    cid2_hist: dict[str, int] = {}
    mode = None
    svc_samples: list[dict] = []        # Seplos service-socket samples

    def ev(t, kind, text, **kw):
        e = {"t": round(t, 4), "t_rel": round(t - t0, 3), "kind": kind, "text": text, **kw}
        events.append(e)
        return e

    for r in recs:
        t = r.get("t", 0.0)
        if r.get("kind") == "note":
            note = r.get("note") or ""
            if r.get("marker") or note.upper().startswith("MARK"):
                m = {"t": round(t, 4), "t_rel": round(t - t0, 3), "text": note}
                markers.append(m)
                ev(t, "marker", note)
                series.append({"t": round(t, 4), "t_rel": round(t - t0, 3),
                               "kind": "marker", "marker": note, **state})
            elif "gap" in note or "failsafe" in note or "exception" in note:
                ev(t, "note", note[:160])
            if r.get("mode"):
                mode = r.get("mode")
            continue
        if r.get("kind") == "service":
            svc_samples.append({"t": round(t, 4), "t_rel": round(t - t0, 3),
                                **(r.get("decoded") or {})})
            continue
        if r.get("kind") == "service-settings":
            if r.get("changed"):
                ev(t, "svc-setting", r.get("note") or "setting changed")
            continue
        if r.get("kind") != "frame":
            if r.get("kind") == "raw" and r.get("len", 0):
                ev(t, "raw", f"{r.get('len')} unframed bytes "
                             f"({(r.get('decoded') or {}).get('verdict', '?')})")
            continue
        dec = r.get("decoded") or {}
        errs = r.get("errors") or dec.get("errors") or []
        flow = _flow(r)
        mode = r.get("mode") or mode
        if flow:
            gap = t - last_t_by_flow[flow] if flow in last_t_by_flow else None
            last_t_by_flow[flow] = t
        else:
            gap = None
        if errs:
            errors.append({"t": round(t, 4), "t_rel": round(t - t0, 3),
                           "flow": flow, "errors": errs})
            ev(t, "frame-error", f"{flow}: {'; '.join(errs)}")

        if flow == "INV->BMS":
            if pending is not None:
                unanswered.append(pending)
                ev(pending["t"], "unanswered",
                   f"request CID2 {pending['cid2']:02X} got no response")
            pending = {"t": t, "cid2": dec.get("cid2"), "adr": dec.get("adr")}
            req_times.append(t)
            key = f"{dec.get('cid2'):02X}" if dec.get("cid2") is not None else "?"
            cid2_hist[key] = cid2_hist.get(key, 0) + 1
            continue
        if flow != "BMS->INV":
            continue

        # ---- response
        req = pending if pending and t - pending["t"] <= PAIR_WINDOW_S else None
        if pending and req is None:
            unanswered.append(pending)
            ev(pending["t"], "unanswered",
               f"request CID2 {pending['cid2']:02X} got no response (late reply ignored)")
        pending = None
        rtn = dec.get("cid2")
        latency = round((t - req["t"]) * 1000, 1) if req else None
        if latency is not None:
            latencies.append(latency)
        if rtn not in (0x00, None) and not (req and rtn == req["cid2"]):
            ev(t, "rtn-error", f"RTN={rtn:02X} ({F.RTN_CODES.get(rtn, '?')})"
                               + (f" to CID2 {req['cid2']:02X}" if req else ""))
        row = {"t": round(t, 4), "t_rel": round(t - t0, 3), "kind": "sample",
               "cid2": f"{req['cid2']:02X}" if req else None, "rtn": rtn,
               "latency_ms": latency, "gap_s": round(gap, 3) if gap is not None else None}
        new: dict = {}
        if req and dec.get("info") and not errs:
            payload = P.decode_response_info(req["cid2"], dec["info"])
            if payload and "decode_error" not in payload:
                new = _extract(req["cid2"], payload, cfg)
        # ---- change detection against the running state
        for k, v in new.items():
            old = state.get(k)
            if k in WATCHED:
                if old is not None and old != v:
                    kind = "ccl-change" if k == "ccl" else f"{k}-change"
                    e = ev(t, kind, f"{k} changed {old} -> {v}", field=k, old=old, new=v)
                    if k == "ccl":
                        ccl_change_events.append(e)
                elif old is None:
                    ev(t, "first", f"{k} = {v}", field=k, new=v)
            elif k == "pack_i":
                if old is None or abs(v - old) >= cfg.current_step_a:
                    ev(t, "current", f"pack current {old} -> {v} A", field=k, old=old, new=v)
            elif k == "max_cell":
                if old is None or abs(v - old) >= cfg.cell_step_v:
                    ev(t, "maxcell", f"max cell {old} -> {v} V", field=k, old=old, new=v)
            elif k == "soc":
                if old is None or v != old:
                    ev(t, "soc", f"SOC {old} -> {v} %", field=k, old=old, new=v)
            state[k] = v
        if "ccl" in new and (not ccl_hist or ccl_hist[-1][1] != new["ccl"]):
            ccl_hist.append((t, new["ccl"]))
        row.update(state)
        series.append(row)
    if pending is not None:
        unanswered.append(pending)

    # ---------------------------------------------------------- timing
    def _stats(vals):
        if not vals:
            return None
        s = sorted(vals)
        return {"n": len(s), "min": round(s[0], 3), "median": round(statistics.median(s), 3),
                "p95": round(s[int(0.95 * (len(s) - 1))], 3), "max": round(s[-1], 3)}
    req_gaps = [b - a for a, b in zip(req_times, req_times[1:])]
    poll = _stats(req_gaps)
    ccl_intervals = [b - a for (a, _), (b, _) in zip(ccl_hist, ccl_hist[1:])]
    hold_durations = [{"t_rel": round(ta - t0, 3), "ccl": ca, "held_s": round(tb - ta, 3)}
                      for (ta, ca), (tb, _) in zip(ccl_hist, ccl_hist[1:])]
    big_gaps = []
    if poll:
        thresh = poll["median"] * cfg.gap_factor
        for a, b in zip(req_times, req_times[1:]):
            if b - a > thresh:
                big_gaps.append({"t": round(a, 4), "t_end": round(b, 4),
                                 "t_rel": round(a - t0, 3), "gap_s": round(b - a, 3)})
                ev(a, "gap", f"no polling for {b - a:.2f}s (median period {poll['median']:.2f}s)")
    stats = {
        "records": len(recs), "requests": len(req_times),
        "responses": len([r for r in series if r["kind"] == "sample"]),
        "duration_s": round((recs[-1]["t"] - t0) if recs else 0, 1),
        "mode": mode, "cid2_requests": cid2_hist,
        "poll_period_s": poll, "response_latency_ms": _stats(latencies),
        "ccl_update_interval_s": _stats(ccl_intervals),
        "ccl_hold_s": _stats([h["held_s"] for h in hold_durations]),
        "unanswered": len(unanswered), "frame_errors": len(errors),
        "polling_gaps": big_gaps,
    }

    # ---------------------------------------------------------- findings
    findings: list[dict] = []

    def find(kind, sev, t, text, **kw):
        findings.append({"kind": kind, "severity": sev, "t": round(t, 4),
                         "t_rel": round(t - t0, 3), "text": text, **kw})

    samples = [r for r in series if r["kind"] == "sample"]
    for e in ccl_change_events:
        old, new, t = e["old"], e["new"], e["t"]
        if new > old and (new - old >= cfg.large_ccl_increase_a or (old > 0 and new >= 1.5 * old)):
            # a gap counts as "near" if the rise falls within the window of
            # the gap interval itself - i.e. the rise came right after silence
            near_gap = [g for g in big_gaps
                        if g["t"] - cfg.gap_window_s <= t <= g["t_end"] + cfg.gap_window_s]
            near_err = [x for x in errors if abs(x["t"] - t) <= cfg.gap_window_s]
            near_un = [u for u in unanswered if abs(u["t"] - t) <= cfg.gap_window_s]
            ctx = {k: state_at(samples, t, k) for k in ("soc", "max_cell", "pack_i",
                                                        "charge_enable", "full_charge_request",
                                                        "ov_warn", "cvl")}
            find("ccl-large-increase", "warn", t,
                 f"CCL rose {old} -> {new} A" + (" with a polling gap nearby (H3?)"
                 if near_gap else "") + (" with frame errors nearby" if near_err else "")
                 + (" with unanswered requests nearby" if near_un else ""),
                 old=old, new=new, context=ctx, gaps=near_gap, errors=near_err,
                 unanswered=[{"t_rel": round(u["t"] - t0, 3), "cid2": f"{u['cid2']:02X}"}
                             for u in near_un])
    # current exceeds CCL. Judged against the most permissive CCL advertised
    # during the trailing response_delay_s (not "this CCL value has been in
    # effect that long"): during taper the CCL changes on every poll, which
    # would otherwise mask a persistent excess. Consecutive violating samples
    # are merged into one episode.
    ccl_times = [ta for ta, _ in ccl_hist]

    def _ccl_ceiling(t):
        lo = bisect.bisect_left(ccl_times, t - cfg.response_delay_s)
        hi = bisect.bisect_right(ccl_times, t)
        if lo == 0:
            return None                 # CCL not known for a full window yet
        return max(c for _, c in ccl_hist[lo - 1:hi])

    episode = None

    def _close_episode():
        e = episode
        dur = e["t_end"] - e["t"]
        find("current-exceeds-ccl", "warn", e["t"],
             f"pack current above advertised CCL for {dur:.1f}s "
             f"({e['n']} samples): worst {e['pack_i']} A vs CCL {e['ccl']} A "
             f"(highest CCL in the preceding {cfg.response_delay_s:.0f}s: "
             f"{e['ceiling']} A; tolerance {cfg.current_tolerance_a} A)",
             pack_i=e["pack_i"], ccl=e["ccl"], ceiling=e["ceiling"],
             duration_s=round(dur, 3), samples=e["n"], excess_a=round(e["excess"], 2))

    for r in samples:
        if r.get("pack_i") is None or r.get("ccl") is None:
            continue
        ceiling = _ccl_ceiling(r["t"])
        if ceiling is None:
            continue
        excess = r["pack_i"] - ceiling
        if excess > cfg.current_tolerance_a:
            if episode is None:
                episode = {"t": r["t"], "n": 0, "excess": -1.0}
            episode["n"] += 1
            episode["t_end"] = r["t"]
            if excess > episode["excess"]:
                episode.update(excess=excess, pack_i=r["pack_i"], ccl=r["ccl"],
                               ceiling=ceiling)
        elif episode is not None:
            _close_episode()
            episode = None
    if episode is not None:
        _close_episode()
    # CCL -> current response delay
    delays = []
    for e in ccl_change_events:
        after = [r for r in samples if r["t"] > e["t"] and r.get("pack_i") is not None]
        before = state_at(samples, e["t"], "pack_i")
        for r in after:
            if before is None:
                break
            moved = abs(r["pack_i"] - before)
            if moved >= max(1.0, 0.2 * abs(e["new"] - e["old"])):
                delays.append(round(r["t"] - e["t"], 3)); break
    stats["ccl_to_current_delay_s"] = _stats(delays)
    # oscillation near top of charge
    dirs = []
    for (ta, ca), (tb, cb) in zip(ccl_hist, ccl_hist[1:]):
        dirs.append((tb, 1 if cb > ca else -1))
    for i, (t, d) in enumerate(dirs):
        window = [x for x in dirs if t <= x[0] <= t + cfg.osc_window_s]
        reversals = sum(1 for (_, a), (_, b) in zip(window, window[1:]) if a != b)
        soc = state_at(samples, t, "soc"); mc = state_at(samples, t, "max_cell")
        near_top = (soc is not None and soc >= cfg.top_soc) or (mc is not None and mc >= cfg.top_cell_v)
        if reversals >= cfg.osc_min_reversals and near_top:
            find("ccl-oscillation", "warn", t,
                 f"{reversals} CCL direction reversals within {cfg.osc_window_s:.0f}s "
                 f"near top of charge (SOC {soc}, max cell {mc})")
            break
    for u in unanswered[:50]:
        find("unanswered-request", "info", u["t"], f"CID2 {u['cid2']:02X} unanswered")
    for g in big_gaps:
        find("polling-gap", "info", g["t"], f"polling gap {g['gap_s']}s")
    # unit sanity (H5)
    mx_ccl = max((r["ccl"] for r in samples if r.get("ccl") is not None), default=None)
    mx_i = max((abs(r["pack_i"]) for r in samples if r.get("pack_i") is not None), default=None)
    cvls = {r["cvl"] for r in samples if r.get("cvl") is not None}
    if mx_ccl is not None and mx_ccl > 500:
        find("unit-suspect", "warn", t0, f"max decoded CCL {mx_ccl} A implausible - "
             f"scaling? try --ccl-factor 0.1")
    if mx_i is not None and mx_i > 500:
        find("unit-suspect", "warn", t0, f"max decoded |current| {mx_i} A implausible - "
             f"scaling? try --current-factor 0.1")
    for v in cvls:
        if not 40 <= v <= 62:
            find("unit-suspect", "warn", t0, f"CVL {v} V outside 40-62 V")

    if svc_samples:
        stats["service"] = _service_side(svc_samples, ev, find)
        events.sort(key=lambda e: e["t"])

    return {"session": str(path), "config": asdict(cfg), "stats": stats,
            "series": series, "events": events, "markers": markers,
            "findings": findings, "unanswered": len(unanswered),
            "service_series": svc_samples}


def _service_side(svc: list[dict], ev, find) -> dict:
    """Events, findings and summary from Seplos service-socket samples:
    what the battery itself reported (flags, charge switch, cells)."""
    from .seplos_monitor import diff_samples
    prev = None
    last_bal_t = None
    peaks: dict[int, float] = {}
    flags_seen: set[str] = set()
    modes_seen: set[str] = set()
    worst_spread = None
    top = None
    for s in svc:
        t = s["t"]
        for text in diff_samples(prev, s, limits=False):
            ev(t, "svc", text)
            if text.startswith("flag SET: ") and "protection" in text:
                find("battery-protection", "warn", t,
                     f"battery reports {text[10:]} (pack {s.get('pack_v')} V, "
                     f"{s.get('pack_i')} A, top cell #{s.get('max_cell_no')} "
                     f"{s.get('max_cell')} V)", flag=text[10:])
            elif text.startswith("flag SET: ") and "alarm" in text:
                find("battery-alarm", "info", t,
                     f"battery reports {text[10:]} (top cell "
                     f"#{s.get('max_cell_no')} {s.get('max_cell')} V, "
                     f"{s.get('pack_i')} A)", flag=text[10:])
            elif text == "Charge FET on: OFF":
                i_before = (prev or {}).get("pack_i")
                find("battery-opened-charge-switch",
                     "warn" if (i_before or 0) > 1.0 else "info", t,
                     f"the battery switched its charge path OFF "
                     f"(current just before: {i_before} A, top cell "
                     f"#{s.get('max_cell_no')} {s.get('max_cell')} V)",
                     pack_i_before=i_before)
        if (prev is not None and prev.get("balancing") != s.get("balancing")
                and (last_bal_t is None or t - last_bal_t >= 15.0)):
            last_bal_t = t
            ev(t, "svc", f"balancing cells: {s.get('balancing') or 'none'}")
        for n, v in enumerate(s.get("cells") or [], 1):
            if v > peaks.get(n, 0):
                peaks[n] = v
        flags_seen.update(s.get("flags") or [])
        modes_seen.update(s.get("mode") or [])
        if s.get("spread_mv") is not None and (
                worst_spread is None or s["spread_mv"] > worst_spread["spread_mv"]):
            worst_spread = {"t_rel": s["t_rel"], "spread_mv": s["spread_mv"],
                            "max_cell_no": s.get("max_cell_no"),
                            "min_cell_no": s.get("min_cell_no")}
        if s.get("max_cell") is not None and (top is None or s["max_cell"] > top["v"]):
            top = {"t_rel": s["t_rel"], "cell": s.get("max_cell_no"),
                   "v": s["max_cell"]}
        prev = s
    limits = [s["bms_charge_a"] for s in svc if s.get("bms_charge_a") is not None]
    return {"samples": len(svc),
            "span_s": round(svc[-1]["t"] - svc[0]["t"], 1),
            "cell_peak_v": {str(n): v for n, v in sorted(peaks.items())},
            "highest_cell": top, "worst_spread": worst_spread,
            "flags_seen": sorted(flags_seen), "modes_seen": sorted(modes_seen),
            "bms_charge_a_range": [min(limits), max(limits)] if limits else None}


class LiveState:
    """Lightweight live pairing/decoding for the capture tools: feed frames
    as they arrive, get back one-line summaries whenever a watched charge
    field changes (plus a periodic heartbeat). Lets the operator read CCL /
    current / MaxCell / OV-warn off the wire instead of transcribing LCDs."""

    def __init__(self, heartbeat_s: float = 30.0, cfg: TimelineConfig | None = None):
        self.cfg = cfg or TimelineConfig()
        self.heartbeat_s = heartbeat_s
        self.state: dict = {}
        self.pending_cid2: int | None = None
        self.last_print = 0.0

    def summary(self) -> str:
        st = self.state
        def f(k, fmt="{}"):
            v = st.get(k)
            return "-" if v is None else fmt.format(v)
        return (f"I={f('pack_i', '{:.1f}')}A CCL={f('ccl', '{:g}')}A "
                f"CVL={f('cvl', '{:.2f}')}V MaxCell={f('max_cell', '{:.3f}')}V "
                f"SOC={f('soc')}% chg={f('charge_enable')} "
                f"full={f('full_charge_request')} OVwarn={f('ov_warn')}")

    def feed(self, flow: str | None, pf, now: float) -> list[str]:
        out: list[str] = []
        if pf.cid2 is None:
            return out
        if flow == "INV->BMS":
            self.pending_cid2 = pf.cid2
            return out
        if flow != "BMS->INV" or self.pending_cid2 is None or not pf.ok:
            return out
        payload = P.decode_response_info(self.pending_cid2, pf.info)
        if not payload or "decode_error" in payload:
            return out
        new = _extract(self.pending_cid2, payload, self.cfg)
        changed = []
        for k, v in new.items():
            old = self.state.get(k)
            if k in WATCHED and old is not None and old != v:
                changed.append(f"{k} {old}->{v}")
            elif k == "pack_i" and old is not None and abs(v - old) >= self.cfg.current_step_a:
                changed.append(f"I {old}->{v}A")
            self.state[k] = v
        if changed:
            out.append("CHANGE " + ", ".join(changed) + "  | " + self.summary())
            self.last_print = now
        elif now - self.last_print >= self.heartbeat_s:
            out.append("STATE  " + self.summary())
            self.last_print = now
        return out


def state_at(samples: list[dict], t: float, key: str):
    val = None
    for r in samples:
        if r["t"] > t:
            break
        if r.get(key) is not None:
            val = r[key]
    return val


# ---------------------------------------------------------------- export

def export(result: dict, base: Path | str) -> dict:
    base = Path(base)
    paths = {}
    csv_path = base.with_name(base.stem + ".timeline.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SERIES_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in result["series"]:
            w.writerow(r)
    paths["csv"] = str(csv_path)
    json_path = base.with_name(base.stem + ".timeline.json")
    svc = result.get("service_series") or []
    if svc:
        ncell = max(len(r.get("cells") or []) for r in svc)
        cols = (["t", "t_rel", "pack_v", "pack_i", "soc", "bms_charge_a",
                 "max_cell", "max_cell_no", "min_cell", "min_cell_no", "spread_mv"]
                + [f"cell{n:02d}" for n in range(1, ncell + 1)]
                + ["mode", "charge_fet", "discharge_fet", "flags",
                   "cells_high_alarm", "balancing"])
        svc_path = base.with_name(base.stem + ".service.csv")
        with open(svc_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in svc:
                row = dict(r)
                for n, v in enumerate(r.get("cells") or [], 1):
                    row[f"cell{n:02d}"] = v
                fets = r.get("fets") or {}
                row.update(mode="+".join(r.get("mode") or []),
                           charge_fet=fets.get("Charge FET on"),
                           discharge_fet=fets.get("Discharge FET on"),
                           flags="; ".join(r.get("flags") or []),
                           cells_high_alarm=" ".join(map(str, r.get("cells_high_alarm") or [])),
                           balancing=" ".join(map(str, r.get("balancing") or [])))
                w.writerow(row)
        paths["service_csv"] = str(svc_path)
    slim = {k: v for k, v in result.items() if k not in ("series", "service_series")}
    json_path.write_text(json.dumps(slim, indent=2, ensure_ascii=False, default=str),
                         encoding="utf-8")
    paths["json"] = str(json_path)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        s = [r for r in result["series"] if r["kind"] == "sample"]
        if s:
            fig, ax = plt.subplots(3, 1, sharex=True, figsize=(12, 8))
            def line(a, key, label):
                xs = [r["t_rel"] for r in s if r.get(key) is not None]
                ys = [r[key] for r in s if r.get(key) is not None]
                if xs:
                    a.step(xs, ys, where="post", label=label)
            line(ax[0], "pack_v", "pack V"); line(ax[0], "cvl", "CVL")
            a0b = ax[0].twinx(); line(a0b, "max_cell", "max cell V"); a0b.set_ylabel("cell V")
            ax[0].legend(loc="upper left"); ax[0].set_ylabel("V")
            line(ax[1], "pack_i", "pack I"); line(ax[1], "ccl", "CCL"); ax[1].legend(); ax[1].set_ylabel("A")
            line(ax[2], "soc", "SOC %")
            for key in ("ov_warn", "charge_enable", "full_charge_request"):
                xs = [r["t_rel"] for r in s if r.get(key) is not None]
                ys = [100 if r[key] else 0 for r in s if r.get(key) is not None]
                if xs:
                    ax[2].step(xs, ys, where="post", label=key, alpha=0.6)
            ax[2].legend(); ax[2].set_xlabel("s since start")
            for m in result["markers"]:
                for a in ax:
                    a.axvline(m["t_rel"], color="k", ls=":", alpha=0.5)
            png = base.with_name(base.stem + ".timeline.png")
            fig.tight_layout(); fig.savefig(png, dpi=110); plt.close(fig)
            paths["png"] = str(png)
    except Exception as e:  # matplotlib missing or plotting failed - CSV is enough
        paths["png"] = f"(no PNG: {e.__class__.__name__}: {e})"
    return paths


def format_timeline(result: dict, max_events: int = 400) -> str:
    st = result["stats"]
    lines = [f"TIMELINE of {result['session']}  mode={st.get('mode')}",
             f"  {st['requests']} requests / {st['responses']} responses over "
             f"{st['duration_s']}s; CID2 polled: {st['cid2_requests']}",
             f"  poll period s: {st['poll_period_s']}",
             f"  response latency ms: {st['response_latency_ms']}",
             f"  CCL update interval s: {st['ccl_update_interval_s']}   "
             f"CCL hold s: {st['ccl_hold_s']}",
             f"  CCL->current delay s: {st.get('ccl_to_current_delay_s')}",
             f"  unanswered: {st['unanswered']}  frame errors: {st['frame_errors']}  "
             f"polling gaps: {len(st['polling_gaps'])}"]
    sv = st.get("service")
    if sv:
        hc, ws = sv.get("highest_cell") or {}, sv.get("worst_spread") or {}
        lines.append(f"SERVICE SIDE (battery's own data): {sv['samples']} samples "
                     f"over {sv['span_s']}s")
        lines.append(f"  highest cell: #{hc.get('cell')} at {hc.get('v')} V "
                     f"(+{hc.get('t_rel')}s); worst spread {ws.get('spread_mv')} mV "
                     f"(#{ws.get('max_cell_no')} above #{ws.get('min_cell_no')}, "
                     f"+{ws.get('t_rel')}s)")
        lines.append("  cell peaks V: " + " ".join(
            f"#{n}={v}" for n, v in sv["cell_peak_v"].items()))
        lines.append(f"  flags seen: {', '.join(sv['flags_seen']) or 'none'}; "
                     f"modes: {', '.join(sv['modes_seen']) or '-'}; BMS allowed "
                     f"charge current range: {sv.get('bms_charge_a_range')} A")
    lines.append("FINDINGS:" if result["findings"] else "FINDINGS: none")
    for f in result["findings"]:
        lines.append(f"  [{f['severity'].upper():4}] +{f['t_rel']:9.3f}s {f['kind']}: {f['text']}")
    evs = result["events"]
    lines.append(f"EVENTS ({len(evs)}):")
    shown = evs if len(evs) <= max_events else evs[:max_events // 2] + [None] + evs[-max_events // 2:]
    for e in shown:
        if e is None:
            lines.append("  ... (truncated; full list in .timeline.json)")
            continue
        lines.append(f"  +{e['t_rel']:9.3f}s {e['kind']:14} {e['text']}")
    return "\n".join(lines)
