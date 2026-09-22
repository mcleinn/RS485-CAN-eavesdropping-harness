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
            note: str | None = None, ts: float | None = None) -> dict:
        rec = {
            "t": round(ts if ts is not None else time.time(), 4),
            "chan": chan, "dir": direction, "kind": kind,
            "hex": data.hex().upper(), "ascii": printable_ascii(data),
        }
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

    def note(self, text: str, chan: str = "-") -> None:
        self.log(chan, "-", b"", kind="note", note=text)

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


def read_session(path: Path | str):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
