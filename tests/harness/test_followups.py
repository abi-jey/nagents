"""Root follow-ups preserve one model owner and the existing child lifecycle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.followups import RunInput
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.asyncio
async def test_root_followup_admission_is_bounded_session_owned_and_cancelled_with_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        entered.set()
        await asyncio.Event().wait()
        yield TextDoneEvent(text="unreachable")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    assert not harness.submit_followup(harness.session_id, RunInput("before run"))
    task = asyncio.create_task(collect(harness))
    try:
        async with asyncio.timeout(HANG_GUARD):
            await entered.wait()
        assert not harness.submit_followup("another-session", RunInput("wrong session"))
        for index in range(32):
            assert harness.submit_followup(harness.session_id, RunInput(f"follow-up {index}", str(index)))
        with pytest.raises(ValueError, match="queue is full"):
            harness.submit_followup(harness.session_id, RunInput("overflow"))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not harness.followups.pending
        assert not harness.submit_followup(harness.session_id, RunInput("after cancellation"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()
