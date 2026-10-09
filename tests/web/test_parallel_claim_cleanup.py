"""A reserved inbox lane is reclaimed even if its producer never starts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Literal

import pytest

from nagents.events import TextDoneEvent
from tests.support.hang_guard import HANG_GUARD
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.routing import Work
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("command", ["", "compact"])
def test_unstarted_claim_is_joined_and_released_on_stop_or_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown: bool, command: Literal["", "compact"]
) -> None:
    async def scenario() -> None:
        received: list[str] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            received.append(str(messages[-1].content))
            assert received == ["Fresh message after Stop"]
            yield TextDoneEvent(text="The reclaimed lane works")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            host, root = state.channels, state.selected_session_id
            claimed, release = asyncio.Event(), asyncio.Event()
            original = host.execute_claim

            async def not_started(work: Work, started: asyncio.Event) -> None:
                claimed.set()
                await release.wait()
                await original(work, started)

            monkeypatch.setattr(host, "execute_claim", not_started)
            try:
                _, admitted = await state.queued_inputs.submit(
                    root, "never-started", "/compact" if command else "Unstarted input", command=command
                )
                assert admitted
                async with asyncio.timeout(HANG_GUARD):
                    await claimed.wait()
                run = state.run_for(root)
                assert run is not None and host.work_tasks.get(root) is run.task
                assert not received and run.harness is None
                if shutdown:
                    await host.close()
                else:
                    stopped = await client.post("/api/cancel", headers=headers, json={"run_id": run.id})
                    assert stopped.status_code == 200
                # Stop/close must join the done-callback cleanup, not merely
                # schedule it after returning a successful response.
                assert run.finished and run.task.done() and run.outcome == "cancelled"
                assert not host.work_tasks and not host.work_cleanup and not state.executions.runs
                assert not state.queued_inputs.claimed and not received
                rows = await host.store._transaction(
                    lambda db: db.execute(
                        "SELECT status FROM ngn_web_inbox WHERE message_id = 'never-started'"
                    ).fetchall()
                )
                assert rows == [("queued" if shutdown else "interrupted",)]
                history = await state.history.snapshot(root)
                assert not history
                if not shutdown:
                    monkeypatch.setattr(host, "execute_claim", original)
                    await state.queued_inputs.submit(root, "fresh-input", "Fresh message after Stop")
                    await until(lambda: bool(received) and not state.executions.runs and not host.work_tasks)
                    assert received == ["Fresh message after Stop"]
                    history = await state.history.snapshot(root)
                    assert [row["content"] for row in history if row["role"] == "assistant"] == [
                        "The reclaimed lane works"
                    ]
            finally:
                release.set()

    asyncio.run(scenario())
