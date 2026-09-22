"""Interactive human test runner: `rsh menu`.

Generic operation launcher: pick WHAT (op), WHICH (profile/options), WHERE
(ch1/ch2 -> COM port, or LOOP). Multiple operations run concurrently as
jobs (e.g. battery-sim on ch2 while identify runs on ch1). All jobs write
the same session JSONL files the CLI/LLM path uses.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .core import session as S
from .core.transport import SerialLink, loopback_pair
from .protocols.pylontech.dialects import BATTERY_PROFILES, INVERTER_PROFILES
from .simulators.battery_state import preset, PRESETS, apply_overrides
from .simulators.battery_sim import BatterySimulator
from .simulators.inverter_sim import InverterSimulator

CONFIG_PATH = Path(__file__).resolve().parents[1] / ".rsh-config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


class Job:
    _next_id = 1

    def __init__(self, name: str, target, stop, session_path, links=()):
        self.id = Job._next_id
        Job._next_id += 1
        self.name = name
        self.session_path = session_path
        self._stop = stop
        self._links = links
        self.live = threading.Event()  # print live lines?
        self.thread = threading.Thread(target=target, daemon=True,
                                       name=f"job-{self.id}")
        self.thread.start()

    def stop(self) -> None:
        self._stop()
        self.thread.join(timeout=3)
        for link in self._links:
            try:
                link.close()
            except Exception:
                pass

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()


class Menu:
    def __init__(self):
        self.cfg = load_config()
        self.jobs: list[Job] = []
        self.watch_all = True

    # ------------------------------------------------------------ helpers

    def ask(self, prompt: str, default: str | None = None) -> str:
        d = f" [{default}]" if default is not None else ""
        val = input(f"{prompt}{d}: ").strip()
        return val or (default or "")

    def choose(self, title: str, options: list[tuple[str, str]],
               default: str | None = None) -> str | None:
        print(f"\n{title}")
        for i, (key, desc) in enumerate(options, 1):
            mark = "*" if key == default else " "
            print(f"  {i}{mark} {key:22} {desc}")
        raw = input("choice (number/name, empty=default, q=cancel): ").strip()
        if raw.lower() == "q":
            return None
        if not raw:
            return default or options[0][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        for key, _ in options:
            if key == raw:
                return key
        print("  ?")
        return self.choose(title, options, default)

    def pick_port(self, label: str, allow_loop: bool = False) -> str | None:
        try:
            from serial.tools import list_ports
            ports = list(list_ports.comports())
        except Exception:
            ports = []
        opts = [(p.device, p.description) for p in ports]
        if allow_loop:
            opts.append(("LOOP", "in-process loopback pair (no hardware)"))
        if not opts:
            print("no serial ports found")
            return None
        default = self.cfg.get(f"port:{label}")
        if default not in [o[0] for o in opts]:
            default = opts[0][0]
        port = self.choose(f"{label}: which port?", opts, default)
        if port:
            self.cfg[f"port:{label}"] = port
            save_config(self.cfg)
        return port

    def on_event_for(self, job_name: str):
        def emit(line: str) -> None:
            if self.watch_all:
                print(f"  {job_name}| {line}", flush=True)
        return emit

    # ---------------------------------------------------------- operations

    def op_selftest(self) -> None:
        from .analyzer.selftest import run_selftest
        p1 = self.pick_port("self-test ch1")
        p2 = self.pick_port("self-test ch2")
        if not p1 or not p2:
            return
        print("\nWire ch1 and ch2 onto ONE bus: A-A, B-B (straight), GND-GND.")
        input("press Enter when wired ... ")
        sess = S.SessionWriter(S.new_session_path("selftest"))
        report = run_selftest(p1, p2, session=sess,
                              on_event=lambda l: print(l, flush=True))
        report["session"] = str(sess.path)
        report_path = S.save_report(sess.path, report, "selftest")
        sess.close()
        print(f"\n==> SELF-TEST {'PASSED' if report['passed'] else 'FAILED'}")
        print(f"session: {sess.path}\nreport:  {report_path}")

    def op_battery_sim(self) -> None:
        prof = self.choose("battery dialect profile",
                           [(k, v.description[:60]) for k, v in BATTERY_PROFILES.items()],
                           self.cfg.get("battery_profile", "ideal-pylontech"))
        if not prof:
            return
        pre = self.choose("battery data preset",
                          [(p, "") for p in PRESETS],
                          self.cfg.get("battery_preset", "ideal"))
        if not pre:
            return
        state = preset(pre)
        overrides = self.ask("state overrides key=value,key=value "
                             "(e.g. soc=20,current_a=-15; empty=none)", "")
        if overrides:
            try:
                apply_overrides(state, dict(kv.split("=", 1)
                                            for kv in overrides.split(",")))
            except (KeyError, ValueError) as e:
                print(f"  bad override: {e}")
                return
        port = self.pick_port("battery-sim")
        if not port:
            return
        self.cfg.update(battery_profile=prof, battery_preset=pre)
        save_config(self.cfg)
        sess = S.SessionWriter(S.new_session_path("battery-sim"))
        link = SerialLink(port, BATTERY_PROFILES[prof].baud, name="battery")
        sim = BatterySimulator(link, state, BATTERY_PROFILES[prof], sess,
                               on_event=self.on_event_for(f"bat@{port}"))
        job = Job(f"battery-sim[{prof}]@{port}", sim.run, sim.stop_event.set,
                  sess.path, links=(link,))
        self.jobs.append(job)
        print(f"started job #{job.id}: {job.name}  (session {sess.path})")

    def op_inverter_sim(self) -> None:
        prof = self.choose("inverter poll profile",
                           [(k, v.description[:60]) for k, v in INVERTER_PROFILES.items()],
                           self.cfg.get("inverter_profile", "voltronic-6x"))
        if not prof:
            return
        port = self.pick_port("inverter-sim")
        if not port:
            return
        self.cfg.update(inverter_profile=prof)
        save_config(self.cfg)
        sess = S.SessionWriter(S.new_session_path("inverter-sim"))
        link = SerialLink(port, INVERTER_PROFILES[prof].baud, name="inverter")
        sim = InverterSimulator(link, INVERTER_PROFILES[prof], sess,
                                on_event=self.on_event_for(f"inv@{port}"))
        job = Job(f"inverter-sim[{prof}]@{port}", sim.run, sim.stop_event.set,
                  sess.path, links=(link,))
        self.jobs.append(job)
        print(f"started job #{job.id}: {job.name}  (session {sess.path})")

    def op_identify(self) -> None:
        from .analyzer.identify import identify
        port = self.pick_port("identify")
        if not port:
            return
        passive = self.ask("passive only (no transmitting)? y/N", "n").lower() == "y"
        sess = S.SessionWriter(S.new_session_path("identify"))
        link = SerialLink(port, 9600, name="id")
        print("identify running (blocks this menu; other jobs keep running) ...")
        report = identify(link, sess, active=not passive,
                          on_event=lambda l: print("  " + l, flush=True))
        print("\nIDENTIFY RESULT:")
        for v in report["verdicts"]:
            print(f"  * {v}")
        report["session"] = str(sess.path)
        report_path = S.save_report(sess.path, report, "identify")
        print(f"session: {sess.path}\nreport:  {report_path}")
        sess.close()
        link.close()

    def op_discover(self) -> None:
        from .analyzer.discover import discover, format_discovery
        port = self.pick_port("discover")
        if not port:
            return
        baud = int(self.ask("baud", "9600"))
        sess = S.SessionWriter(S.new_session_path("discover"))
        link = SerialLink(port, baud, name="disc")
        print("discovery running (blocks this menu; other jobs keep "
              "running) ...")
        report = discover(link, sess,
                          on_event=lambda l: print("  " + l, flush=True))
        report["session"] = str(sess.path)
        report_path = S.save_report(sess.path, report, "discovery")
        print("\n" + format_discovery(report))
        print(f"session: {sess.path}\nreport:  {report_path}")
        sess.close()
        link.close()

    def op_sniff(self) -> None:
        from .analyzer.sniff import Sniffer
        p1 = self.pick_port("sniff ch1")
        if not p1:
            return
        p2 = self.pick_port("sniff ch2 (or same to skip)")
        baud = int(self.ask("baud", str(self.cfg.get("sniff_baud", 9600))))
        self.cfg["sniff_baud"] = baud
        save_config(self.cfg)
        sess = S.SessionWriter(S.new_session_path("sniff"))
        links = {"ch1": SerialLink(p1, baud, name="ch1")}
        if p2 and p2 != p1:
            links["ch2"] = SerialLink(p2, baud, name="ch2")
        sn = Sniffer(links, sess, on_event=self.on_event_for("sniff"))
        job = Job(f"sniff@{p1}{'+' + p2 if p2 and p2 != p1 else ''}",
                  sn.run, sn.stop, sess.path, links=tuple(links.values()))
        self.jobs.append(job)
        print(f"started job #{job.id}: {job.name}  (session {sess.path})")

    def op_bridge(self) -> None:
        from .analyzer.bridge import Bridge
        from .analyzer.translate import Translator
        p1 = self.pick_port("bridge ch1 (e.g. inverter side)")
        p2 = self.pick_port("bridge ch2 (e.g. battery side)")
        if not p1 or not p2:
            return
        baud = int(self.ask("baud", "9600"))
        tr = None
        if self.ask("enable translation fixes? y/N", "n").lower() == "y":
            tr = Translator()
            if self.ask("  fix cid2-echo responses? y/N", "n").lower() == "y":
                tr.fix_cid2_echo = {"ch1->ch2", "ch2->ch1"}
            adr = self.ask("  force response ADR toward ch1 (hex, empty=no)", "")
            if adr:
                tr.force_adr["ch2->ch1"] = int(adr, 16)
        sess = S.SessionWriter(S.new_session_path("bridge"))
        a = SerialLink(p1, baud, name="ch1")
        b = SerialLink(p2, baud, name="ch2")
        br = Bridge(a, b, sess, translator=tr,
                    on_event=self.on_event_for("bridge"))
        job = Job(f"bridge {p1}<->{p2}", br.run, br.stop, sess.path,
                  links=(a, b))
        self.jobs.append(job)
        print(f"started job #{job.id}: {job.name}  (session {sess.path})")

    def op_send(self) -> None:
        from .scripting import build_frame
        from .core.transport import FrameAccumulator
        import time as _t
        port = self.pick_port("send")
        if not port:
            return
        baud = int(self.ask("baud", "9600"))
        raw = self.ask("raw hex/~ASCII frame (empty = build from fields)", "")
        if raw:
            spec = {"hex": raw}
            expect = self.ask("decode reply as CID2 (hex, empty=skip)", "")
        else:
            cid2 = self.ask("CID2 (hex)", "61")
            spec = {"ver": self.ask("VER", "20"), "adr": self.ask("ADR", "02"),
                    "cid2": cid2, "info": self.ask("INFO hex", "")}
            expect = cid2
        sess = S.SessionWriter(S.new_session_path("send"))
        link = SerialLink(port, baud, name="send")
        try:
            frame = build_frame(spec)
        except Exception as e:
            print(f"  build error: {e}"); sess.close(); link.close(); return
        from .protocols.pylontech import frames as F, payloads as P
        link.flush_input(); t0 = _t.perf_counter(); link.write(frame)
        sess.log("send", "tx", frame, "frame", decoded=F.parse_frame(frame).to_dict())
        acc = FrameAccumulator(); reply = None; deadline = t0 + 1.5
        while _t.perf_counter() < deadline and reply is None:
            c = link.read(timeout=0.1)
            if c:
                for ev in acc.feed(c):
                    if ev.kind == "frame":
                        reply = F.parse_frame(ev.data)
                        sess.log("send", "rx", ev.data, "frame", decoded=reply.to_dict())
        print(f"  TX {frame.hex().upper()}")
        if reply:
            print(f"  RX {reply.describe('response')}")
            if expect:
                print("  payload:", P.decode_response_info(int(expect, 16), reply.info))
        else:
            print("  RX (timeout)")
        print(f"  session: {sess.path}")
        sess.close(); link.close()

    def op_script(self) -> None:
        from .scripting import load_scenario, run_scenario, ScenarioError
        import glob
        found = sorted(glob.glob("docs/examples/*.json"))
        if found:
            print("example scenarios: " + ", ".join(found))
        path = self.ask("scenario .json path")
        if not path:
            return
        port = self.pick_port("script")
        if not port:
            return
        try:
            sc = load_scenario(path)
        except ScenarioError as e:
            print(f"  {e}"); return
        baud = sc.get("baud", 9600)
        sess = S.SessionWriter(S.new_session_path("script"))
        link = SerialLink(port, baud, name="script")
        dur = None
        if sc.get("mode") == "responder":
            d = self.ask("responder duration seconds (empty=until stop)", "")
            dur = float(d) if d else None
        print(f"running '{sc.get('name','?')}' mode={sc.get('mode')} ...")
        try:
            stats = run_scenario(link, sc, sess, duration=dur,
                                 on_event=lambda l: print("  " + l, flush=True))
        except (ScenarioError, ValueError) as e:
            print(f"  error: {e}"); sess.close(); link.close(); return
        report_path = S.save_report(sess.path, stats, "script")
        print(f"stats: {stats}\nsession: {sess.path}\nreport: {report_path}")
        sess.close(); link.close()

    def op_decode(self) -> None:
        from .protocols.pylontech import frames as F
        from .protocols.pylontech import payloads as P
        raw = self.ask("paste frame (hex bytes or ~...ASCII)")
        if not raw:
            return
        try:
            if raw.startswith("~"):
                data = raw.encode()
                if not data.endswith(b"\r"):
                    data += b"\r"
            else:
                data = bytes.fromhex(raw.replace(" ", ""))
        except ValueError as e:
            print(f"  bad input: {e}")
            return
        pf = F.parse_frame(data)
        print("  " + pf.describe("auto"))
        if pf.info:
            req = self.ask("decode payload as answer to CID2 (hex, empty=skip)", "")
            if req:
                print("  " + str(P.decode_response_info(int(req, 16), pf.info)))

    def op_diagnose(self) -> None:
        from .analyzer.diagnose import analyze, format_report
        caps = sorted(S.CAPTURE_DIR.glob("*.jsonl"))
        if not caps:
            print("no captures yet")
            return
        latest = caps[-1]
        path = self.ask("session file", str(latest))
        print(format_report(analyze(path)))

    def op_demo(self) -> None:
        sess = S.SessionWriter(S.new_session_path("demo"))
        end_a, end_b = loopback_pair("inverter", "battery")
        bat = BatterySimulator(end_b, preset("ideal"),
                               BATTERY_PROFILES["ideal-pylontech"], sess,
                               on_event=self.on_event_for("bat(LOOP)"))
        inv = InverterSimulator(end_a, INVERTER_PROFILES["voltronic-6x"], sess,
                                on_event=self.on_event_for("inv(LOOP)"))
        jb = Job("demo-battery(LOOP)", bat.run, bat.stop_event.set, sess.path)
        job = Job("demo-inverter(LOOP)", inv.run, inv.stop_event.set, sess.path)
        self.jobs += [jb, job]
        print(f"started loopback demo jobs #{jb.id}/#{job.id} (session {sess.path})")

    def op_jobs(self) -> None:
        if not self.jobs:
            print("no jobs")
            return
        for j in self.jobs:
            state = "running" if j.alive else "finished"
            print(f"  #{j.id} [{state}] {j.name}  session={j.session_path}")
        which = self.ask("stop job # (empty=none, a=all)", "")
        if which == "a":
            for j in self.jobs:
                j.stop()
            self.jobs = []
        elif which.isdigit():
            for j in self.jobs:
                if j.id == int(which):
                    j.stop()
                    self.jobs.remove(j)
                    print(f"stopped #{j.id}")
                    break

    # --------------------------------------------------------------- loop

    OPS = [
        ("selftest", "adapter self-test (ch1+ch2 cross-wired)", op_selftest),
        ("identify", "'who is there?' scan on a channel", op_identify),
        ("discover", "full capability + state inventory of a responder", op_discover),
        ("battery-sim", "simulate a battery (choose profile + channel)", op_battery_sim),
        ("inverter-sim", "simulate an inverter (choose profile + channel)", op_inverter_sim),
        ("sniff", "passive capture on 1-2 channels", op_sniff),
        ("bridge", "forward ch1<->ch2 (+ optional translation)", op_bridge),
        ("demo", "loopback demo: sim vs sim, no hardware", op_demo),
        ("send", "send one frame, decode the reply", op_send),
        ("script", "run a JSON scenario (master seq / responder rules)", op_script),
        ("decode", "decode a pasted frame", op_decode),
        ("diagnose", "run hypothesis checks on a capture", op_diagnose),
        ("jobs", "list / stop running jobs", op_jobs),
    ]

    def run(self) -> int:
        print("rs485harness test runner - jobs keep running while you "
              "navigate; 'w' toggles live frame output; 'q' quits.")
        while True:
            running = sum(1 for j in self.jobs if j.alive)
            print(f"\n--- menu (jobs running: {running}, live output: "
                  f"{'on' if self.watch_all else 'off'}) ---")
            for i, (name, desc, _fn) in enumerate(self.OPS, 1):
                print(f"  {i:2}. {name:13} {desc}")
            choice = input("op (number/name, w=toggle output, q=quit): ").strip()
            if choice.lower() == "q":
                for j in self.jobs:
                    j.stop()
                return 0
            if choice.lower() == "w":
                self.watch_all = not self.watch_all
                continue
            fn = None
            if choice.isdigit() and 1 <= int(choice) <= len(self.OPS):
                fn = self.OPS[int(choice) - 1][2]
            else:
                for name, _d, f in self.OPS:
                    if name == choice:
                        fn = f
                        break
            if fn is None:
                continue
            try:
                fn(self)
            except KeyboardInterrupt:
                print("\n(cancelled)")
            except Exception as e:
                print(f"error: {e}")


def run_menu() -> int:
    return Menu().run()
