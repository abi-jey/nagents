"""Private short child-conversation identity allocation and title validation."""

from __future__ import annotations

import asyncio
import re
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
from nagents.harness.session_identity import validate_session_id
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


def test_session_id_validation_accepts_uuid_public_ids_and_legacy_ids() -> None:
    public_id = "s-a7f2c9d1"
    assert validate_session_id(public_id) == public_id
    assert validate_session_id("s1") == "s1"
    assert session_sequence("s1") == 1

    for value in ("s-A7f2c9d1", "s-a7f2c9d", "s-a7f2c9d10", "s-1234567g", "s", "s0"):
        with pytest.raises(ValueError, match="session_id"):
            validate_session_id(value)


def test_title_is_trimmed_unicode_bounded_and_rejects_controls() -> None:
    assert validate_title("  Review 雪 🛰️  ") == "Review 雪 🛰️"
    assert validate_title("\u3000Review 👩‍💻\u3000") == "Review 👩‍💻"
    assert validate_title("雪" * MAX_TITLE) == "雪" * MAX_TITLE
    assert validate_title(" ") == ""

    for value in (
        "x" * (MAX_TITLE + 1),
        "multi\nline",
        "tab\there",
        "delete\x7f",
        "C1\x85control",
        "line\u2028separator",
        "paragraph\u2029separator",
        "bad\ud800",
    ):
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

    assert all(re.fullmatch(r"s-[0-9a-f]{8}", identity.session_id) for identity in identities)
    assert len({identity.session_id for identity in identities}) == len(requests)
    assert len({identity.storage_id for identity in identities}) == len(requests)

    reopened = SessionIdentityStore(path)
    next_identity = await reopened.reserve("root-0", identities[0].session_id, "Follow-up")
    assert re.fullmatch(r"s-[0-9a-f]{8}", next_identity.session_id)
    assert next_identity.session_id not in {identity.session_id for identity in identities}


@pytest.mark.asyncio
async def test_deleted_mapping_keeps_public_id_reserved_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    first_public = uuid.UUID(hex="a7f2c9d11234567890abcdef12345678")
    first_storage = uuid.UUID(hex="0123456789abcdef0123456789abcdef")
    with patch.object(uuid, "uuid4", side_effect=(first_public, first_storage)):
        first = await SessionIdentityStore(path).reserve("root-a", "", "First")

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM nagents_agent_sessions WHERE public_id = ?", (first.session_id,))

    collision_public = uuid.UUID(hex="a7f2c9d1" + "0" * 24)
    unused_storage = uuid.UUID(hex="10000000000000000000000000000000")
    replacement_public = uuid.UUID(hex="0123456789abcdef0123456789abcdef")
    replacement_storage = uuid.UUID(hex="fedcba9876543210fedcba9876543210")
    with patch.object(
        uuid, "uuid4", side_effect=(collision_public, unused_storage, replacement_public, replacement_storage)
    ):
        next_identity = await SessionIdentityStore(path).reserve("root-b", "", "Second")
    assert next_identity.session_id == "s-01234567"
    assert next_identity.session_id != first.session_id
    with closing(sqlite3.connect(path)) as db:
        reserved = {row[0] for row in db.execute("SELECT public_id FROM nagents_agent_session_ids")}
    assert reserved == {first.session_id, next_identity.session_id}


@pytest.mark.asyncio
async def test_public_id_uses_first_eight_hex_characters_of_generated_uuid(tmp_path: Path) -> None:
    public_uuid = uuid.UUID(hex="a7f2c9d11234567890abcdef12345678")
    storage_uuid = uuid.UUID(hex="0123456789abcdef0123456789abcdef")
    with patch.object(uuid, "uuid4", side_effect=(public_uuid, storage_uuid)):
        identity = await SessionIdentityStore(tmp_path / "agent-sessions.db").reserve("root", "", "Exact")
    assert identity.session_id == "s-a7f2c9d1"
    assert identity.storage_id == "ngn-0123456789abcdef"


@pytest.mark.asyncio
async def test_public_prefix_collision_retries_and_keeps_existing_mapping(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    existing_public = uuid.UUID(hex="cafebabedeadbeef1234567890abcdef")
    existing_storage = uuid.UUID(hex="1234567890abcdef1234567890abcdef")
    with patch.object(uuid, "uuid4", side_effect=(existing_public, existing_storage)):
        existing = await SessionIdentityStore(path).reserve("root", "", "Existing")
    collision_uuid = uuid.UUID(hex="cafebabe" + "f" * 24)
    unused_storage = uuid.UUID(hex="10000000000000000000000000000000")
    replacement_uuid = uuid.UUID(hex="fedcba9876543210fedcba9876543210")
    replacement_storage = uuid.UUID(hex="0123456789abcdef0123456789abcdef")
    with patch.object(
        uuid,
        "uuid4",
        side_effect=(collision_uuid, unused_storage, replacement_uuid, replacement_storage),
    ):
        allocated = await SessionIdentityStore(path).reserve("root", "", "Replacement")

    assert allocated.session_id == "s-fedcba98"
    assert allocated.session_id != existing.session_id
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT public_id, storage_id FROM nagents_agent_sessions ORDER BY public_id").fetchall()
    assert {row[0] for row in rows} == {existing.session_id, allocated.session_id}
    assert len({row[1] for row in rows}) == 2


@pytest.mark.asyncio
async def test_repeated_public_prefix_collision_fails_without_ack_or_extra_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent-sessions.db"
    existing_public = uuid.UUID(hex="cafebabedeadbeef1234567890abcdef")
    existing_storage = uuid.UUID(hex="1234567890abcdef1234567890abcdef")
    with patch.object(uuid, "uuid4", side_effect=(existing_public, existing_storage)):
        existing = await SessionIdentityStore(path).reserve("root", "", "Existing")
    uuids = tuple(
        value
        for index in range(5)
        for value in (
            uuid.UUID(hex="cafebabe" + f"{index:024x}"),
            uuid.UUID(hex=f"{index + 1:032x}"),
        )
    )
    with (
        patch.object(uuid, "uuid4", side_effect=uuids) as uuid4,
        pytest.raises(RuntimeError, match="unique child conversation"),
    ):
        await SessionIdentityStore(path).reserve("root", "", "No alias")

    assert uuid4.call_count == 10
    with closing(sqlite3.connect(path)) as db:
        mappings = db.execute("SELECT public_id, storage_id FROM nagents_agent_sessions").fetchall()
        reserved = db.execute("SELECT public_id FROM nagents_agent_session_ids").fetchall()
    assert mappings == [(existing.session_id, existing.storage_id)]
    assert reserved == [(existing.session_id,)]


@pytest.mark.asyncio
async def test_legacy_identity_migrates_without_changing_id_title_root_or_storage(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    legacy_storage = "ngn-0123456789abcdef"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE nagents_agent_sessions ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, root_session_id TEXT NOT NULL, "
            "storage_id TEXT NOT NULL UNIQUE, parent_session_id TEXT NOT NULL, title TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO nagents_agent_sessions(sequence, root_session_id, storage_id, parent_session_id, title) "
            "VALUES (?, ?, ?, ?, ?)",
            (8, "root-legacy", legacy_storage, "s1", "Legacy title"),
        )

    fresh = await SessionIdentityStore(path).reserve("root-new", "", "New")
    assert re.fullmatch(r"s-[0-9a-f]{8}", fresh.session_id)
    with closing(sqlite3.connect(path)) as db:
        legacy = db.execute(
            "SELECT public_id, sequence, root_session_id, storage_id, parent_session_id, title "
            "FROM nagents_agent_sessions WHERE sequence = 8"
        ).fetchone()
        tombstones = {row[0] for row in db.execute("SELECT public_id FROM nagents_agent_session_ids")}
    assert legacy == ("s8", 8, "root-legacy", legacy_storage, "s1", "Legacy title")
    assert tombstones == {"s8", fresh.session_id}


@pytest.mark.asyncio
async def test_storage_uuid_collision_retries_without_aliasing(tmp_path: Path) -> None:
    store = SessionIdentityStore(tmp_path / "agent-sessions.db")
    existing = await store.reserve("root", "", "Existing")
    collision = uuid.UUID(hex=existing.storage_id.removeprefix("ngn-") + "0" * 16)
    replacement_public = uuid.UUID(hex="abcdef0123456789abcdef0123456789")
    replacement_storage = uuid.UUID(hex="0123456789abcdef0123456789abcdef")
    with patch.object(
        uuid,
        "uuid4",
        side_effect=(
            uuid.UUID(hex="10000000000000000000000000000000"),
            collision,
            replacement_public,
            replacement_storage,
        ),
    ):
        allocated = await store.reserve("root", "", "New")

    assert allocated.storage_id != existing.storage_id
    assert allocated.session_id != existing.session_id
    assert allocated.session_id == "s-abcdef01"
    assert re.fullmatch(r"s-[0-9a-f]{8}", allocated.session_id)


@pytest.mark.asyncio
async def test_repeated_storage_uuid_collision_fails_without_creating_an_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent-sessions.db"
    existing_public = uuid.UUID(hex="cafebabedeadbeef1234567890abcdef")
    existing_storage = uuid.UUID(hex="0123456789abcdef1234567890abcdef")
    with patch.object(uuid, "uuid4", side_effect=(existing_public, existing_storage)):
        existing = await SessionIdentityStore(path).reserve("root", "", "Existing")
    collision = uuid.UUID(hex=existing.storage_id.removeprefix("ngn-") + "0" * 16)
    collision_attempts = tuple(
        value
        for index in range(5)
        for value in (
            uuid.UUID(hex=f"{index + 1:032x}"),
            collision,
        )
    )
    with (
        patch.object(uuid, "uuid4", side_effect=collision_attempts) as uuid4,
        pytest.raises(RuntimeError, match="unique child conversation"),
    ):
        await SessionIdentityStore(path).reserve("root", "", "No alias")

    assert uuid4.call_count == 10

    with closing(sqlite3.connect(path)) as db:
        count = db.execute("SELECT COUNT(*) FROM nagents_agent_sessions").fetchone()[0]
        reserved_count = db.execute("SELECT COUNT(*) FROM nagents_agent_session_ids").fetchone()[0]
    assert count == 1
    assert reserved_count == 1


@pytest.mark.asyncio
async def test_save_title_preserves_identity_and_cannot_update_another_root(tmp_path: Path) -> None:
    path = tmp_path / "agent-sessions.db"
    store = SessionIdentityStore(path)
    identity = await store.reserve("root-a", "", "Before")

    await store.save_title("root-a", identity.session_id, "  After  ")
    with closing(sqlite3.connect(path)) as db:
        updated = db.execute(
            "SELECT public_id, sequence, storage_id, root_session_id, title "
            "FROM nagents_agent_sessions WHERE public_id = ?",
            (identity.session_id,),
        ).fetchone()
    assert updated == (identity.session_id, 1, identity.storage_id, "root-a", "After")

    with pytest.raises(ValueError, match="unavailable"):
        await store.save_title("root-b", identity.session_id, "Foreign edit")
    with closing(sqlite3.connect(path)) as db:
        unchanged = db.execute(
            "SELECT public_id, sequence, storage_id, root_session_id, title "
            "FROM nagents_agent_sessions WHERE public_id = ?",
            (identity.session_id,),
        ).fetchone()
    assert unchanged == updated
