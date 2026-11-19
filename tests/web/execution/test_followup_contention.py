"""Contention on an optional follow-up cannot fail or duplicate an active turn."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from contextlib import closing
from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.web.execution.service import Run
from nagents.web.execution.service import WebState
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import stop_maintenance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    from nagents.web.http.routing import Work


@asynccontextmanager
async def active_run(tmp_path: Path) -> AsyncIterator[tuple[WebState, Run]]:
    async with client_app(tmp_path) as (app, _, _, _):
        state = cast("WebState", app.state.web)
        await stop_maintenance(state)
        worker = state.channels.tasks[0]
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        run = Run(state.selected_session_id, server_owned=True)
        state.active = run
        try:
            yield state, run
        finally:
            state.active = None


_connect = sqlite3.connect


@contextmanager
def short_sqlite_wait() -> Iterator[None]:
    def connect(path: str | Path, timeout: float = 5) -> sqlite3.Connection:
        # Change only test connections while a deliberate blocker owns the lock.
        return _connect(path, timeout=0)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        yield


def statuses(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return db.execute("SELECT message_id, status FROM ngn_web_inbox ORDER BY id").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("lock", ["empty", "probe", "begin", "commit"])
async def test_busy_followup_retries_on_next_boundary_in_fifo_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, lock: str
) -> None:
    async with active_run(tmp_path) as (state, run):
        store = state.channels.store
        inputs = state.queued_inputs
        if lock != "empty":
            for identifier in ("first", "second"):
                assert (await store.web(run.session_id, identifier, identifier))[1]

        admitted: list[str] = []
        observed = inputs.admitted

        def admit(run: Run, work: Work, *, initial: bool) -> None:
            admitted.append(work.message_id)
            observed(run, work, initial=initial)

        # No maintenance or inbox worker can contend with these explicit locks.
        with pytest.MonkeyPatch.context() as patch, closing(_connect(store.db_path, timeout=0)) as blocker:
            patch.setattr(inputs, "admitted", admit)
            if lock == "probe":
                blocker.execute("BEGIN EXCLUSIVE")
            elif lock == "commit":
                # A read lock allows BEGIN IMMEDIATE and UPDATE but denies COMMIT.
                blocker.execute("BEGIN")
                statuses(blocker)
            else:
                blocker.execute("BEGIN IMMEDIATE")
            with short_sqlite_wait():
                assert await inputs.pull(run) is None
            assert not inputs.claimed and not admitted
            assert statuses(blocker) == ([] if lock == "empty" else [("first", "queued"), ("second", "queued")])
            assert ("Follow-up claim deferred" in caplog.text) == (lock != "empty")
            blocker.rollback()

            if lock == "empty":
                return
            for identifier in ("first", "second"):
                result = await inputs.pull(run)
                assert result is not None and result.message_id == identifier
            assert await inputs.pull(run) is None
            assert admitted == ["first", "second"]
            assert statuses(blocker) == [("first", "running"), ("second", "running")]
            await inputs.finish(run)
            assert statuses(blocker) == [("first", "completed"), ("second", "completed")]


@pytest.mark.asyncio
async def test_followup_probe_does_not_choose_past_new_fifo_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with active_run(tmp_path) as (state, run):
        store = state.channels.store
        await store.web(run.session_id, "initial", "initial")
        original = store._has_candidate

        async def changed(predicate: str, parameters: tuple[str | bool, ...]) -> bool:
            assert await original(predicate, parameters)
            # An intervening transaction invalidates the positive probe's view
            # and inserts a queue barrier before another steerable candidate.
            with closing(_connect(store.db_path)) as db, db:
                db.execute("DELETE FROM ngn_web_inbox WHERE message_id = 'initial'")
            await store.web(run.session_id, "barrier", "barrier", delivery="queue")
            await store.web(run.session_id, "later", "later", delivery="steer")
            return True

        monkeypatch.setattr(store, "_has_candidate", changed)
        assert await state.queued_inputs.pull(run) is None
        with closing(_connect(store.db_path)) as db:
            assert statuses(db) == [("barrier", "queued"), ("later", "queued")]


@pytest.mark.asyncio
async def test_cancellation_wins_over_deferred_followup_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with active_run(tmp_path) as (state, run):
        store = state.channels.store
        await store.web(run.session_id, "pending", "pending")
        original = store._has_candidate
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(predicate: str, parameters: tuple[str | bool, ...]) -> bool:
            candidate = await original(predicate, parameters)
            entered.set()
            await release.wait()
            return candidate

        monkeypatch.setattr(store, "_has_candidate", held)
        with closing(_connect(store.db_path)) as blocker, short_sqlite_wait():
            blocker.execute("BEGIN IMMEDIATE")
            task = asyncio.create_task(state.queued_inputs.pull(run))
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                    task.cancel()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert statuses(blocker) == [("pending", "queued")]
                assert not state.queued_inputs.claimed
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("post_claim", [False, True])
async def test_other_errors_remain_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, post_claim: bool) -> None:
    async with active_run(tmp_path) as (state, run):
        store = state.channels.store
        await store.web(run.session_id, "pending", "pending")
        original = store.validate_work

        def bad_schema(db: sqlite3.Connection) -> None:
            db.execute("SELECT * FROM deliberately_missing")

        async def busy_validation(work: Work) -> None:
            with closing(_connect(store.db_path)) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                await original(work)

        if post_claim:
            monkeypatch.setattr(store, "validate_work", busy_validation)
        else:
            monkeypatch.setattr(store, "_quarantine_work", bad_schema)
        with short_sqlite_wait(), pytest.raises(sqlite3.OperationalError) as failure:
            await state.queued_inputs.pull(run)
        assert failure.value.sqlite_errorcode == (sqlite3.SQLITE_BUSY if post_claim else sqlite3.SQLITE_ERROR)
        with closing(_connect(store.db_path)) as db:
            assert statuses(db) == [("pending", "running" if post_claim else "queued")]
        assert bool(state.queued_inputs.claimed) is post_claim
        await state.queued_inputs.finish(run)
