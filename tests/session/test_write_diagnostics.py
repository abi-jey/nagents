"""Session write diagnostics stay bounded and failed inserts roll back."""

import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nagents.session import SessionManager
from nagents.types import Message


def test_failed_message_insert_logs_phase_without_payload_and_rolls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def check() -> None:
        path = tmp_path / "sessions.sqlite"
        manager = SessionManager(path)
        await manager.get_or_create_session("root", "user")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TRIGGER reject_message AFTER INSERT ON v2_messages "
                "BEGIN SELECT RAISE(ABORT, 'SECRET SQL detail and database path'); END"
            )

        caplog.clear()
        with pytest.raises(sqlite3.IntegrityError):
            await manager.add_message("root", Message(role="user", content="SECRET private prompt"))

        records = [record for record in caplog.records if "Session message write failed:" in record.message]
        assert len(records) == 1
        message = records[0].message
        assert "phase=insert" in message
        assert "exception=IntegrityError" in message
        assert f"sqlite_code={sqlite3.SQLITE_CONSTRAINT_TRIGGER}" in message
        assert "elapsed_ms=" in message
        assert "SECRET" not in caplog.text
        assert str(path) not in caplog.text and "private prompt" not in caplog.text

        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT COUNT(*) FROM v2_messages WHERE session_id = 'root'").fetchone() == (0,)

    with caplog.at_level(logging.WARNING, logger="nagents.session.manager"):
        asyncio.run(check())
