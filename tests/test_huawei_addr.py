"""Huawei address negotiation / address-change watch (Phase B B5.1, B5.4,
B5.5): the simulator's negotiation sequence, the AddrWatch on decoded
frames, and `rsh sniff` on a virtual bus emitting ADDR-NEG / ADDR-CHANGE
records with timestamps."""
import threading
import time

from rs485harness.core import canbus as C
from rs485harness.core.session import SessionWriter, read_session
from rs485harness.protocols.huawei_r48xx import ids as I
from rs485harness.protocols.huawei_r48xx import regs as R
from rs485harness.simulators.huawei_psu_sim import HuaweiPsuSimulator
from rs485harness.analyzer.huawei_addr import AddrWatch
from rs485harness.analyzer.sniff import Sniffer

SERIAL_A, SERIAL_B = "0000000000A1", "0000000000B2"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, dt, step=0.05, real=0.004):
        end = self.now + dt
        while self.now < end:
            self.now = min(end, self.now + step)
            time.sleep(real)


def neg(phase: int, serial: str, addr: int) -> dict:
    cid = I.HuaweiId(I.PROTO_PSU, addr, I.CMD_ADDR, False).pack()
    return R.decode(C.CanMsg(0.0, cid, bytes([0x30, phase]) + bytes.fromhex(serial), ext=True))


def unsol(addr: int) -> dict:
    cid = I.HuaweiId(I.PROTO_PSU, addr, I.CMD_UNSOL, False).pack()
    return R.decode(C.CanMsg(0.0, cid, bytes.fromhex("0001000000000062"), ext=True))


def test_addr_watch_learns_changes_and_windows():
    w = AddrWatch()
    assert neg(1, SERIAL_A, 0)["kind"] == "addr_negotiation" and neg(1, SERIAL_A, 0)["phase"] == "3001"
    assert I.HuaweiId(I.PROTO_PSU, 0, I.CMD_ADDR, False).pack() == 0x1000107E
    assert I.HuaweiId(I.PROTO_PSU, 1, I.CMD_ADDR, False).pack() == 0x1001107E
    evs = w.feed(neg(1, SERIAL_A, 0), 10.0)
    assert [e["event"] for e in evs] == ["ADDR-NEG"] and evs[0]["from_addr"] == 0
    evs = w.feed(neg(2, SERIAL_A, 1), 10.5)
    assert [e["event"] for e in evs] == ["ADDR-NEG", "ADDR-LEARN"] and w.map == {SERIAL_A: 1}
    for t in (11.0, 11.4, 11.8):
        assert w.feed(unsol(1), t) == []
    # info reply on addr 2 for serial B: learned from info
    info = R.decode(C.CanMsg(0.0, I.reply_id(I.CMD_INFO, 2, True),
                             bytes([0, 2]) + bytes.fromhex(SERIAL_B), ext=True))
    evs = w.feed(info, 12.0)
    assert evs[0]["event"] == "ADDR-LEARN" and evs[0]["source"] == "info" and w.by_addr[2] == SERIAL_B
    # B restarts and ends up on 1, A moves to 2
    evs = w.feed(neg(2, SERIAL_B, 1), 20.0)
    kinds = [e["event"] for e in evs]
    assert kinds == ["ADDR-NEG", "ADDR-CHANGE", "ADDR-TAKEN"], kinds
    ch = evs[1]
    assert ch["old"] == 2 and ch["new"] == 1 and ch["t_last_old"] is None       # no unsolicited on 2 yet
    assert evs[2]["was"] == SERIAL_A and evs[2]["addr"] == 1
    evs = w.feed(neg(2, SERIAL_A, 2), 20.3)
    ch = [e for e in evs if e["event"] == "ADDR-CHANGE"][0]
    assert ch["old"] == 1 and ch["new"] == 2 and ch["t_last_old"] == 11.8
    assert ch["since_last_old_s"] == 8.5 and ch["unsol_on_old"] == 3
    assert w.map == {SERIAL_A: 2, SERIAL_B: 1} and w.by_addr == {1: SERIAL_B, 2: SERIAL_A}
    # first unsolicited on the new mapping closes the window
    evs = w.feed(unsol(2), 21.0)
    assert evs and evs[0]["event"] == "ADDR-WINDOW" and evs[0]["gap_s"] == 9.2
    assert evs[0]["serial"] == SERIAL_A
    s = w.summary()
    assert len(s["changes"]) == 2 and len(s["windows"]) == 1 and s["negotiation_frames"] == 4


def test_sniff_learns_serial_to_addr_from_negotiation(tmp_path):
    clk = FakeClock()
    bus = C.VirtualCanBus(f"neg-{int(time.time() * 1000)}")
    sess = SessionWriter(tmp_path / "sims.jsonl")
    sims = [HuaweiPsuSimulator(bus.end(f"psu{a}", I.BITRATE), sess, addr=a, serial_hex=s,
                               chan=f"psu{a}", unsol_period_s=0.2, clock=clk, tick_s=0.005,
                               negotiate_on_start=True)
            for a, s in ((1, SERIAL_A), (2, SERIAL_B))]
    for s in sims:
        s.start()
    cap = SessionWriter(tmp_path / "sniff.jsonl")
    events = []
    sn = Sniffer({"ch1": bus.end("tap", I.BITRATE, listen_only=True)}, cap,
                 on_event=events.append, markers=False, quiet=True)
    th = threading.Thread(target=sn.run, daemon=True)
    th.start()
    try:
        clk.advance(4.0)                 # 3 x 0.13 + 8 x 0.35 = 3.2 s of negotiation
        time.sleep(0.1)
    finally:
        sn.stop(); th.join(timeout=2)
        for s in sims:
            s.stop()
        sess.close(); cap.close()
    assert sn.addr_watch.map == {SERIAL_A: 1, SERIAL_B: 2}
    recs = [r for r in read_session(tmp_path / "sniff.jsonl") if r.get("kind") == "addr_event"]
    negs = [r for r in recs if r["event"] == "ADDR-NEG"]
    assert len(negs) == 2 * (3 + 8), len(negs)
    assert [r["phase"] for r in negs if r["serial"] == SERIAL_A] == ["3001"] * 3 + ["3002"] * 8
    assert all(r["from_addr"] == 0 for r in negs if r["phase"] == "3001")
    assert {r["from_addr"] for r in negs if r["phase"] == "3002"} == {1, 2}
    learns = [r for r in recs if r["event"] == "ADDR-LEARN"]
    assert {(r["serial"], r["addr"]) for r in learns} == {(SERIAL_A, 1), (SERIAL_B, 2)}
    assert all(r["t"] > 0 and r["chan"] == "ch1" for r in recs)
    assert [r["t"] for r in recs] == sorted(r["t"] for r in recs)
    assert any("ADDR-NEG phase=3001" in e for e in events) and sn.stats["ch1"]["addr_events"] >= 24
    summ = [r for r in read_session(tmp_path / "sniff.jsonl") if r.get("kind") == "summary"]
    assert summ and summ[-1]["summary"]["addr_watch"]["serial_to_addr"] == {SERIAL_A: 1, SERIAL_B: 2}
    assert not [r for r in recs if r["event"] == "ADDR-CHANGE"]


def test_sniff_reports_addr_change_with_timestamps(tmp_path):
    clk = FakeClock()
    bus = C.VirtualCanBus(f"chg-{int(time.time() * 1000)}")
    sess = SessionWriter(tmp_path / "sims.jsonl")
    sim = HuaweiPsuSimulator(bus.end("psu", I.BITRATE), sess, addr=1, serial_hex=SERIAL_A,
                             unsol_period_s=0.2, clock=clk, tick_s=0.005, negotiate_on_start=True)
    sim.start()
    cap = SessionWriter(tmp_path / "sniff.jsonl")
    events = []
    sn = Sniffer({"ch1": bus.end("tap", I.BITRATE, listen_only=True)}, cap,
                 on_event=events.append, markers=False, quiet=True)
    th = threading.Thread(target=sn.run, daemon=True)
    th.start()
    try:
        clk.advance(4.0)
        assert sn.addr_watch.map == {SERIAL_A: 1}
        t_change_wall = time.time()
        sim.set_addr(2)                              # another unit joined: renegotiation
        clk.advance(3.5)
        time.sleep(0.1)
    finally:
        sn.stop(); th.join(timeout=2)
        sim.stop(); sess.close(); cap.close()
    recs = [r for r in read_session(tmp_path / "sniff.jsonl") if r.get("kind") == "addr_event"]
    ch = [r for r in recs if r["event"] == "ADDR-CHANGE"]
    assert len(ch) == 1 and ch[0]["serial"] == SERIAL_A and (ch[0]["old"], ch[0]["new"]) == (1, 2)
    assert ch[0]["t"] >= t_change_wall - 0.01 and ch[0]["unsol_on_old"] >= 10
    assert ch[0]["t_last_old"] is not None and 0 <= ch[0]["since_last_old_s"] < 2
    win = [r for r in recs if r["event"] == "ADDR-WINDOW"]
    assert len(win) == 1 and win[0]["gap_s"] is not None and win[0]["gap_s"] >= 0
    assert win[0]["t_first_new"] >= ch[0]["t"]
    assert sn.addr_watch.map == {SERIAL_A: 2} and sn.addr_watch.by_addr == {2: SERIAL_A}
    assert any("ADDR-CHANGE serial=0000000000A1 1 -> 2" in e for e in events)
    # the 30 02 frames after the change come from the new address only
    negs_after = [r for r in recs if r["event"] == "ADDR-NEG" and r["t"] >= ch[0]["t"]]
    assert negs_after and all(r["from_addr"] == 2 and r["phase"] == "3002" for r in negs_after)
    # the timeline shows the events
    from rs485harness.analyzer.timeline import build_timeline
    tl = build_timeline(tmp_path / "sniff.jsonl")
    addr_ev = [e for e in tl["events"] if e["kind"] == "addr"]
    assert any(e["event"] == "ADDR-CHANGE" for e in addr_ev)


def test_rsh_sniff_on_can_emits_addr_events(tmp_path, capsys):
    import json
    from rs485harness.cli import main
    name = f"clineg-{int(time.time() * 1000)}"
    bus = C.VirtualCanBus.get(name)
    sess = SessionWriter(tmp_path / "sim.jsonl")
    sim = HuaweiPsuSimulator(bus.end("psu", I.BITRATE), sess, addr=1, serial_hex=SERIAL_A,
                             unsol_period_s=0.1, negotiate_on_start=True)
    sim.start()
    try:
        rc = main(["sniff", "--ch1", f"can:virtual:{name}", "--baud", "125000", "--duration", "1.0",
                   "--no-markers", "--json", "--label", "B5.1-test",
                   "--session", str(tmp_path / "sn.jsonl")])
    finally:
        sim.stop(); sess.close()
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["stats"]["ch1"]["addr_events"] >= 4
    recs = list(read_session(tmp_path / "sn.jsonl"))
    negs = [r for r in recs if r.get("kind") == "addr_event" and r["event"] == "ADDR-NEG"]
    assert len(negs) >= 4 and negs[0]["phase"] == "3001" and negs[0]["from_addr"] == 0
    assert any(r.get("kind") == "note" and r.get("label") == "B5.1-test" for r in recs)
    assert any(r.get("kind") == "addr_event" and r["event"] == "ADDR-LEARN" and r["addr"] == 1
               for r in recs)
