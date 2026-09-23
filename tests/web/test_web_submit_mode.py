"""Web submission policy: queue (default) vs interrupt, with retry/isolation safety.

The /api/messages route admits durable inbox work and, only for a genuinely new
submission in interrupt mode, stops the active run in the *same* session. A
duplicate HTTP retry or another session's input must never interrupt work.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import pytest

from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from tests.support.channels import site

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.service import Run
    from tests.support.channels import Site
    from tests.support.providers import FakeProvider
    from tests.support.providers import Script


# Valid UUID-format message ids (the /api/messages schema requires a UUID).
M1 = "11111111-1111-1111-1111-111111111111"
M2 = "22222222-2222-2222-2222-222222222222"
M3 = "33333333-3333-3333-3333-333333333333"


def inbox_rows(app: Site) -> dict[str, str]:
    """Durable web inbox work keyed by message id (channel-less admissions)."""

    async def read() -> dict[str, str]:
        async with aiosqlite.connect(app.state.channels.store.db_path) as db:
            cursor = await db.execute("SELECT message_id, status FROM ngn_web_inbox WHERE channel = '' ORDER BY id")
            return {str(message_id): str(status) for message_id, status in await cursor.fetchall()}

    assert app.client.portal is not None
    return app.client.portal.call(read)


def holding_first_script(started: asyncio.Event) -> Script:
    """Hold the first model call, then complete later calls immediately."""
    calls = {"count": 0}

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        calls["count"] += 1
        if calls["count"] == 1:
            started.set()
            yield TextChunkEvent(chunk="first work")
            await asyncio.Event().wait()
        else:
            yield TextDoneEvent(text="later work")

    return script


def begin_active_run(app: Site) -> tuple[Run, str]:
    """Start a durable web run and return the active run and its root session."""
    app.configure()
    app.emit("/session")
    app.idle()
    root = app.bindings()["chat-a"]
    assert app.client.portal is not None
    started: asyncio.Event = app.client.portal.call(asyncio.Event)
    app.providers[0].script = holding_first_script(started)
    app.submit("first work", root, id=M1)
    app.client.portal.call(asyncio.wait_for, started.wait(), 5)
    active = app.state.active
    assert active is not None and active.session_id == root and active.message_id == M1
    return active, root


def stop_active(app: Site, active: Run) -> None:
    if app.state.active is active:
        response = app.client.post("/api/cancel", headers=app.headers, json={"run_id": active.id})
        assert response.status_code == 200
    app.idle()


@pytest.mark.requires_posix
def test_queue_default_keeps_active_run_and_durable_followup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        assert app.state.harness.config.submit_mode == "queue"
        active, root = begin_active_run(app)
        try:
            app.submit("second work", root, id=M2)
            # Queue mode never stops the active run; the follow-up is durable.
            assert app.state.active is active and not active.task.cancelling()
            rows = inbox_rows(app)
            assert rows.get(M1) in {"running", "queued"} and rows.get(M2) == "queued"
        finally:
            stop_active(app, active)


@pytest.mark.requires_posix
def test_interrupt_stops_same_session_active_run_for_new_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        active, root = begin_active_run(app)
        app.state.harness.config.submit_mode = "interrupt"
        try:
            app.submit("second work", root, id=M2)
            # A distinct new submission stops the same-session run.
            assert active.task.cancelling() or app.state.active is not active
        finally:
            stop_active(app, active)


@pytest.mark.requires_posix
def test_duplicate_retry_never_interrupts_active_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        active, root = begin_active_run(app)
        app.state.harness.config.submit_mode = "interrupt"
        try:
            # Same message id and prompt: an HTTP retry, not a new submission.
            retry = app.submit("first work", root, id=M1)
            assert retry["status"] == "queued"
            assert not active.task.cancelling() and app.state.active is active
            assert inbox_rows(app).get(M1) in {"running", "queued"}
        finally:
            stop_active(app, active)


@pytest.mark.requires_posix
def test_other_session_input_never_cancels_active_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        active, _ = begin_active_run(app)
        app.state.harness.config.submit_mode = "interrupt"
        store = app.state.channels.store
        assert app.client.portal is not None

        def create(db: object) -> str:
            return store.new_root(db, "unrelated work")  # type: ignore[arg-type]

        other: str = app.client.portal.call(store._transaction, create)
        try:
            app.submit("unrelated", other, id=M3)
            # Different-session input cannot stop this session's run.
            assert not active.task.cancelling() and app.state.active is active
            assert inbox_rows(app).get(M3) == "queued"
        finally:
            stop_active(app, active)


@pytest.mark.requires_posix
def test_cancelled_http_admission_still_applies_interrupt_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        from tests.support.hang_guard import HANG_GUARD
        from tests.support.web import client_app

        async with client_app(tmp_path) as (app, client, headers, harnesses):
            state = app.state.web
            harness = harnesses[0]
            session = harness.session_id
            state.harness.config.submit_mode = "interrupt"

            # Start a durable run that the controlled provider holds open.
            first = await client.post(
                "/api/messages",
                json={"session_id": session, "prompt": "wait", "message_id": M1},
                headers=headers,
            )
            assert first.status_code == 200
            async with asyncio.timeout(HANG_GUARD):
                while state.active is None:
                    await asyncio.sleep(0.01)
            active = state.active
            assert active is not None and active.session_id == session

            entered, release = asyncio.Event(), asyncio.Event()
            original = state.channels.store.web

            async def blocked(session_id: str, message_id: str, prompt: str) -> tuple[str, bool]:
                entered.set()
                await release.wait()
                return cast("tuple[str, bool]", await original(session_id, message_id, prompt))

            monkeypatch.setattr(state.channels.store, "web", blocked)
            request = asyncio.create_task(
                client.post(
                    "/api/messages",
                    json={"session_id": session, "prompt": "second work", "message_id": M2},
                    headers=headers,
                )
            )
            await entered.wait()
            # The HTTP caller disconnects after the admission commit; the shielded
            # policy must still stop the same-session run.
            request.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert active.task.cancelling() or state.active is not active

            # Clean up the follow-up run the worker now owns.
            async with asyncio.timeout(HANG_GUARD):
                while state.active is None or state.active is active:
                    await asyncio.sleep(0.01)
            second = state.active
            if second is not None:
                canceled = await client.post("/api/cancel", json={"run_id": second.id}, headers=headers)
                assert canceled.status_code == 200

    asyncio.run(check())
