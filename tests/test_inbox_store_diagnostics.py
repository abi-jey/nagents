"""SQLite phase diagnostics preserve transactions and never disclose their data."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from nagents.channels.store import InboxStore
from tests.support.hang_guard import HANG_GUARD


def database(path: Path) -> InboxStore:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE probe(id INTEGER)")
        db.execute("INSERT INTO probe VALUES (1)")
    return InboxStore(path, "diagnostic", 10)


@pytest.mark.parametrize("phase", ["begin", "body", "commit"])
def test_sqlite_failure_phase_is_precise_and_failed_write_rolls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, phase: str
) -> None:
    store = database(tmp_path / "SECRET-private-database.db")
    connect = sqlite3.connect
    calls: list[bool] = []

    def operation(db: sqlite3.Connection) -> int:
        calls.append(True)
        db.execute("INSERT INTO probe VALUES (2)")
        if phase == "body":
            db.execute("SELECT * FROM SECRET_private_table")
        return 2

    def no_wait(path: str | Path, timeout: float = 5) -> sqlite3.Connection:
        return connect(path, timeout=0)

    with closing(connect(store.db_path)) as blocker:
        if phase != "body":
            blocker.execute("BEGIN IMMEDIATE" if phase == "begin" else "BEGIN")
            blocker.execute("SELECT * FROM probe").fetchall()
        with (
            patch("nagents.channels.store.sqlite3.connect", side_effect=no_wait),
            pytest.raises(sqlite3.OperationalError) as failure,
        ):
            asyncio.run(store._transaction(operation))
        assert failure.value.sqlite_errorcode == (sqlite3.SQLITE_ERROR if phase == "body" else sqlite3.SQLITE_BUSY)
        blocker.rollback()
        assert blocker.execute("SELECT id FROM probe").fetchall() == [(1,)]
    assert bool(calls) == (phase != "begin")
    records = [record for record in caplog.records if record.name == "nagents.channels.store"]
    assert len(records) == 1 and records[0].exc_info is None
    assert f"phase={phase} exception=OperationalError sqlite_code=" in records[0].message
    assert "SECRET" not in caplog.text and "INSERT" not in caplog.text and str(tmp_path) not in caplog.text


@pytest.mark.parametrize("fail", [False, True])
def test_phase_logging_preserves_owned_transaction_cancellation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fail: bool
) -> None:
    store = database(tmp_path / "owned.db")

    async def check() -> None:
        entered = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        calls = 0

        def operation(db: sqlite3.Connection) -> int:
            nonlocal calls
            calls += 1
            db.execute("INSERT INTO probe VALUES (2)")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(HANG_GUARD)
            if fail:
                db.execute("SELECT * FROM SECRET_private_table")
            return 2

        owner = asyncio.create_task(store._transaction(operation))
        try:
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
            owner.cancel()
            assert owner.cancelling() and not owner.done()
            release.set()
            if fail:
                # Existing ownership semantics expose an operation failure
                # before remembered cancellation; diagnostics must not change it.
                with pytest.raises(sqlite3.OperationalError) as failure:
                    await owner
                assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
            else:
                with pytest.raises(asyncio.CancelledError):
                    await owner
            assert calls == 1
        finally:
            release.set()
            await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(check())
    with closing(sqlite3.connect(store.db_path)) as db:
        assert db.execute("SELECT id FROM probe ORDER BY id").fetchall() == ([(1,)] if fail else [(1,), (2,)])
    messages = [record.message for record in caplog.records if record.name == "nagents.channels.store"]
    assert messages == (
        ["SQLite transaction failed: phase=body exception=OperationalError sqlite_code=1"] if fail else []
    )
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("code", [True, "SECRET", {"SECRET": "payload"}, -1, 2**64])
def test_untrusted_sqlite_code_is_replaced_by_fixed_sentinel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, code: object
) -> None:
    store = database(tmp_path / "private.db")
    error = sqlite3.OperationalError("SECRET provider payload")
    # Deliberately bypass the driver's integer contract to test malformed diagnostics.
    vars(error)["sqlite_errorcode"] = code

    def operation(db: sqlite3.Connection) -> None:
        raise error

    with pytest.raises(sqlite3.OperationalError) as failure:
        asyncio.run(store._transaction(operation))
    assert failure.value is error
    assert "phase=body exception=OperationalError sqlite_code=unavailable" in caplog.text
    assert "SECRET" not in caplog.text


def test_diagnostic_property_failure_does_not_replace_transaction_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenCode(sqlite3.OperationalError):
        pass

    def broken_code(error: BrokenCode) -> int:
        raise RuntimeError("SECRET diagnostic failure")

    store = database(tmp_path / "private.db")
    error = BrokenCode("SECRET original error")

    def operation(db: sqlite3.Connection) -> None:
        raise error

    with (
        patch.object(BrokenCode, "sqlite_errorcode", property(broken_code), create=True),
        pytest.raises(BrokenCode) as failure,
    ):
        asyncio.run(store._transaction(operation))
    assert failure.value is error
    assert "phase=body exception=BrokenCode sqlite_code=unavailable" in caplog.text
    assert "SECRET" not in caplog.text
