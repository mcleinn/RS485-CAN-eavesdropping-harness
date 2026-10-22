"""Huawei R48xx address watch: which serial answers from which address,
and when that changes (Phase B tests B5.1, B5.4, B5.5).

Rules (docs/refs/huawei/NOTES.md section 8): addresses are negotiated among
the units on power-up and whenever a unit joins or restarts - `30 01` +
serial to 0x1000107E (no address yet), then `30 02` + serial from the
unit's current address on 0x10xx107E; lowest serial takes address 1. So a
command sent to an address can reach a different unit after a change.
This watcher keeps serial -> address from the negotiation frames and
from 0x50 info replies (serial sub-frame on a given address) and reports
every change with timestamps and the window between the last unsolicited
frame on the old mapping and the first on the new one. It only observes;
it never binds anything in the config (that is `rsh huawei info`).

Events (dicts, also logged as kind="addr_event" records):
  ADDR-NEG     phase=3001|3002 serial=<hex> from_addr=<n>
  ADDR-LEARN   serial=<hex> addr=<n> source=negotiation|info
  ADDR-CHANGE  serial=<hex> <old> -> <new> (+ last unsolicited on old)
  ADDR-WINDOW  serial=<hex> gap between last frame on old and first on new
  ADDR-TAKEN   addr <n> now answers as serial X (was Y)
"""
from __future__ import annotations


class AddrWatch:
    def __init__(self):
        self.map: dict[str, int] = {}              # serial -> addr
        self.by_addr: dict[int, str] = {}          # addr -> serial
        self.unsol_count: dict[int, int] = {}
        self.last_unsol_t: dict[int, float] = {}
        self.first_unsol_t: dict[int, float] = {}
        self.events: list[dict] = []
        self._pending: dict[str, dict] = {}        # serial -> window being measured
        self.neg_frames = 0

    # ------------------------------------------------------------- feed
    def feed(self, dec: dict | None, t: float) -> list[dict]:
        """One decoded Huawei frame at time t; returns the events it caused."""
        if not dec or dec.get("proto") != "huawei-r48xx":
            return []
        out: list[dict] = []
        kind = dec.get("kind")
        addr = dec.get("addr")
        if kind == "unsolicited":
            self.unsol_count[addr] = self.unsol_count.get(addr, 0) + 1
            self.last_unsol_t[addr] = t
            self.first_unsol_t.setdefault(addr, t)
            for serial, w in list(self._pending.items()):
                if w["new"] == addr:
                    gap = None if w["t_last_old"] is None else round(t - w["t_last_old"], 3)
                    out.append(self._ev(t, "ADDR-WINDOW", serial=serial, old=w["old"], new=addr,
                                        t_last_old=w["t_last_old"], t_first_new=round(t, 4),
                                        gap_s=gap,
                                        text=f"ADDR-WINDOW serial={serial} {w['old']} -> {addr}: "
                                             f"last unsolicited on {w['old']} to first on {addr} "
                                             f"= {gap} s"))
                    del self._pending[serial]
        elif kind == "addr_negotiation":
            self.neg_frames += 1
            phase, serial = dec.get("phase"), dec.get("serial_hex")
            out.append(self._ev(t, "ADDR-NEG", phase=phase, serial=serial, from_addr=addr,
                                text=f"ADDR-NEG phase={phase} serial={serial} from_addr={addr}"))
            if phase == "3002" and serial:
                out += self._learn(serial, addr, t, "negotiation")
        elif kind == "info" and dec.get("name") == "serial" and dec.get("serial_hex"):
            out += self._learn(dec["serial_hex"], addr, t, "info")
        return out

    def _ev(self, t: float, event: str, **kw) -> dict:
        e = {"t": round(t, 4), "event": event, **kw}
        self.events.append(e)
        return e

    def _learn(self, serial: str, addr: int, t: float, source: str) -> list[dict]:
        serial = serial.upper()
        out = []
        old = self.map.get(serial)
        if old is None:
            out.append(self._ev(t, "ADDR-LEARN", serial=serial, addr=addr, source=source,
                                text=f"ADDR-LEARN serial={serial} addr={addr} ({source})"))
        elif old != addr:
            t_last_old = self.last_unsol_t.get(old)
            out.append(self._ev(t, "ADDR-CHANGE", serial=serial, old=old, new=addr, source=source,
                                t_last_old=t_last_old,
                                unsol_on_old=self.unsol_count.get(old, 0),
                                since_last_old_s=None if t_last_old is None
                                else round(t - t_last_old, 3),
                                text=f"ADDR-CHANGE serial={serial} {old} -> {addr} ({source}; "
                                     f"{self.unsol_count.get(old, 0)} unsolicited on {old}, last "
                                     f"{None if t_last_old is None else round(t - t_last_old, 3)}"
                                     f" s ago)"))
            self._pending[serial] = {"old": old, "new": addr, "t_last_old": t_last_old}
        self.map[serial] = addr
        prev = self.by_addr.get(addr)
        if prev and prev != serial:
            out.append(self._ev(t, "ADDR-TAKEN", addr=addr, serial=serial, was=prev,
                                text=f"ADDR-TAKEN addr {addr} now answers as {serial} (was {prev}) "
                                     f"- a command to {addr} reaches a different unit"))
        self.by_addr[addr] = serial
        for a, s in list(self.by_addr.items()):
            if s == serial and a != addr:
                del self.by_addr[a]
        return out

    def summary(self) -> dict:
        return {"serial_to_addr": dict(self.map), "unsolicited_per_addr": dict(self.unsol_count),
                "negotiation_frames": self.neg_frames,
                "changes": [e for e in self.events if e["event"] == "ADDR-CHANGE"],
                "windows": [e for e in self.events if e["event"] == "ADDR-WINDOW"]}
