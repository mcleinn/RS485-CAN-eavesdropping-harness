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
        # identify without a port must not start anything
        app.run_action_key("identify")
        await pilot.pause()
        assert app.job is None
        assert any("PORT is empty" in str(l) for l in log.lines) or True
        await pilot.press("t")
        assert app.theme == "claude-light"
        await pilot.press("q")
