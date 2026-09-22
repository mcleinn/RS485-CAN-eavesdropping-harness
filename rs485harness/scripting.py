"""Scenario engine: let an LLM (or human) formulate its own exchanges.

Two execution modes, both driven by a JSON scenario:

- **responder** — reactive "if it writes X, answer Y": a list of rules
  matched against each incoming frame; the first match's action fires
  (respond with a frame, stay silent, note, or stop). Generalises the
  battery simulator into something fully user-defined.
- **master** — proactive send/branch: an ordered list of steps, each
  sends a frame, waits for a reply, and branches (next / goto <label> /
  stop) based on `expect` conditions evaluated against the reply.

Frame specs build a real (checksummed) frame from fields, OR send raw hex
verbatim (to test malformed frames). Field values may echo the triggering
request via "$req.adr" / "$req.ver" / "$req.cid1".

Everything is logged to the session JSONL. 0x64 (system shutdown) is
refused unless the scenario sets allow_shutdown:true.

Scenario shape (JSON):
{
  "name": "my-scenario",
  "mode": "responder" | "master",
  "baud": 9600,
  "allow_shutdown": false,
  # responder:
  "unmatched": "silence" | "rtn04",
  "rules": [
    {"when": {"cid2": "61"}, "respond": {"adr": "$req.adr", "cid2": "00",
      "info": "..."}, "delay_ms": 20, "note": "answer analog"},
    {"when": {"cid2": ["60","51"]}, "respond": null, "note": "play dead"}
  ],
  # master:
  "steps": [
    {"label": "s1", "send": {"ver":"20","adr":"02","cid2":"61"},
     "wait_ms": 800,
     "expect": [{"when": {"is_response": true}, "goto": "stop",
                 "note": "battery answered"}],
     "default": {"goto": "next", "note": "no/again"}}
  ]
}
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .core.transport import Link, FrameAccumulator
from .core.session import SessionWriter
from .protocols.pylontech import frames as F
from .protocols.pylontech import payloads as P


class ScenarioError(Exception):
    pass


# --------------------------------------------------------------- conditions

def _as_int(v) -> int:
    if isinstance(v, int):
        return v
    return int(str(v), 16)


def match_frame(pf: F.ParsedFrame, cond: dict) -> bool:
    """All present keys must match (AND). Keys: cid2 (hex str or list),
    adr, ver, cid1, family ('4x'/'6x'/'?'), info_prefix, info_regex,
    hex_regex, is_request, is_response, has_errors, no_reply."""
    if cond.get("no_reply"):
        return pf is None
    if pf is None:
        return False
    def want(key):
        return key in cond
    if want("cid2"):
        allowed = cond["cid2"]
        allowed = [allowed] if not isinstance(allowed, list) else allowed
        if pf.cid2 not in {_as_int(x) for x in allowed}:
            return False
    for key, attr in (("adr", "adr"), ("ver", "ver"), ("cid1", "cid1")):
        if want(key) and pf.__dict__.get(attr) != _as_int(cond[key]):
            return False
    if want("family") and F.cid2_family(pf.cid2) != cond["family"]:
        return False
    if want("info_prefix") and not pf.info.upper().startswith(cond["info_prefix"].upper()):
        return False
    if want("info_regex") and not re.search(cond["info_regex"], pf.info, re.I):
        return False
    if want("hex_regex") and not re.search(cond["hex_regex"], pf.raw.hex().upper(), re.I):
        return False
    # is_request/is_response are heuristics: a request has a known CID2 and
    # short INFO; treat everything else as response-ish.
    is_req = pf.cid2 in F.CID2_NAMES and len(pf.info) <= 4
    if want("is_request") and cond["is_request"] != is_req:
        return False
    if want("is_response") and cond["is_response"] == is_req:
        return False
    if want("has_errors") and bool(pf.errors) != bool(cond["has_errors"]):
        return False
    return True


# --------------------------------------------------------------- frame build

def _resolve(v, req: F.ParsedFrame | None):
    if isinstance(v, str) and v.startswith("$req."):
        if req is None:
            raise ScenarioError(f"{v} used with no request in context")
        field = v[5:]
        val = getattr(req, field, None)
        if val is None:
            raise ScenarioError(f"$req.{field} unavailable")
        return val
    return _as_int(v)


def build_frame(spec: dict, req: F.ParsedFrame | None = None,
                allow_shutdown: bool = False) -> bytes:
    """Build wire bytes from a frame spec. `raw_hex`/`hex` = verbatim bytes
    (may be malformed, for testing). Otherwise fields ver/adr/cid1/cid2/info
    with a correct checksum."""
    raw = spec.get("raw_hex") or spec.get("hex")
    if raw:
        s = raw.strip().replace(" ", "")
        if s.startswith("~"):  # ascii frame form
            b = s.encode("ascii")
            return b if b.endswith(b"\r") else b + b"\r"
        return bytes.fromhex(s)
    ver = _resolve(spec.get("ver", 0x20), req)
    adr = _resolve(spec.get("adr", 0x02), req)
    cid1 = _resolve(spec.get("cid1", 0x46), req)
    if "rtn" in spec:
        cid2 = _as_int(spec["rtn"])
    else:
        cid2 = _resolve(spec.get("cid2", 0x00), req)
    if cid2 == 0x64 and not allow_shutdown:
        raise ScenarioError("refusing to build a 0x64 shutdown frame "
                            "(set allow_shutdown:true to override)")
    info = spec.get("info", "")
    return F.encode_frame(ver, adr, cid1, cid2, info)


# --------------------------------------------------------------- read helper

def _read_one_frame(link: Link, timeout_s: float, session: SessionWriter,
                    chan: str) -> F.ParsedFrame | None:
    acc = FrameAccumulator()
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        chunk = link.read(timeout=min(0.1, max(0.01, deadline - time.perf_counter())))
        if not chunk:
            continue
        for ev in acc.feed(chunk):
            if ev.kind == "frame":
                pf = F.parse_frame(ev.data)
                session.log(chan, "rx", ev.data, "frame",
                            decoded=pf.to_dict(), errors=pf.errors)
                return pf
            else:
                session.log(chan, "rx", ev.data, "raw")
    return None


# --------------------------------------------------------------- responder

def run_responder(link: Link, scenario: dict, session: SessionWriter,
                  chan: str = "resp", duration: float | None = None,
                  on_event=None) -> dict:
    on_event = on_event or (lambda s: None)
    rules = scenario.get("rules", [])
    unmatched = scenario.get("unmatched", "silence")
    allow_sd = scenario.get("allow_shutdown", False)
    stats = {"received": 0, "responded": 0, "silent": 0, "unmatched": 0,
             "by_cid2": {}}
    session.note(f"scenario responder start '{scenario.get('name','?')}' "
                 f"rules={len(rules)} unmatched={unmatched}", chan)
    acc = FrameAccumulator()
    t_end = time.monotonic() + duration if duration else None
    stop = False
    while not stop:
        if t_end and time.monotonic() > t_end:
            break
        chunk = link.read(timeout=0.05)
        events = acc.feed(chunk) if chunk else []
        stale = acc.flush_if_stale(time.perf_counter())
        if stale:
            events.append(stale)
        for ev in events:
            if ev.kind != "frame":
                session.log(chan, "rx", ev.data, "raw")
                continue
            pf = F.parse_frame(ev.data)
            session.log(chan, "rx", ev.data, "frame", decoded=pf.to_dict(),
                        errors=pf.errors)
            stats["received"] += 1
            key = f"{pf.cid2:02X}" if pf.cid2 is not None else "?"
            stats["by_cid2"][key] = stats["by_cid2"].get(key, 0) + 1
            on_event(f"<- {pf.describe('request')}")
            rule = next((r for r in rules if match_frame(pf, r.get("when", {}))),
                        None)
            if rule is None:
                if unmatched == "rtn04" and pf.cid1 is not None:
                    resp = F.encode_frame(pf.ver or 0x20, pf.adr or 0x02,
                                          pf.cid1, 0x04, "")
                    link.write(resp)
                    session.log(chan, "tx", resp, "frame",
                                decoded=F.parse_frame(resp).to_dict(),
                                note="unmatched -> RTN04")
                stats["unmatched"] += 1
                on_event("   (no rule matched)")
                continue
            note = rule.get("note", "")
            if rule.get("stop"):
                stop = True
            if rule.get("delay_ms"):
                time.sleep(rule["delay_ms"] / 1000)
            spec = rule.get("respond")
            if spec is None:
                stats["silent"] += 1
                session.note(f"rule silent: {note}", chan)
                on_event(f"   -> silent ({note})")
                continue
            try:
                out = build_frame(spec, req=pf, allow_shutdown=allow_sd)
            except (ScenarioError, ValueError) as e:
                session.note(f"rule build error: {e}", chan)
                on_event(f"   !! build error: {e}")
                continue
            link.write(out)
            session.log(chan, "tx", out, "frame",
                        decoded=F.parse_frame(out).to_dict(),
                        note=f"rule: {note}")
            stats["responded"] += 1
            on_event(f"   -> responded ({note})")
    session.note(f"scenario responder stop stats={stats}", chan)
    return stats


# --------------------------------------------------------------- master

def run_master(link: Link, scenario: dict, session: SessionWriter,
               chan: str = "master", on_event=None) -> dict:
    on_event = on_event or (lambda s: None)
    steps = scenario.get("steps", [])
    allow_sd = scenario.get("allow_shutdown", False)
    labels = {s["label"]: i for i, s in enumerate(steps) if "label" in s}
    stats = {"steps_run": 0, "responses": 0, "timeouts": 0, "transcript": []}
    session.note(f"scenario master start '{scenario.get('name','?')}' "
                 f"steps={len(steps)}", chan)
    i = 0
    guard = 0
    max_steps = scenario.get("max_steps", 1000)
    while 0 <= i < len(steps) and guard < max_steps:
        guard += 1
        step = steps[i]
        stats["steps_run"] += 1
        out = build_frame(step["send"], allow_shutdown=allow_sd)
        link.flush_input()
        t0 = time.perf_counter()
        link.write(out)
        session.log(chan, "tx", out, "frame",
                    decoded=F.parse_frame(out).to_dict(),
                    note=step.get("label"))
        wait_s = step.get("wait_ms", 800) / 1000
        reply = _read_one_frame(link, wait_s, session, chan)
        latency = round((time.perf_counter() - t0) * 1000, 1) if reply else None
        if reply:
            stats["responses"] += 1
        else:
            stats["timeouts"] += 1
        # evaluate expect cases against the reply (or no-reply)
        action = None
        for case in step.get("expect", []):
            if match_frame(reply, case.get("when", {})):
                action = case
                break
        if action is None:
            action = step.get("default", {"goto": "next"})
        entry = {"step": step.get("label", i), "sent": out.hex().upper(),
                 "reply": reply.to_dict() if reply else None,
                 "latency_ms": latency, "note": action.get("note"),
                 "goto": action.get("goto", "next")}
        stats["transcript"].append(entry)
        on_event(f"[{entry['step']}] "
                 + (f"reply RTN/CID2={reply.cid2:02X} {latency}ms"
                    if reply else "no reply")
                 + f" -> {entry['goto']}"
                 + (f" ({action['note']})" if action.get("note") else ""))
        goto = action.get("goto", "next")
        if goto == "stop":
            break
        if goto == "next":
            i += 1
        elif goto in labels:
            i = labels[goto]
        else:
            raise ScenarioError(f"goto unknown label '{goto}'")
    session.note(f"scenario master stop steps_run={stats['steps_run']} "
                 f"responses={stats['responses']}", chan)
    return stats


# --------------------------------------------------------------- entry

def load_scenario(path_or_text: str) -> dict:
    p = Path(path_or_text)
    text = p.read_text(encoding="utf-8") if p.exists() else path_or_text
    try:
        sc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScenarioError(f"invalid scenario JSON: {e}")
    if "mode" not in sc:
        sc["mode"] = "master" if "steps" in sc else "responder"
    return sc


def run_scenario(link: Link, scenario: dict, session: SessionWriter,
                 chan: str | None = None, duration: float | None = None,
                 on_event=None) -> dict:
    mode = scenario.get("mode", "responder")
    if mode == "responder":
        return run_responder(link, scenario, session, chan or "resp",
                             duration=duration, on_event=on_event)
    if mode == "master":
        return run_master(link, scenario, session, chan or "master",
                          on_event=on_event)
    raise ScenarioError(f"unknown scenario mode '{mode}'")
