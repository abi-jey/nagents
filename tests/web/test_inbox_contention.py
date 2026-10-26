"""Real SQLite lock failures must not lose or duplicate unstarted inbox work."""

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar
from typing import cast
from unittest.mock import patch

import pytest

from nagents._async import finish_on_cancel
from nagents.web.channels.host import ChannelHost
from nagents.web.execution.service import Run
from nagents.web.execution.service import WebState
from nagents.web.routing import Work
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.support.web import stop_maintenance

T = TypeVar("T")


@pytest.mark.parametrize("phase", ["pending", "claim", "commit"])
@pytest.mark.parametrize("shutdown", [False, True])
def test_real_sqlite_busy_preserves_unstarted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, shutdown: bool
) -> None:
    async def check() -> None:
        start, claim_ready, claim_release, contended, admitted, backing_off, backoff_release = (
            asyncio.Event() for _ in range(7)
        )
        original_worker = ChannelHost.worker

        async def gated_worker(host: ChannelHost) -> None:
            await start.wait()
            await original_worker(host)

        monkeypatch.setattr(ChannelHost, "worker", gated_worker)
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            host = state.channels
            try:
                # The explicit blocker must determine BEGIN versus COMMIT failure;
                # an unrelated catalog/trash writer cannot participate in this race.
                await stop_maintenance(state)
                worker = host.tasks[0]
                root = state.selected_session_id
                identifier = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                result = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": identifier,
                        "prompt": "wait",
                    },
                )
                assert result.status_code == 200, result.text
                original_transaction = host.store._transaction
                body_finished: list[bool] = []
                busy_codes: list[int] = []

                async def observe_transaction(operation: Callable[[sqlite3.Connection], T]) -> T:
                    target = getattr(operation, "__name__", "") == ("pending" if phase == "pending" else "claim")

                    def observe_body(db: sqlite3.Connection) -> T:
                        value = operation(db)
                        if target:
                            body_finished.append(True)
                        return value

                    try:
                        return await original_transaction(observe_body)
                    except sqlite3.OperationalError as error:
                        if target:
                            busy_codes.append(error.sqlite_errorcode)
                            contended.set()
                        raise

                monkeypatch.setattr(host.store, "_transaction", observe_transaction)
                original_claim = host.claim

                async def pause_claim() -> Work | None:
                    claim_ready.set()
                    await claim_release.wait()
                    return await original_claim()

                if phase != "pending":
                    monkeypatch.setattr(host, "claim", pause_claim)
                    start.set()
                    async with asyncio.timeout(HANG_GUARD):
                        await claim_ready.wait()

                original_reserve = state.reserve_work
                reservations: list[int] = []

                def reserve(work: Work) -> Run:
                    reservations.append(work.id)
                    run = original_reserve(work)
                    admitted.set()
                    return run

                monkeypatch.setattr(state, "reserve_work", reserve)
                connect = sqlite3.connect

                def no_wait(database: str | Path, timeout: float = 5) -> sqlite3.Connection:
                    # Shorten only the test's SQLite busy handler; production stays at five seconds.
                    return connect(database, timeout=0)

                original_sleep = asyncio.sleep

                async def observe_backoff(delay: float, result: object = None) -> object:
                    if asyncio.current_task() is worker and delay == 0.25:
                        # Claim's owned transaction task observes BUSY before its
                        # shielded caller resumes. Inspect the parent at the actual
                        # backoff boundary, after it has handled that exception.
                        assert not state.mutating
                        backing_off.set()
                        await backoff_release.wait()
                    return await original_sleep(delay, result)

                with closing(connect(host.store.db_path, timeout=0)) as blocker:
                    blocker.execute("BEGIN" if phase == "commit" else "BEGIN IMMEDIATE")
                    if phase == "commit":
                        # A reader allows BEGIN IMMEDIATE and the claim UPDATE, but blocks COMMIT.
                        blocker.execute("SELECT id FROM ngn_web_inbox").fetchall()
                    with (
                        patch("nagents.channels.store.sqlite3.connect", side_effect=no_wait),
                        patch("asyncio.sleep", side_effect=observe_backoff),
                    ):
                        start.set()
                        claim_release.set()
                        try:
                            async with asyncio.timeout(HANG_GUARD):
                                await contended.wait()
                                await backing_off.wait()
                            assert busy_codes and all(code & 0xFF == sqlite3.SQLITE_BUSY for code in busy_codes)
                            assert bool(body_finished) == (phase == "commit")
                            assert not worker.done() and not reservations and not state.mutating
                            assert blocker.execute(
                                "SELECT status FROM ngn_web_inbox WHERE message_id = ?", (identifier,)
                            ).fetchone() == ("queued",)
                            if shutdown:
                                host.closed = True
                                backoff_release.set()
                                async with asyncio.timeout(HANG_GUARD):
                                    await worker
                                assert not reservations
                        finally:
                            backoff_release.set()
                    blocker.rollback()
                if not shutdown:
                    async with asyncio.timeout(HANG_GUARD):
                        await admitted.wait()
                    assert len(reservations) == 1 and not worker.done()
                    run = state.run_for(root)
                    assert run is not None and run.message_id == identifier
                    cancelled = await client.post("/api/cancel", headers=headers, json={"run_id": run.id})
                    assert cancelled.status_code == 200, cancelled.text
            finally:
                # Setup failures must not strand any artificial gate while
                # lifespan shutdown joins the inbox worker.
                host.closed = True
                start.set()
                claim_release.set()
                backoff_release.set()

    asyncio.run(check())


def test_non_busy_sqlite_failure_remains_fatal_and_logs_numeric_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original_worker = ChannelHost.worker

    async def invalid_schema(host: ChannelHost) -> None:
        async def pending(*, web_only: bool = False, available_channels: tuple[str, ...] | None = None) -> bool:
            def query(db: sqlite3.Connection) -> bool:
                return db.execute("SELECT * FROM SECRET_missing_table").fetchone() is not None

            return await host.store._transaction(query)

        monkeypatch.setattr(host.store, "has_pending", pending)
        await original_worker(host)

    monkeypatch.setattr(ChannelHost, "worker", invalid_schema)

    async def check() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            with pytest.raises(sqlite3.OperationalError) as failure:
                await state.channels.tasks[0]
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
            assert not state.mutating and not state.channels.work_tasks

    asyncio.run(check())
    assert "exception=OperationalError sqlite_code=1" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("phase", ["pending", "claim"])
def test_cancellation_wins_over_late_busy_from_owned_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def check() -> None:
        start, entered, release = (asyncio.Event() for _ in range(3))
        original_worker = ChannelHost.worker

        async def gated_worker(host: ChannelHost) -> None:
            await start.wait()
            await original_worker(host)

        monkeypatch.setattr(ChannelHost, "worker", gated_worker)
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            host, root = state.channels, state.selected_session_id
            worker = host.tasks[0]
            try:
                # Only the deliberate blocker may contend with the transaction
                # under test; background maintenance has separate coverage.
                await stop_maintenance(state)
                identifier = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
                response = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": identifier,
                        "prompt": "wait",
                    },
                )
                assert response.status_code == 200, response.text
                original_transaction = host.store._transaction
                failures: list[int] = []
                attempts = 0

                async def delayed_transaction(operation: Callable[[sqlite3.Connection], T]) -> T:
                    nonlocal attempts
                    name = getattr(operation, "__name__", "")
                    if name in {"pending", "claim"}:
                        assert not failures, "A cancelled inbox owner retried admission"
                    if name != phase:
                        return await original_transaction(operation)
                    attempts += 1

                    async def owned() -> T:
                        entered.set()
                        await release.wait()
                        try:
                            return await original_transaction(operation)
                        except sqlite3.OperationalError as error:
                            failures.append(error.sqlite_errorcode)
                            raise

                    # Preserve the real ownership helper: its failed task.result()
                    # can precede propagation of an already remembered cancellation.
                    return await finish_on_cancel(owned())

                monkeypatch.setattr(host.store, "_transaction", delayed_transaction)
                start.set()
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                connect = sqlite3.connect

                def no_wait(database: str | Path, timeout: float = 5) -> sqlite3.Connection:
                    return connect(database, timeout=0)

                with closing(connect(host.store.db_path, timeout=0)) as blocker:
                    blocker.execute("BEGIN IMMEDIATE")
                    with patch("nagents.channels.store.sqlite3.connect", side_effect=no_wait):
                        worker.cancel()
                        assert worker.cancelling() and not worker.done()
                        release.set()
                        async with asyncio.timeout(HANG_GUARD):
                            with pytest.raises(asyncio.CancelledError):
                                await worker
                    assert worker.cancelled() and attempts == 1 and failures == [sqlite3.SQLITE_BUSY]
                    assert not state.mutating and not state.executions.runs and not host.work_tasks
                    assert blocker.execute(
                        "SELECT status FROM ngn_web_inbox WHERE message_id = ?", (identifier,)
                    ).fetchone() == ("queued",)
            finally:
                # Lifespan shutdown joins this worker. Release both artificial
                # gates even if POST, the watchdog, connect, or BEGIN fails.
                # The blocker closes first, so cleanup cannot hold its lock.
                host.closed = True
                start.set()
                release.set()

    asyncio.run(check())
