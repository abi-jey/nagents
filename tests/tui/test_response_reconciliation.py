"""Final response snapshots reconcile streamed prose across tool boundaries."""

from pathlib import Path

import pytest
from textual.widgets import Markdown

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.events import ToolResultEvent
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send


def identity(response: str) -> dict[str, str]:
    return {"response_id": response, "response_session_id": "parent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [True, False])
async def test_second_round_snapshot_does_not_duplicate_after_tool(tmp_path: Path, preview: bool) -> None:
    backend = FakeHarness(tmp_path)
    paragraph = "OAuth registration defaults to ephemeral devices."
    backend.events = [
        TextChunkEvent(chunk="Earlier recovery explanation.", extra=identity("first")),
        TextDoneEvent(text="Earlier recovery explanation.", extra=identity("first")),
        ToolCallEvent(id="first", name="shell", arguments={}),
        ToolResultEvent(id="first", name="shell", result="ok"),
        TextChunkEvent(chunk=paragraph, extra=identity("second")),
        ToolCallProgressEvent(generation_id="second", index=0, name="shell", extra=identity("second"))
        if preview
        else ToolCallEvent(id="second", name="shell", arguments={}),
        TextDoneEvent(text=paragraph, extra=identity("second")),
        DoneEvent(),
    ]
    app = make_app(backend)
    async with app.run_test() as pilot:
        await idle(app, pilot)
        await send(app, pilot, "check recovery")
        await idle(app, pilot)
        assert [item.source for item in app.query(Markdown)] == ["Earlier recovery explanation.", paragraph]


@pytest.mark.asyncio
async def test_snapshots_update_original_widgets_and_preserve_distinct_repetitions(tmp_path: Path) -> None:
    app = make_app(FakeHarness(tmp_path))
    async with app.run_test() as pilot:
        await idle(app, pilot)
        await app._event(TextChunkEvent(chunk="Identical text", extra=identity("one")))
        await app._event(ToolCallProgressEvent(generation_id="one", index=0, name="shell"))
        original = next(iter(app.query(Markdown)))
        await app._event(TextDoneEvent(text="Corrected text", extra=identity("one")))
        assert original.source == "Corrected text"
        await app._event(TextDoneEvent(text="Corrected text", extra=identity("one")))
        assert len(app.query(Markdown)) == 1
        await app._event(TextChunkEvent(chunk="Corrected text", extra=identity("two")))
        await app._event(TextDoneEvent(text="Corrected text", extra=identity("two")))
        await app._event(TextDoneEvent(text="Corrected text", extra=identity("one")))
        assert [item.source for item in app.query(Markdown)] == ["Corrected text", "Corrected text"]


@pytest.mark.asyncio
async def test_snapshot_reconciles_parts_split_by_preview_and_final_only_text(tmp_path: Path) -> None:
    app = make_app(FakeHarness(tmp_path))
    async with app.run_test() as pilot:
        await idle(app, pilot)
        await app._event(TextChunkEvent(chunk="Before ", extra=identity("split")))
        await app._event(ToolCallProgressEvent(generation_id="split", index=0, name="shell"))
        await app._event(TextChunkEvent(chunk="after", extra=identity("split")))
        await app._event(TextDoneEvent(text="Before after!", extra=identity("split")))
        assert [item.source for item in app.query(Markdown)] == ["Before ", "after!"]
        await app._event(TextDoneEvent(text="Final only", extra=identity("final")))
        assert [item.source for item in app.query(Markdown)] == ["Before ", "after!", "Final only"]
