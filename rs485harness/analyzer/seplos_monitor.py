"""Continuous monitor on a Seplos BMS 3.0 service socket (read-only).

Polls pack status, all cell voltages and the battery's own status flags
every `interval_s` and logs one decoded "service" record per poll into the
session. Run it alongside a tap on the inverter link (`rsh sniff --service`
puts both into one session file) to see what the inverter link cannot show:
which cell runs ahead, which alarm or protection fired, whether the BMS
opened its charge switch, which cells are being balanced.

Only Modbus read functions (0x04, 0x01) are ever sent.
"""
from __future__ import annotations

import threading
import time

from ..core.transport import Link
from ..core.session import SessionWriter
from . import seplos_native as SN

TOP_CELL_MIN_SPREAD_MV = 10      # below this the "top cell" is noise
TOP_CELL_LEAD_V = 0.003          # new top cell must lead the old one by this
BALANCE_PRINT_EVERY_S = 15.0


def build_sample(pia: list[int], pib: list[int], pic: list[bool] | None) -> dict:
    a = {e["name"]: e["value"] for e in SN.decode_block("PIA", pia)}
    b = SN.decode_block("PIB", pib)
    cells = [e["value"] for e in b if e["name"].startswith("Cell") and
             "temperature" not in e["name"].lower() and e["raw"]]
    temps = [e["value"] for e in b if "temperature" in e["name"].lower()
             and e["raw"] and e["raw"] != 2731]
    s = {"pack_v": a["Battery voltage"], "pack_i": a["Current"],
         "soc": a["SOC"], "soh": a["SOH"],
         "bms_charge_a": a["Max charge current"],
         "bms_discharge_a": a["Max discharge current"],
         "cells": cells, "temps_c": temps}
    if cells:
        hi, lo = max(cells), min(cells)
        s.update(max_cell=hi, min_cell=lo, max_cell_no=cells.index(hi) + 1,
                 min_cell_no=cells.index(lo) + 1,
                 spread_mv=round((hi - lo) * 1000))
    if pic is not None:
        s.update(SN.decode_pic(pic))
    return s


def diff_samples(prev: dict | None, cur: dict, limits: bool = True) -> list[str]:
    """Human-readable changes between two service samples."""
    if prev is None:
        return []
    out = []
    if prev.get("mode") != cur.get("mode") and "mode" in cur:
        out.append(f"battery mode {'+'.join(prev.get('mode') or ['-'])} -> "
                   f"{'+'.join(cur['mode']) or '-'}")
    for fet, on in (cur.get("fets") or {}).items():
        was = (prev.get("fets") or {}).get(fet)
        if was is not None and was != on:
            out.append(f"{fet}: {'ON' if on else 'OFF'}")
    pf, cf = set(prev.get("flags") or []), set(cur.get("flags") or [])
    out += [f"flag SET: {f}" for f in sorted(cf - pf)]
    out += [f"flag cleared: {f}" for f in sorted(pf - cf)]
    for key, label in (("cells_high_alarm", "cells in high-voltage alarm"),
                       ("cells_low_alarm", "cells in low-voltage alarm"),
                       ("temps_high_alarm", "temperature sensors in high alarm"),
                       ("temps_low_alarm", "temperature sensors in low alarm")):
        if key in cur and prev.get(key) != cur[key]:
            out.append(f"{label}: {cur[key] or 'none'}")
    if (cur.get("spread_mv", 0) >= TOP_CELL_MIN_SPREAD_MV and
            prev.get("max_cell_no") != cur.get("max_cell_no")):
        # only when the new top cell is clearly ahead of the previous one
        # (two cells within a millivolt of each other just trade places)
        cells = cur.get("cells") or []
        old_no = prev.get("max_cell_no")
        old_v = cells[old_no - 1] if old_no and old_no <= len(cells) else None
        if old_v is None or cur["max_cell"] - old_v >= TOP_CELL_LEAD_V:
            out.append(f"top cell is now #{cur['max_cell_no']} "
                       f"({cur['max_cell']:.3f} V, spread {cur['spread_mv']} mV)")
    if limits and prev.get("bms_charge_a") != cur.get("bms_charge_a"):
        out.append(f"BMS allowed charge current {prev.get('bms_charge_a')} -> "
                   f"{cur.get('bms_charge_a')} A")
    return out


def state_line(s: dict) -> str:
    parts = [f"V={s['pack_v']:.2f}", f"I={s['pack_i']:.1f}A", f"SOC={s['soc']}%"]
    if "max_cell" in s:
        parts.append(f"top #{s['max_cell_no']} {s['max_cell']:.3f}V "
                     f"low #{s['min_cell_no']} {s['min_cell']:.3f}V "
                     f"spread {s['spread_mv']}mV")
    parts.append(f"allow-chg {s['bms_charge_a']}A")
    if "mode" in s:
        parts.append("mode " + ("+".join(s["mode"]) or "-"))
        off = [f for f, on in s["fets"].items()
               if not on and f in ("Charge FET on", "Discharge FET on")]
        if off:
            parts.append("OFF: " + ", ".join(off))
        if s["flags"]:
            parts.append("flags: " + ", ".join(s["flags"]))
        if s["cells_high_alarm"]:
            parts.append(f"high-V cells {s['cells_high_alarm']}")
        if s["balancing"]:
            parts.append(f"balancing {s['balancing']}")
    return "  ".join(parts)


class ServiceMonitor:
    def __init__(self, link: Link, session: SessionWriter, chan: str = "svc",
                 slave: int = 0, interval_s: float = 2.0, on_event=None,
                 heartbeat_s: float = 30.0, settings_every_s: float = 60.0,
                 print_limits: bool = True):
        self.link = link
        self.session = session
        self.chan = chan
        self.slave = slave
        self.interval_s = interval_s
        self.on_event = on_event or (lambda line: None)
        self.heartbeat_s = heartbeat_s
        self.settings_every_s = settings_every_s
        self.print_limits = print_limits
        self.stop_event = threading.Event()
        self.stats = {"polls": 0, "ok": 0, "failed": 0, "changes": 0}
        self.last: dict | None = None
        self.settings: list[int] | None = None
        self._thread: threading.Thread | None = None
        self._down = False
        self._last_hb = 0.0
        self._last_bal_print = 0.0
        self._last_settings = 0.0

    def _say(self, text: str) -> None:
        self.on_event(time.strftime("%H:%M:%S ") + "SVC " + text)

    # ------------------------------------------------------------- one poll
    def poll_once(self) -> dict | None:
        self.stats["polls"] += 1
        pia, ia = SN.read_registers(self.link, self.slave, 0x1000, len(SN.PIA),
                                    timeout=0.6)
        pib, ib = (SN.read_registers(self.link, self.slave, 0x1100, len(SN.PIB),
                                     timeout=0.6) if pia is not None else (None, ia))
        if pia is None or pib is None:
            self.stats["failed"] += 1
            if not self._down:
                self._down = True
                why = (ia if pia is None else ib).get("status")
                msg = f"service socket not answering ({why})"
                self._say("!! " + msg)
                self.session.note("service monitor: " + msg, self.chan)
            return None
        pic, ic = SN.read_coils(self.link, self.slave, SN.PIC_START,
                                SN.PIC_COUNT, timeout=0.6)
        if self._down:
            self._down = False
            self._say("service socket answering again")
            self.session.note("service monitor: answering again", self.chan)
        self.stats["ok"] += 1
        sample = build_sample(pia, pib, pic)
        self.session.log(self.chan, "rx", b"", kind="service", decoded=sample,
                         raw={"pia": pia, "pib": pib, "pic": ic.get("hex")})
        now = time.monotonic()
        changes = diff_samples(self.last, sample, limits=self.print_limits)
        if (self.last is not None and
                self.last.get("balancing") != sample.get("balancing") and
                now - self._last_bal_print >= BALANCE_PRINT_EVERY_S):
            self._last_bal_print = now
            changes.append(f"balancing cells: {sample.get('balancing') or 'none'}")
        for c in changes:
            self.stats["changes"] += 1
            self._say("CHANGE " + c + "  | " + state_line(sample))
        if self.last is None or now - self._last_hb >= self.heartbeat_s:
            self._last_hb = now
            self._say("STATE  " + state_line(sample))
        self.last = sample
        if now - self._last_settings >= self.settings_every_s:
            self._last_settings = now
            self._check_settings()
        return sample

    def _check_settings(self) -> None:
        words, _info = SN.read_registers(self.link, self.slave, 0x1300,
                                         len(SN.SPA), timeout=0.8)
        if words is None:
            return
        if self.settings is None:
            dec = SN.decode_block("SPA", words)
            self.session.log(self.chan, "rx", b"", kind="service-settings",
                             decoded={e["no"]: [e["name"], e["value"], e["unit"]]
                                      for e in dec}, raw={"spa": words})
            key = {e["no"]: e for e in dec}
            self._say(f"settings: 102 charging request voltage "
                      f"{key[102]['value']} V, 103 charging request current "
                      f"{key[103]['value']} A")
        else:
            old = SN.decode_block("SPA", self.settings)
            new = SN.decode_block("SPA", words)
            for o, n in zip(old, new):
                if o["raw"] != n["raw"]:
                    msg = (f"setting {n['no']} {n['name']} changed "
                           f"{o['value']} -> {n['value']} {n['unit']}")
                    self._say("CHANGE " + msg)
                    self.session.log(self.chan, "rx", b"", kind="service-settings",
                                     decoded={n["no"]: [n["name"], n["value"], n["unit"]]},
                                     note=msg, changed=True)
        self.settings = words

    # ------------------------------------------------------------------ run
    def run(self, duration: float | None = None) -> dict:
        self.session.note(f"service monitor start on {self.link.name} "
                          f"device={self.slave} interval={self.interval_s}s",
                          self.chan)
        started = time.monotonic()
        try:
            while not self.stop_event.is_set():
                t = time.monotonic()
                if duration and t - started >= duration:
                    break
                try:
                    self.poll_once()
                except Exception as e:      # adapter re-enumerated etc.
                    self.stats["failed"] += 1
                    msg = f"serial error on {self.link.name}: {e!r} - reopening"
                    self._say("!! " + msg)
                    self.session.note("service monitor: " + msg, self.chan,
                                      serial_error=True)
                    while not self.stop_event.is_set():
                        if not hasattr(self.link, "reopen") or self.link.reopen():
                            break
                        self.stop_event.wait(1.0)
                self.stop_event.wait(max(0.05, self.interval_s -
                                         (time.monotonic() - t)))
        except KeyboardInterrupt:
            pass
        self.session.note(f"service monitor stop stats={self.stats}", self.chan)
        return self.stats

    def start(self, duration: float | None = None) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, args=(duration,),
                                        daemon=True, name="seplos-service-monitor")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
