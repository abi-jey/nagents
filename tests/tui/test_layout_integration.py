"""Footer geometry remains independent of rail activity rows."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from nagents.harness.agents.subagents import TaskInfo
from nagents.tui.widgets import Composer
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_background_rows_do_not_reflow_wrapped_draft_or_footer(tmp_path: Path, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        tasks: list[TaskInfo] = []
        backend.tasks.list = lambda: tasks
        app = make_app(backend)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("preserve this wrapped draft across background updates " * 3)
            composer.focus()
            await pilot.pause()
            assert composer.wrapped_document.height > 1

            rail = app.query_one("#rail")
            background = app.query_one("#background-status", Static)
            footer = app.query_one("#footer")
            status = app.query_one("#status", Static)
            provider_model = app.query_one("#provider-model", Static)
            assert background.parent is rail
            assert {child.id for child in footer.children} == {"status", "provider-model"}
            model_content = provider_model.content
            assert isinstance(model_content, Text)
            assert str(model_content) == "openai · gpt-6-astra"
            spans = model_content.spans
            assert isinstance(model_content.style, Style) and model_content.style.color is not None
            assert len(spans) == 2
            assert spans[0].start == len("openai") and spans[0].style == "dim"
            assert isinstance(spans[1].style, Style) and spans[1].style.color is not None and spans[1].style.bold

            def layout() -> tuple[object, ...]:
                return (
                    composer.region,
                    footer.region,
                    status.region,
                    provider_model.region,
                    str(provider_model.content),
                    composer.text,
                )

            stable = layout()
            assert footer.region.y >= composer.region.bottom
            assert footer.region.bottom <= size[1]

            tasks[:] = [
                TaskInfo(f"child-{index}", f"Child {index}", status="running", depth=1, phase="responding")
                for index in range(1, 4)
            ]
            app._refresh_tasks()
            app._render_background_activity(monotonic())
            await pilot.pause()
            assert background.display
            if rail.display:
                content = str(background.content)
                assert all(f"Child {index}" in content for index in range(1, 4))
            assert layout() == stable

            tasks[0].phase = "tool"
            tasks.pop()
            app._refresh_tasks()
            app._wakeups.schedule(backend.session_id, "child-1", 60, "Continue review")
            app._render_background_activity(monotonic())
            await pilot.pause()
            if rail.display:
                content = str(background.content)
                assert "Child 1" in content and "Child 2" in content
                assert "Wake-up" in content and "Continue review" in content
            assert layout() == stable

            tasks.clear()
            app._refresh_tasks()
            app._wakeups.cancel_turn()
            app._render_background_activity(monotonic())
            await pilot.pause()
            assert not background.display
            assert layout() == stable

    asyncio.run(scenario())
