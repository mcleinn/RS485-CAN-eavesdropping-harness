"""JSONL session logs - the LLM-facing record of everything on the wire.

One record per event:
  {"t": <unix epoch float>, "chan": "ch1", "dir": "rx"|"tx", "kind":
   "frame"|"raw"|"note", "hex": "7E32...", "ascii": "~20...", "decoded":
   {...}|null, "errors": [...], "note": "..."}
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

CAPTURE_DIR = Path(__file__).resolve().parents[2] / "captures"


def new_session_path(prefix: str) -> Path:
    CAPTURE_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return CAPTURE_DIR / f"{stamp}-{prefix}.jsonl"


def printable_ascii(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else f"<{b:02X}>" for b in data)


class SessionWriter:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}

    def log(self, chan: str, direction: str, data: bytes, kind: str = "frame",
            decoded: dict | None = None, errors: list | None = None,
            note: str | None = None, ts: float | None = None,
            ts_mono: float | None = None, **extra) -> dict:
        """ts_mono = perf_counter() at *arrival* of the bytes; the wall-clock
        `t` is then back-computed so records carry arrival time, not log
        time. extra: flow ("INV->BMS"/"BMS->INV"), mode (tap/transparent/
        modify), or anything else worth keeping per record."""
        now_wall, now_mono = time.time(), time.perf_counter()
        if ts is None:
            ts = now_wall - (now_mono - ts_mono) if ts_mono is not None else now_wall
        rec = {
            "t": round(ts, 4),
            "chan": chan, "dir": direction, "kind": kind, "len": len(data),
            "hex": data.hex().upper(), "ascii": printable_ascii(data),
        }
        if ts_mono is not None:
            rec["t_mono"] = round(ts_mono, 4)
        for k, v in extra.items():
            if v is not None:
                rec[k] = v
        if decoded is not None:
            rec["decoded"] = decoded
        if errors:
            rec["errors"] = errors
        if note:
            rec["note"] = note
        with self._lock:
            try:
                self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except ValueError:
                return rec  # session already closed (late background writer)
            key = f"{chan}:{direction}:{kind}"
            self.counts[key] = self.counts.get(key, 0) + 1
        return rec

    def note(self, text: str, chan: str = "-", **extra) -> None:
        self.log(chan, "-", b"", kind="note", note=text, **extra)

    MARK_SHORTHAND = {
        "": "(enter)", "r": "rebound/current-rise", "o": "ovwarn=1",
        "l": "inverter_red_led", "z": "current=0", "s": "start", "e": "end",
        "u": "current-up", "d": "current-down", "x": "something-odd",
    }

    def mark(self, text: str = "") -> None:
        """User marker aligned with the traffic. Designed for one keypress
        while watching the system: a bare Enter is enough (auto-numbered),
        single letters expand (r/o/l/z/s/e/u/d/x), a bare number means an
        inverter current reading, anything else is kept verbatim. Exact
        values can be filled in afterwards from the numbered marker."""
        raw = text.strip()
        self._mark_n = getattr(self, "_mark_n", 0) + 1
        key = raw.lower()
        if key.upper().startswith("MARK "):
            body = raw[5:].strip()
        elif key in self.MARK_SHORTHAND:
            body = self.MARK_SHORTHAND[key]
        elif key.replace(".", "", 1).isdigit():
            body = f"inverter_current={raw}"
        else:
            body = raw
        note = f"MARK #{self._mark_n} {body}".rstrip()
        self.log("-", "-", b"", kind="note", note=note, marker=True,
                 mark_n=self._mark_n, mark_raw=raw)

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def save_report(session_path: Path | str, report: dict, kind: str = "report") -> Path:
    """Persist a tool's result report as JSON next to its session file, so
    verdicts/findings survive for later analysis alongside the raw capture."""
    p = Path(session_path)
    out = p.with_name(p.stem + f".{kind}.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                              default=str), encoding="utf-8")
    return out


def start_marker_reader(session: "SessionWriter", on_event=None,
                        enabled: bool = True) -> threading.Thread | None:
    """Background thread: each line typed on stdin while a capture runs
    becomes a timestamped MARK note in the session. Only active when stdin
    is an interactive terminal (otherwise returns None)."""
    import sys
    if not enabled or not sys.stdin or not sys.stdin.isatty():
        return None

    def _run():
        for line in sys.stdin:
            session.mark(line)          # empty line = bare marker, by design
            n = getattr(session, "_mark_n", "?")
            if on_event:
                on_event(f"[marker #{n}] {line.strip() or '(enter)'}")
    t = threading.Thread(target=_run, daemon=True, name="marker-reader")
    t.start()
    return t


def read_session(path: Path | str):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
