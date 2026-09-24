"""Adapter self-test: verify the 2-channel RS485 adapter with ch1 and ch2
wired to the same bus (A-A, B-B, GND-GND - a straight bus join).

Checks per baud rate:
- byte-exact transfer ch1->ch2 and ch2->ch1 (pattern incl. full 0x00-0xFF)
- golden Pylontech frame end-to-end
- per-channel TX echo behaviour (feeds echo_suppress used everywhere else)
- transfer latency vs theoretical wire time (flags FTDI 16 ms latency timer)
"""
from __future__ import annotations

import time

from ..core.transport import SerialLink
from ..protocols.pylontech.frames import encode_frame

GOLDEN_FRAME = bytes.fromhex("7E32303031343634324530303230314644"
                             "33350D")  # ~20014642E00201FD35\r


def _transfer(tx, rx, payload: bytes,
              timeout: float = 2.0) -> tuple[bool, bytes, float]:
    rx.flush_input()
    tx.flush_input()
    t0 = time.perf_counter()
    tx.write(payload)
    got = b""
    deadline = t0 + timeout
    while len(got) < len(payload) and time.perf_counter() < deadline:
        chunk = rx.read(timeout=0.1)
        if chunk:
            got += chunk.data
    elapsed = time.perf_counter() - t0
    return got == payload, got, elapsed


def run_selftest(port1: str | None = None, port2: str | None = None,
                 bauds=(9600, 19200, 115200), on_event=None,
                 links=None, session=None) -> dict:
    """links=(link1, link2) runs the same checks over pre-opened Links
    (e.g. a VirtualAnalyzer with cross_wire()) instead of COM ports.
    session: optional SessionWriter - every check result is logged as a
    note so the run is analyzable later."""
    on_event = on_event or (lambda line: None)
    report: dict = {"port1": port1, "port2": port2, "bauds": {}, "checks": [],
                    "passed": True}
    if session:
        session.note(f"selftest start port1={port1} port2={port2} "
                     f"bauds={list(bauds)}")

    def check(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        # warn=True: advisory only (does not fail the run); e.g. latency timer.
        tag = "PASS" if ok else ("WARN" if warn else "FAIL")
        report["checks"].append({"name": name, "ok": ok, "detail": detail,
                                 "severity": "warn" if (warn and not ok) else
                                 ("pass" if ok else "fail")})
        if not ok and not warn:
            report["passed"] = False
        if session:
            session.note(f"selftest [{tag}] {name}"
                         + (f" - {detail}" if detail else ""))
        on_event(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""))

    own_links = links is None
    if links is not None:
        l1, l2 = links
    else:
        try:
            l1 = SerialLink(port1, name="ch1")
            l2 = SerialLink(port2, name="ch2")
        except Exception as e:  # serial.SerialException etc.
            report["passed"] = False
            report["error"] = f"could not open ports: {e}"
            on_event(f"  [FAIL] open ports - {e}")
            return report

    try:
        # echo detection once, at default baud, on the (quiet) joined bus
        on_event("detecting TX echo per channel ...")
        echo1 = l1.detect_echo()
        l2.flush_input()  # ch2 received ch1's probe - clear it
        echo2 = l2.detect_echo()
        l1.flush_input()
        report["echo"] = {"ch1": echo1, "ch2": echo2}
        check("echo detection", True,
              f"ch1 echo={echo1}, ch2 echo={echo2} (informational; "
              f"suppression configured accordingly)")

        pattern = bytes(range(256)) + b"stress-TEST-0123456789" * 4
        for baud in bauds:
            l1.set_baud(baud)
            l2.set_baud(baud)
            l1.flush_input()
            l2.flush_input()
            wire_time = len(pattern) * 10 / baud
            bres: dict = {}
            on_event(f"baud {baud}:")

            ok, got, dt = _transfer(l1, l2, pattern)
            bres["ch1_to_ch2"] = {"ok": ok, "elapsed_s": round(dt, 4),
                                  "got_bytes": len(got)}
            check(f"{baud} ch1->ch2 pattern", ok,
                  f"{len(got)}/{len(pattern)}B in {dt * 1000:.1f}ms")
            overhead_ms = (dt - wire_time) * 1000
            if ok and overhead_ms > 40:
                check(f"{baud} ch1->ch2 latency", False, warn=True,
                      detail=f"overhead {overhead_ms:.0f}ms - check USB latency "
                      f"timer (FTDI: Device Manager > COM port > Advanced > "
                      f"Latency Timer = 1 ms, then replug)")
            ok, got, dt = _transfer(l2, l1, pattern)
            bres["ch2_to_ch1"] = {"ok": ok, "elapsed_s": round(dt, 4),
                                  "got_bytes": len(got)}
            check(f"{baud} ch2->ch1 pattern", ok,
                  f"{len(got)}/{len(pattern)}B in {dt * 1000:.1f}ms")

            ok, got, dt = _transfer(l1, l2, GOLDEN_FRAME)
            if session:
                session.log("ch1", "tx", GOLDEN_FRAME, "frame",
                            note=f"selftest golden frame @ {baud}")
                session.log("ch2", "rx", got, "frame" if ok else "raw",
                            note=f"selftest golden frame @ {baud} "
                                 f"{'ok' if ok else 'CORRUPT'}")
            bres["golden_frame"] = {"ok": ok, "elapsed_ms": round(dt * 1000, 1)}
            check(f"{baud} golden Pylontech frame", ok,
                  f"{dt * 1000:.1f}ms" if ok else f"got {got.hex().upper()}")

            # small-frame turnaround estimate (request-sized transfer)
            small = encode_frame(0x20, 0x01, 0x46, 0x61, "")
            ok, _, dt = _transfer(l1, l2, small)
            if ok:
                bres["small_frame_ms"] = round(dt * 1000, 1)
                # A small frame's delivery is dominated by the USB latency
                # timer, not wire time. Overhead > ~6ms is the FTDI 16ms
                # default -> flag it (matters for bridge/MITM turnaround).
                small_wire_ms = len(small) * 10 / baud * 1000
                overhead_ms = dt * 1000 - small_wire_ms
                bres["small_frame_overhead_ms"] = round(overhead_ms, 1)
                if overhead_ms > 6:
                    check(f"{baud} small-frame latency", False, warn=True,
                          detail=f"{overhead_ms:.0f}ms overhead on a "
                          f"{len(small)}B frame - USB latency timer likely at "
                          f"default 16ms. Set it to 1ms (Device Manager > COM "
                          f"port > Port Settings > Advanced > Latency Timer = "
                          f"1) and replug. Harmless for polling/sniffing; adds "
                          f"per-turn delay in bridge/MITM mode.")
            report["bauds"][str(baud)] = bres
    finally:
        if own_links:
            l1.close()
            l2.close()

    if session:
        session.note(f"selftest done: "
                     f"{'PASSED' if report['passed'] else 'FAILED'} "
                     f"echo={report.get('echo')}")
    on_event(f"SELF-TEST {'PASSED' if report['passed'] else 'FAILED'}")
    return report
