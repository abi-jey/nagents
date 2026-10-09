"""Admission reservations close SQL and task-start races across independent roots."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from nagents.events import TextDoneEvent
from nagents.web.wakeups import Chain
from tests.support.hang_guard import HANG_GUARD
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.routing import Work
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


def test_held_sql_claim_blocks_timer_and_http_reservations_then_unstarted_timers_cleanly_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            raise AssertionError("No admitted producer should start in this cancellation test")
            yield TextDoneEvent(text="unreachable")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            root = state.selected_session_id
            targets = [await state.harness.new_session(), await state.harness.new_session()]
            claimed, release = asyncio.Event(), asyncio.Event()
            original = state.channels.store.claim_work

            async def held(
                *,
                web_only: bool = False,
                available_channels: tuple[str, ...] | None = None,
                session_id: str = "",
                excluded_sessions: tuple[str, ...] = (),
            ) -> Work | None:
                work = await original(
                    web_only=web_only,
                    available_channels=available_channels,
                    session_id=session_id,
                    excluded_sessions=excluded_sessions,
                )
                claimed.set()
                await release.wait()
                return work

            monkeypatch.setattr(state.channels.store, "claim_work", held)
            try:
                await state.queued_inputs.submit(root, "unstarted-sql", "durable work")
                async with asyncio.timeout(HANG_GUARD):
                    await claimed.wait()
                assert state.mutating
                chains = [Chain(), Chain()]
                for target, chain in zip(targets, chains, strict=True):
                    state.wakeups.schedule(target, "origin", chain, "", 1, "deferred timer")
                clock.now += 1
                await state.wakeups.tick(dispatch=True)
                assert len(state.wakeups.pending) == 2 and all(chain.activations == 0 for chain in chains)
                assert not state.executions.runs
                response = await client.post(
                    "/api/run", headers=headers, json={"session_id": root, "prompt": "competing request"}
                )
                assert response.status_code == 409
                worker = state.channels.tasks[0]
                worker.cancel()
                release.set()
                await asyncio.gather(worker, return_exceptions=True)
                status = await state.channels.store._transaction(
                    lambda db: db.execute(
                        "SELECT status FROM ngn_web_inbox WHERE message_id = 'unstarted-sql'"
                    ).fetchone()[0]
                )
                assert status == "queued" and not state.mutating
                for target in targets:
                    await state.wakeups.tick(dispatch=True)
                    assert state.run_for(target) is not None
                # No await after each successful tick before its reservation:
                # configuration sees both owners before either coroutine starts.
                assert len(state.executions.runs) == 2
                assert not state.wakeups.pending and all(chain.activations == 1 for chain in chains)
                with pytest.raises(HTTPException), state.idle():
                    raise AssertionError("Reserved timer must block configuration")
                runs = list(state.executions.runs.values())
                await asyncio.gather(*(state.stop(run) for run in runs))
                assert all(run.finished and run.outcome == "cancelled" for run in runs)
                assert not state.executions.runs and not state.wakeups._claimed and not state.wakeups._running
                assert not state.channels.work_tasks and not state.channels.work_cleanup
                assert all(not provider.requests for provider in providers)
            finally:
                release.set()

    asyncio.run(scenario())
