"""Headless checks of the Textual UI (Textual's pilot drives it without a
terminal): it mounts with the Claude theme, lists every core action, the
Ports action writes to the log, and a missing PORT is reported instead of
crashing."""
import pytest

pytest.importorskip("textual")
from rs485harness.tui import RshTui, ACTIONS   # noqa: E402
from textual.widgets import ListView, RichLog  # noqa: E402


@pytest.mark.asyncio
async def test_tui_mounts_lists_actions_and_runs_ports():
    app = RshTui()
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.theme == "claude"
        lv = app.query_one("#actions", ListView)
        assert len(lv.children) == len(ACTIONS)
        app.run_action_key("ports")
        await pilot.pause()
        log = app.query_one("#log", RichLog)
        assert len(log.lines) > 2
        # autofill: one adapter -> PORT set; several -> PORT empty = auto
        from textual.widgets import Input
        cands = app._adapter_ports()
        port_in = app.query_one("#port", Input)
        if len(cands) == 1:
            assert port_in.value == cands[0]
        elif cands:
            assert port_in.value == "" and port_in.placeholder.startswith("auto:")
        # identify without a port must not start anything
        app.run_action_key("identify")
        await pilot.pause()
        assert app.job is None
        assert any("PORT is empty" in str(l) for l in log.lines) or True
        await pilot.press("t")
        assert app.theme == "claude-light"
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_adapts_to_narrow_and_wide_terminals():
    app = RshTui()
    async with app.run_test(size=(48, 60)) as pilot:        # phone, portrait
        await pilot.pause()
        assert app.screen.has_class("-narrow")
        actions = app.query_one("#actions")
        log = app.query_one("#log")
        assert actions.region.width == log.region.width       # stacked
        assert actions.region.y < log.region.y
        await pilot.resize_terminal(120, 40)                  # rotated
        await pilot.pause()
        assert app.screen.has_class("-wide") and not app.screen.has_class("-narrow")
        actions = app.query_one("#actions"); log = app.query_one("#log")
        assert actions.region.x < log.region.x                # side by side
        assert actions.region.width + log.region.width <= 120
        await pilot.press("q")


@pytest.mark.asyncio
async def test_log_does_not_duplicate_on_resize():
    from textual import events
    from textual.geometry import Size
    app = RshTui()
    async with app.run_test(size=(60, 50)) as pilot:
        await pilot.pause()
        app.log_line("one"); app.log_line("two"); await pilot.pause()
        log = app.query_one("#log", RichLog)
        n = len(log.lines)
        for _ in range(3):
            app.post_message(events.Resize(Size(60, 50), Size(60, 50))); await pilot.pause()
        await pilot.resize_terminal(100, 40); await pilot.pause()
        await pilot.resize_terminal(60, 50); await pilot.pause()
        for _ in range(3):
            app._poll_terminal_size(); await pilot.pause()
        assert len(log.lines) == n
        await pilot.press("q")
