"""Session navigation waits for bookkeeping without taking an active run's owner."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import PropertyMock
from unittest.mock import patch

import pytest

from nagents.types import Message
from nagents.web.execution.service import Run
from nagents.web.execution.service import WebState
from nagents.web.routing import RoutingStore
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.web.test_web_deletion import quiet

if TYPE_CHECKING:
    from pathlib import Path

    import httpx


async def navigate(client: httpx.AsyncClient, headers: dict[str, str], action: str, target: str) -> httpx.Response:
    if action == "list":
        return await client.get("/api/sessions", headers=headers)
    return await client.post(
        f"/api/sessions/{action}",
        headers=headers,
        json={"session_id": target} if action == "resume" else {},
    )


async def seed(state: WebState) -> tuple[str, str, str]:
    await quiet(state)
    root = state.selected_session_id
    await state.history.add_message(root, Message(role="user", content="Keep this conversation"))
    target, unused = await state.channels.store._transaction(
        lambda db: (RoutingStore.new_root(db, "Other conversation"), RoutingStore.new_root(db, ""))
    )
    return root, target, unused


async def queued(request: asyncio.Task[httpx.Response], entered: asyncio.Event) -> None:
    barrier = asyncio.create_task(entered.wait())
    try:
        async with asyncio.timeout(HANG_GUARD):
            done, _ = await asyncio.wait((request, barrier), return_when=asyncio.FIRST_COMPLETED)
        assert barrier in done and not request.done(), "Navigation must wait for the bookkeeping owner"
    finally:
        barrier.cancel()
        await asyncio.gather(barrier, return_exceptions=True)


@pytest.mark.parametrize("action", ["new", "resume", "list"])
@pytest.mark.parametrize("admit_run", [False, True])
def test_navigation_waits_for_bookkeeping_and_preserves_newly_admitted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, admit_run: bool
) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root, target, unused = await seed(state)
            waiting = asyncio.Event()
            original_wait = state.mutation_released.wait

            async def observe_wait() -> bool:
                waiting.set()
                return await original_wait()

            monkeypatch.setattr(state.mutation_released, "wait", observe_wait)
            run = Run(root, harness=state.harness)
            try:
                with state.idle(allow_running=True), state.harness.operation("bookkeeping fixture"):
                    request = asyncio.create_task(navigate(client, headers, action, target))
                    await queued(request, waiting)
                    assert state.selected_session_id == state.harness.session_id == root
                    if admit_run:
                        state.executions.reserve(run)
                response = await request
                assert response.status_code == 200, response.text
                selected = response.json()["session_id"]
                if action == "new":
                    assert selected not in {root, target, unused}
                else:
                    assert selected == (target if action == "resume" else root)
                sessions = {session["id"] for session in response.json()["sessions"]}
                assert (unused in sessions) == admit_run
                assert {root, target} <= sessions
                if admit_run:
                    assert state.run_for(root) is run and not run.finished
                    assert state.harness.session_id == root
                else:
                    assert state.harness.session_id == selected
                assert not state.mutating and not state.harness._busy
            finally:
                state.executions.release(run)

    asyncio.run(check())


@pytest.mark.parametrize("action", ["new", "resume", "list"])
@pytest.mark.parametrize("interruption", ["cancel", "shutdown"])
def test_navigation_interrupted_while_queued_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, interruption: str
) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root, target, unused = await seed(state)
            waiting = asyncio.Event()
            original_wait = state.mutation_released.wait

            async def observe_wait() -> bool:
                waiting.set()
                return await original_wait()

            monkeypatch.setattr(state.mutation_released, "wait", observe_wait)
            close = AsyncMock()
            with (
                patch.object(
                    type(app.state.live), "active_session_id", new_callable=PropertyMock, return_value="live-call"
                ),
                patch.object(app.state.live, "close", close),
            ):
                try:
                    with state.idle(allow_running=True), state.harness.operation("bookkeeping fixture"):
                        request = asyncio.create_task(navigate(client, headers, action, target))
                        await queued(request, waiting)
                        close.assert_not_awaited()
                        if interruption == "cancel":
                            request.cancel()
                            with pytest.raises(asyncio.CancelledError):
                                await request
                            assert state.mutating and not state.mutation_released.is_set()
                        else:
                            state.executions.closed = True
                    if interruption == "shutdown":
                        response = await request
                        assert response.status_code == 503, response.text
                    close.assert_not_awaited()
                    assert state.selected_session_id == state.harness.session_id == root
                    assert {session.id for session in await state.list_sessions()} == {root, target, unused}
                    assert not state.mutating and not state.harness._busy
                finally:
                    state.executions.closed = False

    asyncio.run(check())
