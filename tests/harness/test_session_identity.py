"""Private short child-conversation identity allocation and title validation."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.harness.session_identity import MAX_TITLE
from nagents.harness.session_identity import SessionIdentityStore
from nagents.harness.session_identity import session_sequence
from nagents.harness.session_identity import short_session_id
from nagents.harness.session_identity import validate_title

if TYPE_CHECKING:
    from pathlib import Path


def test_base36_short_session_ids_round_trip_and_parse_strictly() -> None:
    assert [short_session_id(value) for value in (1, 35, 36, 71)] == ["s1", "sz", "s10", "s1z"]
    maximum = 2**63 - 1
    assert session_sequence(short_session_id(maximum)) == maximum

    for numeric_value in (True, False, 0, -1, 1.5):
        with pytest.raises(ValueError, match="positive"):
            short_session_id(numeric_value)  # type: ignore[arg-type]

    invalid = (None, 1, "", "s", "S1", "s0", "s01", "sA", "s!", "s" + "z" * 14)
    for invalid_value in invalid:
        with pytest.raises(ValueError, match="session_id"):
            session_sequence(invalid_value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive signed 64-bit"):
        short_session_id(maximum + 1)
    with pytest.raises(ValueError, match="supported range"):
        session_sequence("s1y2p0ij32e8e8")


def test_title_is_trimmed_unicode_bounded_and_rejects_controls() -> None:
    assert validate_title("  Review 雪 🛰️  ") == "Review 雪 🛰️"
    assert validate_title("雪" * MAX_TITLE) == "雪" * MAX_TITLE
    assert validate_title(" ") == ""

    for value in ("x" * (MAX_TITLE + 1), "multi\nline", "tab\there", "delete\x7f", "bad\ud800"):
        with pytest.raises(ValueError, match="title"):
            validate_title(value)
    with pytest.raises(ValueError, match="title"):
        validate_title(12)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_concurrent_reservations_are_unique_across_roots_and_reopened_store(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    store = SessionIdentityStore(path)
    requests = [(f"root-{index % 3}", "", "Shared title") for index in range(24)]
    identities = await asyncio.gather(*(store.reserve(root, parent, title) for root, parent, title in requests))

    sequences = {session_sequence(identity.session_id) for identity in identities}
    assert sequences == set(range(1, len(requests) + 1))
    assert len({identity.session_id for identity in identities}) == len(requests)
    assert len({identity.storage_id for identity in identities}) == len(requests)

    reopened = SessionIdentityStore(path)
    next_identity = await reopened.reserve("root-0", identities[0].session_id, "Follow-up")
    assert session_sequence(next_identity.session_id) == len(requests) + 1
    assert next_identity.session_id not in {identity.session_id for identity in identities}


@pytest.mark.asyncio
async def test_autoincrement_high_water_is_not_reused_after_allocation_row_delete(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    store = SessionIdentityStore(path)
    first = await store.reserve("root-a", "", "First")
    first_sequence = session_sequence(first.session_id)

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM nagents_agent_sessions WHERE sequence = ?", (first_sequence,))

    next_identity = await SessionIdentityStore(path).reserve("root-b", "", "Second")
    assert session_sequence(next_identity.session_id) == first_sequence + 1


@pytest.mark.asyncio
async def test_storage_uuid_collision_retries_without_aliasing(tmp_path: Path) -> None:
    store = SessionIdentityStore(tmp_path / "agent-sessions.db")
    existing = await store.reserve("root", "", "Existing")
    collision = uuid.UUID(hex=existing.storage_id.removeprefix("ngn-") + "0" * 16)
    replacement = uuid.UUID(hex="0123456789abcdef0123456789abcdef")
    with patch.object(uuid, "uuid4", side_effect=(collision, replacement)):
        allocated = await store.reserve("root", "", "New")

    assert allocated.storage_id != existing.storage_id
    assert allocated.session_id != existing.session_id
    assert session_sequence(allocated.session_id) == session_sequence(existing.session_id) + 1


@pytest.mark.asyncio
async def test_repeated_storage_uuid_collision_fails_without_creating_an_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent-sessions.db"
    store = SessionIdentityStore(path)
    existing = await store.reserve("root", "", "Existing")
    collision = uuid.UUID(hex=existing.storage_id.removeprefix("ngn-") + "0" * 16)
    with (
        patch.object(uuid, "uuid4", return_value=collision),
        pytest.raises(RuntimeError, match="unique child conversation"),
    ):
        await store.reserve("root", "", "No alias")

    with closing(sqlite3.connect(path)) as db:
        count = db.execute("SELECT COUNT(*) FROM nagents_agent_sessions").fetchone()[0]
    assert count == 1


@pytest.mark.asyncio
async def test_save_title_preserves_identity_and_cannot_update_another_root(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    store = SessionIdentityStore(path)
    identity = await store.reserve("root-a", "", "Before")
    sequence = session_sequence(identity.session_id)

    await store.save_title("root-a", identity.session_id, "  After  ")
    with closing(sqlite3.connect(path)) as db:
        updated = db.execute(
            "SELECT sequence, storage_id, root_session_id, title FROM nagents_agent_sessions WHERE sequence = ?",
            (sequence,),
        ).fetchone()
    assert updated == (sequence, identity.storage_id, "root-a", "After")

    with pytest.raises(ValueError, match="unavailable"):
        await store.save_title("root-b", identity.session_id, "Foreign edit")
    with closing(sqlite3.connect(path)) as db:
        unchanged = db.execute(
            "SELECT sequence, storage_id, root_session_id, title FROM nagents_agent_sessions WHERE sequence = ?",
            (sequence,),
        ).fetchone()
    assert unchanged == updated
