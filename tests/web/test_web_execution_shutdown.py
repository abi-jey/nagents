"""Shutdown must not miss a claimed run still validating its durable owner."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from tests.support.channels import FakeChannel
from tests.support.hang_guard import HANG_GUARD
from tests.web.test_web_host_lifecycle import application
from tests.web.test_web_host_lifecycle import idle
from tests.web.test_web_host_lifecycle import statuses
from tests.web.test_web_subscription_lifecycle import until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.routing import Work
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("shutdown", ["host", "lifespan"])
def test_shutdown_during_owner_validation_requeues_without_starting_an_unowned_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown: str
) -> None:
    async def scenario() -> None:
        validated = asyncio.Event()
        release_validation = asyncio.Event()
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        async def holding(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            provider_started.set()
            yield TextChunkEvent(chunk="unfinished draft")
            await release_provider.wait()
            yield TextDoneEvent(text="released only for old-code cleanup")

        context = application(tmp_path, monkeypatch, holding)
        state, providers = await context.__aenter__()
        host = state.channels
        channel = FakeChannel("fixture")
        await host.open("fixture", channel)
        validate = host.store.validate_work

        async def gated(work: Work) -> None:
            await validate(work)
            validated.set()
            await release_validation.wait()

        monkeypatch.setattr(host.store, "validate_work", gated)
        root = state.selected_session_id
        await host.store.web(root, "validation-race", "hold")
        host.changed.set()
        started = asyncio.create_task(provider_started.wait())
        try:
            await asyncio.wait_for(validated.wait(), HANG_GUARD)
            assert state.active is None and not providers[0].requests
            assert await statuses(state) == {"validation-race": "running"}
            closing = asyncio.create_task(host.close() if shutdown == "host" else context.__aexit__(None, None, None))
            try:
                # _close cancels the poller only AFTER checking state.active.
                # Release validation after that check, not on a timing-based sleep.
                await until(lambda: host.tasks[1].cancelling() > 0)
                assert host.closed and not closing.done() and state.active is None
                release_validation.set()
                done, _ = await asyncio.wait(
                    (closing, started), return_when=asyncio.FIRST_COMPLETED, timeout=HANG_GUARD
                )
                stacks = {
                    task.get_name(): [(frame.f_code.co_name, frame.f_lineno) for frame in task.get_stack()]
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                }
                assert closing in done and not provider_started.is_set(), (
                    f"Shutdown missed a producer started after owner validation: {stacks}"
                )
                await closing
                assert not providers[0].requests and channel.closed
                assert state.active is None and all(task.done() for task in host.tasks)
                assert state.harness._worker is None
                assert await statuses(state) == {"validation-race": "queued"}
                for frame, _ in state.bus.ring:
                    record = frame.get("record")
                    assert not isinstance(record, dict) or record.get("event") != "run_started"
            finally:
                # Let the old implementation finish its late producer rather than
                # leave a shielded shutdown hanging after the regression fails.
                release_validation.set()
                release_provider.set()
                await asyncio.gather(closing, return_exceptions=True)
        finally:
            release_validation.set()
            release_provider.set()
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            await context.__aexit__(None, None, None)

        # A claim that never started must remain executable after reopening, with
        # exactly one user turn and no automatic replay of a started activation.
        async with application(tmp_path, monkeypatch) as (reopened, providers):
            await idle(reopened)
            assert await statuses(reopened) == {"validation-race": "completed"}
            assert len(providers[0].requests) == 1
            history = await reopened.harness.agent.session.get_history(root)
            assert [message.content for message in history if message.role == "user"] == ["hold"]

    asyncio.run(scenario())
