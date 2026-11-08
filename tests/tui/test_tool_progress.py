"""Streamed tool previews update in place without executing partial calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.events import TextChunkEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.events import ToolResultEvent
from nagents.tui.widgets import ToolCard
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_tool_preview_fills_in_and_promotes_same_card(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await app._event(ToolCallProgressEvent(generation_id="g", index=0))
            card = app.query_one(ToolCard)
            assert card.tool == "..."
            assert "preparing" in card.title
            assert "0.00s" not in card.title
            assert not app._running_tools
            # Preparing headers shimmer, but never accrue execution duration.
            assert card._elapsed_timer is not None
            await pilot.pause(0.15)
            assert card._duration == ""
            assert "0.00s" not in card.title

            await app._event(
                ToolCallProgressEvent(generation_id="g", index=0, name="read_file", arguments_text='{"path":"READ')
            )
            assert card.tool == "read_file"
            assert "READ" in card.title
            assert len(app.query(ToolCard)) == 1
            card.collapsed = False
            assert "READ" in str(card.arguments.content)

            await app._event(
                ToolCallProgressEvent(
                    generation_id="g",
                    index=0,
                    id="call",
                    name="read_file",
                    arguments_text='{"path":"README.md"}',
                    status="ready",
                )
            )
            assert "ready" in card.title
            assert not app._running_tools
            await app._event(TextChunkEvent(chunk="Before execution"))
            before_answer = app._answer
            await app._event(
                ToolCallEvent(
                    id="call",
                    name="read_file",
                    arguments={"path": "README.md"},
                    extra={"generation_id": "g", "index": 0},
                )
            )
            assert app._tools["call"] is card
            assert not card.collapsed
            assert len(app.query(ToolCard)) == 1
            assert "running" in card.title
            assert card._elapsed_timer is not None
            assert not app._tool_previews
            await app._event(ToolResultEvent(id="call", name="read_file", result="contents", duration_ms=5))
            assert "complete" in card.title
            assert card.output_text == "contents"
            await app._event(TextChunkEvent(chunk="After execution"))
            assert app._answer is not before_answer
            assert app._answer_text == "After execution"

    asyncio.run(scenario())


def test_unfinished_preview_cancelled_at_run_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.events = [ToolCallProgressEvent(generation_id="g", name="shell", arguments_text='{"command":')]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "preview only")
            await idle(app, pilot)
            assert "cancelled" in app.query_one(ToolCard).title
            assert not app._tool_previews
            assert not app._running_tools

    asyncio.run(scenario())


def test_tool_previews_separate_attempts_indices_and_abandonment(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            for generation, index in (("g", 0), ("g", 1), ("retry", 0)):
                await app._event(ToolCallProgressEvent(generation_id=generation, index=index, name="shell"))
            cards = dict(app._tool_previews)
            assert len(app.query(ToolCard)) == 3
            await app._event(ToolCallProgressEvent(generation_id="g", index=0, status="abandoned"))
            assert "cancelled" in cards[("g", 0)].title
            assert len(app._tool_previews) == 2
            app._cancel_tool_previews()
            assert not app._tool_previews
            assert all("cancelled" in card.title for card in cards.values())
            assert not app._running_tools

    asyncio.run(scenario())
