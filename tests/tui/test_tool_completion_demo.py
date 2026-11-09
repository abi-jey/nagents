"""The experimental completion pass reaches the rendered header and settles."""

import pytest
from examples import tui_tool_completion as demo
from textual.content import Content


@pytest.mark.asyncio
async def test_completion_sweep_moves_once_then_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    app = demo.CompletionStudy()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        cards = list(app.query(demo.CompletionCard))
        card = cards[0]
        card.finish("Done", None, 1000)
        started = card.completion_started
        frames = []
        for age in (0.0, 0.4, 0.8, 1.5, 3.0):
            monkeypatch.setattr(demo, "monotonic", lambda age=age: started + age)
            card.paint_completion()
            label = card._title.label
            assert isinstance(label, Content)
            frames.append(tuple(label.spans))
            assert label.plain == card.title
            assert card.collapsed
        assert frames[0] != frames[1] != frames[2]
        assert frames[3] == frames[4]
        assert card.completion_timer is None
        assert frames[4]
        assert "143" in repr(frames[4])
        failed = cards[-1]
        failed.finish("", "Denied", 1000)
        assert failed.completion_timer is None
        assert failed._outcome == "failed"
