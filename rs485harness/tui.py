"""`rsh tui` - interactive full-screen UI for the core functions.

Built on Textual (pure Python, runs in any terminal incl. Termux). Every
action calls the same analyzer code the CLI uses and writes the same
session files; the UI only adds a port picker, a live log and job control.

Keys: Enter/Space run the highlighted action, s stops the running job,
c clears the log, t cycles the theme, q quits. Touch/mouse works too.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from .core import session as S

try:
    from textual import events, on, work
    from textual.app import App, ComposeResult
    from textual.geometry import Size
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.theme import Theme
    from textual.widgets import (Footer, Header, Input, Label, ListItem,
                                 ListView, RichLog, Static)
except ImportError as e:  # pragma: no cover
    raise SystemExit("rsh tui needs Textual: pip install textual") from e


# Claude colour scheme (anthropic.com / claude.ai brand palette): terracotta
# accent on warm, near-black neutrals with ivory text.
CLAUDE_DARK = Theme(
    name="claude",
    primary="#D97757",      # Claude terracotta
    secondary="#CC785C",    # book cloth
    accent="#D4A27F",       # kraft
    warning="#E8B87A",
    error="#E05A4F",
    success="#7FA883",
    foreground="#F4F3EE",   # ivory
    background="#1F1E1D",   # warm near-black
    surface="#2A2927",
    panel="#353331",
    boost="#D9775718",
    dark=True,
    variables={
        "footer-key-foreground": "#D97757",
        "input-cursor-background": "#D97757",
        "block-cursor-background": "#D97757",
        "block-cursor-foreground": "#1F1E1D",
    },
)
CLAUDE_LIGHT = Theme(
    name="claude-light",
    primary="#C4623F",
    secondary="#CC785C",
    accent="#B5865A",
    warning="#B9792A",
    error="#C2423A",
    success="#4F7F55",
    foreground="#1F1E1D",
    background="#F4F3EE",
    surface="#EBE9E2",
    panel="#E1DED5",
    dark=False,
)

ACTIONS = [
    ("ports", "Ports", "list serial ports / USB adapter channels"),
    ("identify", "Identify", "who is there? passive listen + active probes on PORT"),
    ("discover", "Discover", "full command/state inventory of the responder on PORT"),
    ("dump", "Dump", "every unit: cells, temps, limits, cycles, serial, firmware"),
    ("readout", "Battery check", "identify + discover + dump into one report folder"),
    ("sniff", "Sniff", "passive tap on PORT (+PORT2), decoded CHANGE/STATE lines"),
    ("seplos", "Seplos monitor", "Seplos BMS 3.0 service socket on PORT (19200)"),
    ("timeline", "Timeline", "analyse the newest capture (or the path in PORT2)"),
    ("selftest", "Self-test", "adapter check: PORT and PORT2 cross-wired"),
    ("stop", "Stop job", "stop the running sniff / monitor"),
]


class ActionItem(ListItem):
    def __init__(self, key: str, title: str, hint: str):
        super().__init__(Label(f"[b]{title}[/b]\n[dim]{hint}[/dim]"))
        self.key = key


class RshTui(App):
    TITLE = "rs485harness"
    SUB_TITLE = "inverter <-> battery RS485 harness"
    # Responsive: below 80 columns (phone in portrait) everything stacks
    # vertically and the action list shows titles only.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (80, "-wide")]
    CSS = """
    Screen { layout: vertical; }
    #top { height: auto; padding: 0 1; background: $surface; }
    #top Input { width: 1fr; }
    #top Label { padding: 1 1 0 0; color: $primary; }
    #main { height: 1fr; }
    #actions { width: 34; border: tall $primary; background: $surface; }
    #actions > ListItem { padding: 0 1; }
    #actions > ListItem.-highlight { background: $primary 25%; }
    #log { width: 1fr; border: tall $secondary; background: $background; }
    #status { height: 1; padding: 0 1; background: $panel; color: $accent; }

    Screen.-narrow #top { layout: vertical; padding: 0; }
    Screen.-narrow #top Label { display: none; }
    Screen.-narrow #top Input { width: 1fr; }
    Screen.-narrow #main { layout: vertical; }
    Screen.-narrow #actions { width: 1fr; height: 12; }
    Screen.-narrow #actions > ListItem { padding: 0 1; height: 1; }
    Screen.-narrow .hint { display: none; }
    Screen.-narrow #log { width: 1fr; height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "stop_job", "Stop job"),
        Binding("c", "clear_log", "Clear log"),
        Binding("t", "cycle_theme", "Theme"),
        Binding("enter", "run_selected", "Run", show=True, priority=True),
    ]

    def __init__(self, port: str = "", port2: str = "", baud: int = 9600):
        super().__init__()
        self._port0, self._port20, self._baud0 = port, port2, baud
        self.job: dict | None = None       # {"name", "stop", "links", "thread"}

    # ----------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield Label("PORT")
            yield Input(value=self._port0, placeholder="COM5 / /dev/ttyUSB0 / usb:A", id="port")
            yield Label("PORT2")
            yield Input(value=self._port20, placeholder="optional (sniff ch2, selftest, timeline file)", id="port2")
            yield Label("BAUD")
            yield Input(value=str(self._baud0), placeholder="9600", id="baud")
        with Horizontal(id="main"):
            yield ListView(*[ActionItem(k, t, h) for k, t, h in ACTIONS], id="actions")
            yield RichLog(id="log", wrap=True, highlight=True, markup=True)
        yield Static("idle", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(CLAUDE_DARK)
        self.register_theme(CLAUDE_LIGHT)
        self.theme = "claude"
        # Termux does not always deliver SIGWINCH to a process started through
        # termux-usb (rotating the phone left half the screen black): poll
        # the terminal size and raise the resize ourselves when it changes.
        self.set_interval(0.5, self._poll_terminal_size)
        self.log_line(f"[b]rs485harness TUI[/b]  sessions -> {S.CAPTURE_DIR}")
        self.log_line("Pick an action (Enter), fill PORT first. "
                      "On Termux start via tools/termux/rsh-usb tui, then use usb:A / usb:B.")
        self.query_one("#actions", ListView).focus()

    # ----------------------------------------------------------- helpers
    def _poll_terminal_size(self) -> None:
        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            return
        if (cols, rows) != (self.size.width, self.size.height) and cols > 0 and rows > 0:
            size = Size(cols, rows)
            self.post_message(events.Resize(size, size))
            self.screen.refresh(layout=True)

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _event(self, line: str) -> None:
        """Thread-safe log from analyzer callbacks."""
        self.call_from_thread(self.log_line, line)

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _inputs(self) -> tuple[str, str, int]:
        port = self.query_one("#port", Input).value.strip()
        port2 = self.query_one("#port2", Input).value.strip()
        try:
            baud = int(self.query_one("#baud", Input).value.strip() or "9600")
        except ValueError:
            baud = 9600
        return port, port2, baud

    def _need_port(self, port: str) -> bool:
        if not port:
            self.log_line("[red]PORT is empty[/red] - enter COM5, /dev/ttyUSB0 or usb:A")
            return False
        return True

    # ----------------------------------------------------------- actions
    @on(ListView.Selected, "#actions")
    def _selected(self, ev: ListView.Selected) -> None:
        self.run_action_key(ev.item.key)

    def action_run_selected(self) -> None:
        lv = self.query_one("#actions", ListView)
        if lv.highlighted_child is not None:
            self.run_action_key(lv.highlighted_child.key)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_cycle_theme(self) -> None:
        self.theme = "claude-light" if self.theme == "claude" else "claude"

    def action_stop_job(self) -> None:
        if not self.job:
            self.log_line("no running job")
            return
        self.log_line(f"stopping {self.job['name']} ...")
        try:
            self.job["stop"]()
        finally:
            for link in self.job.get("links", ()):
                try:
                    link.close()
                except Exception:
                    pass
            self.job = None
            self.set_status("idle")

    def run_action_key(self, key: str) -> None:
        port, port2, baud = self._inputs()
        if key == "stop":
            self.action_stop_job(); return
        if key == "ports":
            self._do_ports(); return
        if key == "timeline":
            self._do_timeline(port2); return
        if self.job and key in ("sniff", "seplos", "identify", "discover",
                                "dump", "readout", "selftest"):
            self.log_line(f"[yellow]job {self.job['name']} is running - stop it first (s)[/yellow]")
            return
        if key in ("identify", "discover", "dump", "readout", "sniff", "seplos") and not self._need_port(port):
            return
        if key == "selftest" and not (self._need_port(port) and self._need_port(port2)):
            return
        self.log_line(f"[b]{key}[/b] on {port}{' + ' + port2 if port2 else ''} @ {baud}")
        self.set_status(f"running {key} ...")
        self._run_blocking(key, port, port2, baud)

    # --- one-shot tools run in a worker thread; live jobs keep a handle
    @work(thread=True, exclusive=False)
    def _run_blocking(self, key: str, port: str, port2: str, baud: int) -> None:
        from .core.transport import open_link
        try:
            if key == "identify":
                from .analyzer.identify import identify
                sess = S.SessionWriter(S.new_session_path("identify"))
                link = open_link(port, baud, name="id")
                try:
                    report = identify(link, sess, on_event=self._event)
                finally:
                    link.close()
                for v in report["verdicts"]:
                    self._event(f"  [b]* {v}[/b]")
                self._event(f"report: {S.save_report(sess.path, report, 'identify')}")
                sess.close()
            elif key == "discover":
                from .analyzer.discover import discover, format_discovery
                sess = S.SessionWriter(S.new_session_path("discover"))
                link = open_link(port, baud, name="disc")
                try:
                    report = discover(link, sess, on_event=self._event)
                finally:
                    link.close()
                self._event(format_discovery(report))
                self._event(f"report: {S.save_report(sess.path, report, 'discovery')}")
                sess.close()
            elif key == "dump":
                from .analyzer.dump import dump_stack, format_dump
                sess = S.SessionWriter(S.new_session_path("dump"))
                link = open_link(port, baud, name="dump")
                try:
                    report = dump_stack(link, sess, on_event=self._event)
                finally:
                    link.close()
                self._event(format_dump(report))
                self._event(f"report: {S.save_report(sess.path, report, 'dump')}")
                sess.close()
            elif key == "readout":
                self._readout(port, baud)
            elif key == "selftest":
                from .analyzer.selftest import run_selftest
                sess = S.SessionWriter(S.new_session_path("selftest"))
                report = run_selftest(port, port2, session=sess, on_event=self._event)
                self._event(f"RESULT: {'PASSED' if report['passed'] else 'FAILED'}")
                self._event(f"report: {S.save_report(sess.path, report, 'selftest')}")
                sess.close()
            elif key == "sniff":
                from .analyzer.sniff import Sniffer
                sess = S.SessionWriter(S.new_session_path("sniff"))
                links = {"ch1": open_link(port, baud, name="ch1")}
                if port2 and port2 != port:
                    links["ch2"] = open_link(port2, baud, name="ch2")
                sn = Sniffer(links, sess, on_event=self._event, quiet=True)
                self.job = {"name": f"sniff@{port}", "stop": sn.stop,
                            "links": tuple(links.values())}
                self._event(f"sniffing ... session {sess.path}  (s = stop)")
                sn.run()
                sess.close()
            elif key == "seplos":
                from .analyzer.seplos_monitor import ServiceMonitor
                sess = S.SessionWriter(S.new_session_path("seplos-monitor"))
                link = open_link(port, 19200 if baud == 9600 else baud, name="svc")
                mon = ServiceMonitor(link, sess, on_event=self._event)
                self.job = {"name": f"seplos-monitor@{port}", "stop": mon.stop,
                            "links": (link,)}
                self._event(f"monitoring ... session {sess.path}  (s = stop)")
                mon.run()
                sess.close()
        except Exception as e:
            self._event(f"[red]error:[/red] {e!r}")
        finally:
            self.job = None
            self.call_from_thread(self.set_status, "idle")

    def _readout(self, port: str, baud: int) -> None:
        from .core.transport import open_link
        from .analyzer.identify import identify
        from .analyzer.discover import discover, format_discovery
        from .analyzer.dump import dump_stack, format_dump
        folder = S.CAPTURE_DIR / "battery-checks" / time.strftime("%Y-%m-%d_%H-%M-%S")
        folder.mkdir(parents=True, exist_ok=True)
        self._event(f"[b]battery check[/b] -> {folder}")
        sess = S.SessionWriter(folder / "identify.jsonl")
        link = open_link(port, baud, name="id")
        try:
            rep = identify(link, sess, listen_s=3.0, on_event=self._event)
        finally:
            link.close()
        for v in rep["verdicts"]:
            self._event(f"  [b]* {v}[/b]")
        S.save_report(sess.path, rep, "identify"); sess.close()
        hits = [a for a in rep.get("active", []) if a.get("answered")]
        if hits and int(hits[0]["baud"]) != baud:
            baud = int(hits[0]["baud"])
            self._event(f"[b]using detected baud {baud}[/b]")
        sess = S.SessionWriter(folder / "discover.jsonl")
        link = open_link(port, baud, name="disc")
        try:
            rep = discover(link, sess, on_event=self._event)
        finally:
            link.close()
        self._event(format_discovery(rep)); S.save_report(sess.path, rep, "discovery"); sess.close()
        sess = S.SessionWriter(folder / "dump.jsonl")
        link = open_link(port, baud, name="dump")
        try:
            rep = dump_stack(link, sess, on_event=self._event)
        finally:
            link.close()
        self._event(format_dump(rep)); S.save_report(sess.path, rep, "dump"); sess.close()
        self._event(f"[b]done[/b] - files in {folder}")

    def _do_ports(self) -> None:
        import os
        lines = []
        if sys.platform != "android":
            try:
                from serial.tools import list_ports
                lines += [f"{p.device:10} {p.description} [{p.hwid}]" for p in list_ports.comports()]
            except Exception as e:
                lines.append(f"serial enumeration failed: {e}")
        fd = os.environ.get("RSH_USB_FD")
        if fd:
            try:
                from .core.ftdi_usb import describe_device
                d = describe_device(int(fd))
                lines += [f"usb:{ch:9} {d['product']} channel {ch} (via termux-usb)"
                          for ch in d["channels"]]
            except Exception as e:
                lines.append(f"usb: {e}")
        elif sys.platform == "android":
            lines.append("no USB fd: start the TUI via tools/termux/rsh-usb tui")
        self.log_line("\n".join(lines) or "no ports found")

    def _do_timeline(self, path: str) -> None:
        from .analyzer.timeline import TimelineConfig, build_timeline, export, format_timeline
        if not path:
            caps = sorted(S.CAPTURE_DIR.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime)
            if not caps:
                self.log_line("no captures yet"); return
            path = str(caps[-1])
        self.log_line(f"timeline of {path} ...")
        res = build_timeline(path, TimelineConfig())
        paths = export(res, path)
        self.log_line(format_timeline(res, max_events=80))
        self.log_line("exports: " + ", ".join(f"{k}={v}" for k, v in paths.items()))


def run_tui(port: str = "", port2: str = "", baud: int = 9600) -> int:
    RshTui(port, port2, baud).run()
    return 0
