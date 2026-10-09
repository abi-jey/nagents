"""Shutdown must not miss a claimed run still validating its durable owner."""

from __future__ import annotations

import asyncio
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from typing import TypeVar

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
    import sqlite3
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.routing import Work
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix
T = TypeVar("T")


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
            reserved = state.run_for(root)
            assert reserved is not None and not reserved.executing and not providers[0].requests
            assert await statuses(state) == {"validation-race": "running"}
            closing = asyncio.create_task(host.close() if shutdown == "host" else context.__aexit__(None, None, None))
            try:
                # Admission now reserves a visible owner before validation. A
                # shutdown must still return this never-executed claim to queued.
                await until(lambda: host.closed)
                assert not provider_started.is_set()
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
                assert not host.work_tasks and not host.work_cleanup
                assert state.harness._worker is None
                assert await statuses(state) == {"validation-race": "queued"}
                for frame, _ in state.bus.ring:
                    record = frame.get("record")
                    assert not isinstance(record, dict) or record.get("event") not in {
                        "text_chunk",
                        "text_done",
                        "tool_call",
                        "tool_result",
                    }
                assert await state.history.snapshot(root) == []
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


@pytest.mark.parametrize("ending", ["host", "lifespan", "stop"])
def test_repeated_claim_cancellation_joins_final_sql_and_releases_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    async def scenario() -> None:
        validated, release_validation = asyncio.Event(), asyncio.Event()
        committing, release_commit = ThreadEvent(), ThreadEvent()
        context = application(tmp_path, monkeypatch)
        state, providers = await context.__aenter__()
        host, root = state.channels, state.selected_session_id
        validate, transaction = host.store.validate_work, host.store._transaction
        expected = "interrupted" if ending == "stop" else "queued"

        async def gated(work: Work) -> None:
            await validate(work)
            validated.set()
            await release_validation.wait()

        async def delayed(operation: Callable[[sqlite3.Connection], T]) -> T:
            def blocked(db: sqlite3.Connection) -> T:
                result = operation(db)
                row = db.execute("SELECT status FROM ngn_web_inbox WHERE message_id = 'final-commit'").fetchone()
                if row == (expected,) and not committing.is_set():
                    committing.set()
                    assert release_commit.wait(HANG_GUARD)
                return result

            return await transaction(blocked)

        monkeypatch.setattr(host.store, "validate_work", gated)
        await host.store.web(root, "final-commit", "Execute only if recovered")
        host.changed.set()
        try:
            await asyncio.wait_for(validated.wait(), HANG_GUARD)
            run = state.run_for(root)
            assert run is not None and not run.executing
            claim = host.work_tasks[root]
            monkeypatch.setattr(host.store, "_transaction", delayed)
            closing = asyncio.create_task(
                state.stop(run)
                if ending == "stop"
                else host.close()
                if ending == "host"
                else context.__aexit__(None, None, None)
            )
            try:
                await until(committing.is_set)
                # A real final SQL transaction is held before commit. Further
                # cancellation must join both it and its in-memory bookkeeping.
                assert claim.cancelling() and not claim.done() and not closing.done()
                claim.cancel()
                await asyncio.sleep(0)
                claim.cancel()
                assert not providers[0].requests
            finally:
                release_commit.set()
                release_validation.set()
                await asyncio.wait_for(asyncio.shield(closing), HANG_GUARD)
            assert claim.done() and run.finished and state.run_for(root) is None
            assert not host.work_tasks and not host.work_cleanup and not state.queued_inputs.claimed
            assert await statuses(state) == {"final-commit": expected}
            assert not providers[0].requests and await state.history.snapshot(root) == []
        finally:
            release_commit.set()
            release_validation.set()
            await context.__aexit__(None, None, None)

        async with application(tmp_path, monkeypatch) as (reopened, providers):
            await idle(reopened)
            assert await statuses(reopened) == {"final-commit": "interrupted" if ending == "stop" else "completed"}
            assert len(providers[0].requests) == (0 if ending == "stop" else 1)
            history = await reopened.history.snapshot(root)
            assert [row["content"] for row in history if row["role"] == "user"] == (
                [] if ending == "stop" else ["Execute only if recovered"]
            )

    asyncio.run(scenario())
