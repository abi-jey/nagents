"""Child pickup receipts clear only the draft that this UI actually sent."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Literal

import pytest
from textual.widgets import TextArea

from nagents.harness.agents.subagents import TaskInfo
from nagents.harness.types import Notice
from nagents.harness.types import TaskMessage
from nagents.tui.tasks import TaskScreen
from nagents.tui.widgets import Turn
from tests.support.hang_guard import HANG_GUARD
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from textual.pilot import Pilot

    from nagents.harness.runtime.followups import RunInput
    from nagents.harness.types import HarnessEvent
    from nagents.tui import NagentsApp


class NoteHarness(FakeHarness):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.inputs: list[tuple[str, RunInput]] = []
        self.admission: Literal["accept", "reject", "cancel"] = "accept"

    async def continue_task(self, session_id: str, prompt: str, *, message: RunInput) -> AsyncIterator[HarnessEvent]:
        assert message.prompt == prompt
        assert message.identifier and message.identifier == message.message_id
        self.inputs.append((session_id, message))
        if self.admission == "reject":
            raise RuntimeError("The child cannot accept this follow-up")
        if self.admission == "cancel":
            raise asyncio.CancelledError
        yield Notice("Follow-up accepted; pickup will arrive on the active stream.")


async def start_parent(app: NagentsApp, backend: NoteHarness, pilot: Pilot[None]) -> None:
    backend._busy = "run"
    app._launch(lambda: app._send("wait"), "Working")
    async with asyncio.timeout(HANG_GUARD):
        while not backend.running:
            await pilot.pause(0.01)


def pickup(session_id: str, message_id: str) -> TaskMessage:
    return TaskMessage(
        session_id,
        "Explorer",
        "A human message was picked up by this agent.",
        message_id=message_id,
    )


def test_busy_pickup_matches_task_and_message_without_losing_a_newer_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = NoteHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await start_parent(app, backend, pilot)
            app._task_drafts["s1"] = "My private child note"
            await app._busy_task_followup("s1", "My private child note")
            _, message = backend.inputs[-1]
            key = ("s1", message.message_id)
            assert key in app._pending_task_notes
            assert app._task_drafts["s1"] == "My private child note"

            await app._event(pickup("s2", message.message_id))
            await app._event(pickup("s1", "other-message"))
            assert key in app._pending_task_notes
            assert app._task_drafts["s1"] == "My private child note"

            app._task_drafts["s1"] = "A newer unsent direction"
            event = pickup("s1", message.message_id)
            await app._event(event)
            assert key not in app._pending_task_notes
            assert app._task_drafts["s1"] == "A newer unsent direction"
            assert event.prompt == "A human message was picked up by this agent."
            assert any("My private child note" in str(turn.body.render()) for turn in app.query(Turn))

            await app._busy_task_followup("s1", "A newer unsent direction")
            _, second = backend.inputs[-1]
            assert second.message_id != message.message_id
            await app._event(pickup("s1", second.message_id))
            assert "s1" not in app._task_drafts
            assert not app._pending_task_notes

    asyncio.run(scenario())


def test_pickup_preserves_the_open_inspectors_unsaved_editor(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = NoteHarness(tmp_path)
        info = TaskInfo(session_id="s1", title="Explorer", prompt="Research", root_session_id=backend.session_id)
        info.status = "completed"
        backend.tasks.list = lambda: [info]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            app._task_drafts["s1"] = "Sent direction"
            message = app._task_input("s1", "Sent direction")
            app._show_tasks("s1")
            await pilot.pause()
            assert isinstance(app.screen, TaskScreen)
            editor = app.screen.query_one("#task-followup", TextArea)
            editor.load_text("Still editing this follow-up")
            await app._event(pickup("s1", message.message_id))
            assert editor.text == "Still editing this follow-up"
            assert app._task_drafts["s1"] == "Still editing this follow-up"
            app.screen.action_close()
            await pilot.pause()
            assert app._task_drafts["s1"] == "Still editing this follow-up"

    asyncio.run(scenario())


@pytest.mark.parametrize("admission", ["reject", "cancel"])
def test_failed_admission_keeps_the_draft_and_removes_pending_correlation(
    tmp_path: Path, admission: Literal["reject", "cancel"]
) -> None:
    async def scenario() -> None:
        backend = NoteHarness(tmp_path)
        backend.admission = admission
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await start_parent(app, backend, pilot)
            app._task_drafts["s1"] = "Retry this direction"
            if admission == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await app._busy_task_followup("s1", "Retry this direction")
            else:
                await app._busy_task_followup("s1", "Retry this direction")
            assert app._task_drafts["s1"] == "Retry this direction"
            assert not app._pending_task_notes

    asyncio.run(scenario())


def test_parent_cancellation_discards_only_unpicked_receipts_not_drafts(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = NoteHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await start_parent(app, backend, pilot)
            app._task_drafts["child"] = "Not picked up yet"
            await app._busy_task_followup("child", "Not picked up yet")
            assert app._pending_task_notes
            assert app._active is not None
            app._active.cancel()
            await idle(app, pilot)
            assert not app._pending_task_notes
            assert app._task_drafts["child"] == "Not picked up yet"

    asyncio.run(scenario())
