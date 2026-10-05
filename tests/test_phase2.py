"""P1/P2 infrastructure: policy engine invariants, modify-mode bridge with
audit + fail-safe, byte-level transparent forwarding, markers, timeline."""
import json
import threading
import time

from rs485harness.core.session import SessionWriter, read_session
from rs485harness.core.virtual import VirtualAnalyzer
from rs485harness.protocols.pylontech import frames as F
from rs485harness.protocols.pylontech import payloads as P
from rs485harness.protocols.pylontech.dialects import (BATTERY_PROFILES,
                                                       INVERTER_PROFILES,
                                                       DialectProfile,
                                                       InverterPollProfile)
from rs485harness.simulators.battery_state import preset
from rs485harness.simulators.battery_sim import BatterySimulator
from rs485harness.simulators.inverter_sim import InverterSimulator
from rs485harness.analyzer.bridge import Bridge
from rs485harness.analyzer.translate import Translator
from rs485harness.analyzer.policy import (PolicyEngine, CapPolicy, PayloadPolicy,
                                          decode_fields, rebuild_info, make_policy)
from rs485harness.analyzer.timeline import TimelineConfig, build_timeline, export


def resp63(cvl_v=56.0, dvl_v=46.0, ccl_a=100.0, dcl_a=100.0, status=0xC0):
    info = (f"{int(cvl_v * 1000):04X}{int(dvl_v * 1000):04X}"
            f"{int(ccl_a * 10) & 0xFFFF:04X}{int(dcl_a * 10) & 0xFFFF:04X}{status:02X}")
    return F.parse_frame(F.encode_frame(0x20, 0x02, 0x46, 0x00, info))


# ------------------------------------------------------------ policy engine

def test_policy_cap_and_rebuild_roundtrip():
    eng = PolicyEngine(CapPolicy(ccl_max=40))
    pf = resp63(ccl_a=100)
    new_info, audit = eng.process(pf, 0x63, now=1.0)
    assert new_info is not None
    f = decode_fields(0x63, new_info)
    assert f["ccl"] == 40.0 and f["dcl"] == 100.0 and f["cvl"] == 56.0
    assert f["status"] == 0xC0                  # untouched
    assert audit["modified"] == {"ccl": 40.0}
    # bytes outside the CCL field are identical
    assert new_info[:8] == pf.info[:8] and new_info[12:] == pf.info[12:]


def test_policy_cannot_raise_or_touch_locked():
    class Evil(PayloadPolicy):
        name = "evil"
        def apply(self, ctx):
            return {"ccl": 500.0, "dcl": 1.0, "status": 0x00, "cvl": 99.0}
    eng = PolicyEngine(Evil())
    pf = resp63(ccl_a=40, cvl_v=56.0)
    new_info, audit = eng.process(pf, 0x63, now=1.0)
    assert new_info is None                      # everything clamped/ignored -> no change
    assert any("clamped ccl" in n for n in audit["notes"])
    assert any("ignored 'dcl'" in n for n in audit["notes"])
    assert any("ignored 'status'" in n for n in audit["notes"])
    assert eng.stats["clamped"] >= 2


def test_policy_skips_bad_frames_and_resets_on_loss():
    eng = PolicyEngine(CapPolicy(ccl_max=10), loss_timeout_s=5.0)
    bad = F.parse_frame(b"~20024600D012E100AF0003B603B6C0F9B6\r")  # corrupted checksum
    assert not bad.ok
    assert eng.process(bad, 0x63, now=1.0)[0] is None
    # unsupported command never touched
    assert eng.process(resp63(), 0x61, now=1.0)[0] is None
    eng.process(resp63(), 0x63, now=10.0)
    _, audit = eng.process(resp63(), 0x63, now=30.0)   # 20s gap > loss timeout
    assert "reset" in audit and eng.stats["resets"] == 1


def test_make_policy_parsing():
    p = make_policy("cap:ccl_max=40,cvl_max=56.5")
    assert isinstance(p, CapPolicy) and p.ccl_max == 40 and p.cvl_max == 56.5
    try:
        make_policy("nonexistent")
        assert False
    except ValueError:
        pass


# ------------------------------------------------------------ bridge modify mode

def _run_bridge(tmp_path, translator, duration=1.2):
    va = VirtualAnalyzer()
    inv_end = va.bus1.end("inverter")
    bat_end = va.bus2.end("battery")
    sess = SessionWriter(tmp_path / "br.jsonl")
    prof = DialectProfile(**{**BATTERY_PROFILES["ideal-pylontech"].to_dict(),
                             "response_delay_s": 0.0})
    state = preset("ideal")                       # CCL 100 A, CVL 56.0 V
    bat = BatterySimulator(bat_end, state, prof, sess, chan="bat")
    ip = InverterPollProfile(**{**INVERTER_PROFILES["voltronic-6x"].to_dict(),
                                "period_s": 0.05, "response_timeout_s": 0.5})
    inv = InverterSimulator(inv_end, ip, sess, chan="inv")
    br = Bridge(va.ch1, va.ch2, sess, translator=translator, markers=False)
    bat.start()
    bt = threading.Thread(target=br.run, daemon=True); bt.start()
    res63 = None
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        r = inv.poll_once(0x63)
        if r["status"] == "response":
            res63 = r
        inv.poll_once(0x61)
    br.stop(); bt.join(timeout=3); bat.stop(); sess.close()
    return br, res63, list(read_session(tmp_path / "br.jsonl"))


def test_bridge_modify_caps_ccl_with_audit(tmp_path):
    tr = Translator(engine=PolicyEngine(CapPolicy(ccl_max=50)))
    br, res63, recs = _run_bridge(tmp_path, tr)
    assert br.mode == "modify"
    assert res63 is not None
    assert res63["payload"]["charge_current_limit_a"] == 50.0      # inverter sees the cap
    assert res63["payload"]["status"]["charge_enable"] is True      # untouched
    originals = [r for r in recs if r.get("note", "").startswith("ORIGINAL")]
    modified = [r for r in recs if r.get("modified")]
    assert originals and modified and len(originals) == len(modified)
    assert originals[0]["audit"]["modified"] == {"ccl": 50.0}
    assert all(r.get("mode") == "modify" for r in originals + modified)
    assert originals[0]["flow"] == "BMS->INV"
    # the modified frame on the wire is checksum-valid
    assert F.parse_frame(bytes.fromhex(modified[0]["hex"])).ok


def test_bridge_modify_failsafe_forwards_original(tmp_path):
    class Boom(PayloadPolicy):
        name = "boom"
        def apply(self, ctx):
            raise RuntimeError("policy blew up")
    tr = Translator(engine=PolicyEngine(Boom()))
    br, res63, recs = _run_bridge(tmp_path, tr)
    assert res63 is not None
    assert res63["payload"]["charge_current_limit_a"] == 100.0     # original went through
    assert any(r.get("failsafe") for r in recs if r["kind"] == "note")
    assert not any(r.get("modified") for r in recs)


def test_bridge_transparent_is_byte_level():
    """A partial frame (no CR yet) must cross a transparent bridge
    immediately - no store-and-forward."""
    va = VirtualAnalyzer()
    inv_end = va.bus1.end("inverter"); bat_end = va.bus2.end("battery")
    import tempfile, pathlib
    sess = SessionWriter(pathlib.Path(tempfile.mkdtemp()) / "t.jsonl")
    br = Bridge(va.ch1, va.ch2, sess, markers=False)
    assert br.mode == "transparent"
    bt = threading.Thread(target=br.run, daemon=True); bt.start()
    time.sleep(0.1)
    inv_end.write(b"~2002466100")           # half a frame, no terminator
    chunk = bat_end.read(timeout=0.5)
    assert chunk is not None and chunk.data == b"~2002466100"
    inv_end.write(b"00FDAA\r")
    chunk2 = bat_end.read(timeout=0.5)
    assert chunk2 is not None and chunk2.data.endswith(b"\r")
    br.stop(); bt.join(timeout=2); sess.close()


# ------------------------------------------------------------ markers

def test_session_mark_and_arrival_timestamps(tmp_path):
    sess = SessionWriter(tmp_path / "m.jsonl")
    t_arrival = time.perf_counter() - 0.5           # bytes "arrived" 500 ms ago
    sess.log("ch1", "rx", b"~20\r", "frame", ts_mono=t_arrival, flow="BMS->INV", mode="tap")
    sess.mark("maxcell=3.550")
    sess.close()
    recs = list(read_session(tmp_path / "m.jsonl"))
    assert recs[0]["flow"] == "BMS->INV" and recs[0]["mode"] == "tap" and recs[0]["len"] == 4
    assert recs[1]["marker"] is True and recs[1]["note"] == "MARK maxcell=3.550"
    # arrival-time back-computation: the frame is ~0.5s older than the marker
    assert 0.4 < recs[1]["t"] - recs[0]["t"] < 0.7


# ------------------------------------------------------------ timeline

def _synthetic_capture(path):
    """Tap-style capture: inverter polls 0x61/0x62/0x63 at 1 Hz; Seplos-like
    CCL sequence 40,40,6,6,40,0 with current following, one OV-warn, one
    marker, one polling gap."""
    sess = SessionWriter(path)
    t = 1_700_000_000.0
    ccl_seq = [40, 40, 6, 6, 40, 0]
    cur_seq = [37.0, 36.5, 5.8, 5.5, 31.0, 0.0]
    cell_seq = [3.48, 3.50, 3.55, 3.552, 3.53, 3.50]
    ov_seq = [False, False, True, True, True, True]

    def req(cid2, ts):
        f = F.encode_frame(0x20, 0x02, 0x46, cid2, "")
        sess.log("ch1", "rx", f, "frame", decoded=F.parse_frame(f).to_dict(),
                 ts=ts, flow="INV->BMS", mode="tap")

    def resp(info, ts):
        f = F.encode_frame(0x20, 0x02, 0x46, 0x00, info)
        sess.log("ch1", "rx", f, "frame", decoded=F.parse_frame(f).to_dict(),
                 ts=ts, flow="BMS->INV", mode="tap")

    s = preset("ideal")
    for i in range(6):
        base = t + i * 3.0 + (4.0 if i >= 4 else 0.0)   # gap before the rebound
        s.current_a = cur_seq[i]; s.cell_mv = [int(cell_seq[i] * 1000)] * 16
        s.soc_percent = 95 + i
        req(0x61, base); resp(P.build_61(s), base + 0.02)
        s.alarms = {"cell_voltage_high_alarm": True} if ov_seq[i] else {}
        req(0x62, base + 1.0); resp(P.build_62(s), base + 1.02)
        s.charge_current_limit_da = ccl_seq[i] * 10
        req(0x63, base + 2.0); resp(P.build_63(s), base + 2.02)
        if i == 3:
            sess.log("-", "-", b"", kind="note", note="MARK maxcell=3.552 ovwarn=1",
                     marker=True, ts=base + 2.5)
    sess.close()


def test_timeline_events_findings_and_export(tmp_path):
    path = tmp_path / "cap.jsonl"
    _synthetic_capture(path)
    res = build_timeline(path, TimelineConfig(response_delay_s=0.0))
    kinds = [e["kind"] for e in res["events"]]
    assert "ccl-change" in kinds and "ov_warn-change" in kinds and "marker" in kinds
    ccl_changes = [(e["old"], e["new"]) for e in res["events"] if e["kind"] == "ccl-change"]
    assert ccl_changes == [(40.0, 6.0), (6.0, 40.0), (40.0, 0.0)]
    fk = {f["kind"] for f in res["findings"]}
    assert "ccl-large-increase" in fk                 # the 6 -> 40 rebound
    assert "current-exceeds-ccl" in fk                # 31 A while CCL was still 6 A
    assert "polling-gap" in fk                        # the inserted gap
    big = [f for f in res["findings"] if f["kind"] == "ccl-large-increase"][0]
    assert big["gaps"], "gap near the CCL rise should be attached (H3 context)"
    assert big["context"]["ov_warn"] is True
    st = res["stats"]
    assert st["requests"] == 18 and st["responses"] == 18
    assert st["response_latency_ms"]["median"] == 20.0
    assert st["ccl_update_interval_s"]["n"] == 3     # 40 -> 6 -> 40 -> 0
    # markers appear in the series; exports exist
    assert any(r["kind"] == "marker" for r in res["series"])
    paths = export(res, path)
    assert (tmp_path / "cap.timeline.csv").exists()
    assert (tmp_path / "cap.timeline.json").exists()
    rows = (tmp_path / "cap.timeline.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("t,t_rel,kind,cid2,soc")
    assert any(",marker," in r for r in rows)


def test_timeline_scaling_override(tmp_path):
    path = tmp_path / "cap2.jsonl"
    _synthetic_capture(path)
    res = build_timeline(path, TimelineConfig(ccl_factor=0.1, response_delay_s=0.0))
    assert [e["new"] for e in res["events"] if e["kind"] == "ccl-change"] == [0.6, 4.0, 0.0]
