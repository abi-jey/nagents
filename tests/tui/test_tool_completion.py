"""Successful production headers sweep once and retain their green tint."""

import pytest
from textual.content import Content

from nagents.tui import widgets
from nagents.tui.widgets import ToolCard
from tests.tui.test_tool_card_design import design_app


@pytest.mark.asyncio
@pytest.mark.parametrize("animations", [True, False])
async def test_success_green_is_rendered_and_settles(monkeypatch: pytest.MonkeyPatch, animations: bool) -> None:
    app = design_app("terminal")
    card = ToolCard("green", "read_file", {"path": "file.py"}, animations=animations)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.mount(card)
        await pilot.pause()
        card.finish("Done", None, 1000)
        started = card._completion_started
        snapshots = []
        for age in (0.0, 0.6, 1.5, 3.0):
            monkeypatch.setattr(widgets, "monotonic", lambda age=age: started + age)
            card._paint_completion()
            label = card._title.label
            assert isinstance(label, Content)
            snapshots.append(tuple(label.spans))
            assert card.collapsed
        assert (snapshots[0] != snapshots[1]) == animations
        assert snapshots[-1] == snapshots[-2]
        assert card._completion_timer is None
        frozen = snapshots[-1]
        card._update_summary()
        label = card._title.label
        assert isinstance(label, Content)
        assert tuple(label.spans) == frozen
        card.cancel()
        assert card._completion_timer is None
