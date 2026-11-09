"""Exercise the offline design study without a model or a terminal."""

import pytest
from examples.tui_tool_calls import SCENARIOS
from examples.tui_tool_calls import Call
from examples.tui_tool_calls import ToolCallStudy
from examples.tui_tool_calls import summary
from textual.widgets import Collapsible


def test_summary_keeps_literal_arguments() -> None:
    call = Call("shell", "echo [red]hello[/red]", "error", "1s", "exit 1")
    assert "[red]hello[/red]" in summary(call).plain
    assert "error" in summary(call).plain


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [48, 120])
async def test_designs_scenarios_and_lifecycle(width: int) -> None:
    app = ToolCallStudy()
    async with app.run_test(size=(width, 35)) as pilot:
        for design in range(3):
            await pilot.press(str(design + 1))
            assert app.design == design
            for _ in SCENARIOS:
                assert len(app.query(Collapsible)) == len(SCENARIOS[app.scenario])
                await pilot.press("n")
        await pilot.press("e")
        assert all(not card.collapsed for card in app.query(Collapsible))
        for state in ("queued", "approval", "running", "success"):
            await pilot.press("space")
            assert app.visible_calls()[0].state == state
            assert len(app.query(Collapsible)) == 1
        await pilot.press("n")
        assert app.phase == -1
