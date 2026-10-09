"""A fork observes only its source's quiescent boundary and owns commit cleanup."""

from __future__ import annotations

import asyncio
from threading import Event
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.session.forks import fork_in
from nagents.types import Message
from nagents.web.wakeups import Chain
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.web.test_web_deletion import quiet
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event as ModelEvent
    from nagents.harness.types import SessionInfo
    from nagents.web.service import WebState
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
def test_fork_rejects_own_run_but_other_root_can_fork_and_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[ModelEvent]:
            entered.set()
            await release.wait()
            yield TextDoneEvent(text="Parent work finished in its own root")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            parent = state.selected_session_id
            other = await state.harness.new_session()
            await state.history.add_message(other, Message(role="user", content="Branch this independent history"))
            live = app.state.live
            voice_open = True
            monkeypatch.setattr(
                type(live), "active_session_id", property(lambda _: "owned-voice" if voice_open else "")
            )

            async def close(identifier: str) -> dict[str, object]:
                nonlocal voice_open
                assert identifier == "owned-voice"
                voice_open = False
                return {"status": "closed"}

            monkeypatch.setattr(live, "close", close)
            try:
                await state.queued_inputs.submit(parent, "parent-work", "Held parent request")
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                run = state.run_for(parent)
                assert run is not None and run.harness is state.harness
                denied = await client.post(f"/api/sessions/{parent}/fork", headers=headers, json={})
                assert denied.status_code == 409 and not run.task.cancelling() and voice_open
                renamed = await client.post(
                    f"/api/sessions/{parent}/rename", headers=headers, json={"title": "User title during stream"}
                )
                assert renamed.status_code == 200
                forked = await client.post(f"/api/sessions/{other}/fork", headers=headers, json={})
                assert forked.status_code == 200, forked.text
                assert not voice_open and state.harness.session_id == parent and not run.task.cancelling()
                target = forked.json()["session_id"]
                release.set()
                await until(lambda: not state.executions.runs and not state.channels.work_tasks)
                assert any(
                    row["content"] == "Parent work finished in its own root"
                    for row in await state.history.snapshot(parent)
                )
                assert not any(
                    row["content"] == "Parent work finished in its own root"
                    for row in await state.history.snapshot(target)
                )
                assert (
                    next(item for item in await state.harness.list_sessions() if item.id == parent).title
                    == "User title during stream"
                )
            finally:
                release.set()

    asyncio.run(scenario())


def test_rename_retries_catalog_read_that_began_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state: WebState = app.state.web
            await quiet(state)
            root = state.selected_session_id
            entered, release = asyncio.Event(), asyncio.Event()
            original = state.harness.list_sessions

            async def delayed() -> list[SessionInfo]:
                result = await original()
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return result

            monkeypatch.setattr(state.harness, "list_sessions", delayed)
            pending = asyncio.create_task(state.list_sessions())
            try:
                await entered.wait()
                changed = await client.post(
                    f"/api/sessions/{root}/rename", headers=headers, json={"title": "Current title"}
                )
                assert changed.status_code == 200
                release.set()
                assert next(item for item in await pending if item.id == root).title == "Current title"
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


def test_fork_blocks_source_wakeups_and_cancellation_joins_atomic_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state: WebState = app.state.web
            await quiet(state)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Original context"))
            chain = Chain()
            state.wakeups.schedule(root, "old-run", chain, "", 30, "Scheduled only in original root")
            assert (await client.post(f"/api/sessions/{root}/fork", headers=headers, json={})).status_code == 409
            state.wakeups.cancel(chain)
            entered, release = Event(), Event()
            original = fork_in

            def held(db: sqlite3.Connection, source: str, title: str) -> str:
                new = original(db, source, title)
                entered.set()
                assert release.wait(HANG_GUARD)
                return new

            monkeypatch.setattr("nagents.web.session_actions.fork_in", held)
            task = asyncio.create_task(
                client.post(f"/api/sessions/{root}/fork", headers=headers, json={"title": "Owned fork"})
            )
            try:
                assert await asyncio.to_thread(entered.wait, HANG_GUARD)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                sessions = await state.harness.list_sessions()
                assert len(sessions) == 2
                target = next(item for item in sessions if item.id != root)
                assert target.id == state.selected_session_id and target.forked_from == root
                assert [row["content"] for row in await state.history.snapshot(target.id)] == ["Original context"]
                assert not state.wakeups.pending
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
