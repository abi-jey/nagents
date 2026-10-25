"""Private, durable allocation of compact child-conversation identities.

The workspace database atomically reserves UUID prefixes across every root tree.
Aliases never resolve by title, profile, display text, or a provider's task ID.
The storage conversation ID is an implementation detail, not a tool input.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nagents._sqlite import write_database

if TYPE_CHECKING:
    from pathlib import Path

MAX_TITLE = 120
SESSION_ID_PATTERN = r"s(?:-[0-9a-f]{8}|[1-9a-z][0-9a-z]{0,12})"
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def validate_title(title: str) -> str:
    if not isinstance(title, str):
        raise ValueError("Agent title must be text")
    value = title.strip()
    if len(value) > MAX_TITLE or any(
        unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"} for character in value
    ):
        raise ValueError(f"Agent title must be one line of at most {MAX_TITLE} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Agent title must contain valid Unicode text") from None
    return value


def short_session_id(sequence: int) -> str:
    """Reconstruct an existing base-36 alias during the schema upgrade."""
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise ValueError("Child conversation sequence must fit a positive signed 64-bit integer")
    digits: list[str] = []
    while sequence:
        sequence, digit = divmod(sequence, 36)
        digits.append(_DIGITS[digit])
    return "s" + "".join(reversed(digits))


def session_sequence(session_id: str) -> int:
    """Validate the original base-36 format without reassigning its identity."""
    if (
        not isinstance(session_id, str)
        or not 2 <= len(session_id) <= 14
        or not session_id.startswith("s")
        or session_id[1] == "0"
        or any(character not in _DIGITS for character in session_id[1:])
    ):
        raise ValueError("Use the exact short session_id returned by to_agent")
    sequence = int(session_id[1:], 36)
    if sequence > 2**63 - 1:
        raise ValueError("Child session_id is outside the supported range")
    return sequence


def validate_session_id(session_id: str) -> str:
    """Accept new UUID prefixes and saved base-36 aliases, never display titles."""
    if not isinstance(session_id, str) or not re.fullmatch(SESSION_ID_PATTERN, session_id):
        raise ValueError("Use the exact short session_id returned by to_agent, such as s-a7f2c9d1")
    if session_id.startswith("s-"):
        return session_id
    session_sequence(session_id)
    return session_id


def initialize_identity_schema(db: sqlite3.Connection) -> None:
    """Upgrade within the caller's write transaction, preserving every old alias.

    Reservations contain only opaque public IDs. They outlive deleted mapping
    rows so purging a conversation cannot assign its ID to another owner.
    """
    db.execute(
        "CREATE TABLE IF NOT EXISTS nagents_agent_sessions ("
        "sequence INTEGER PRIMARY KEY AUTOINCREMENT, root_session_id TEXT NOT NULL, "
        "storage_id TEXT NOT NULL UNIQUE, parent_session_id TEXT NOT NULL, title TEXT NOT NULL, "
        "public_id TEXT NOT NULL)"
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(nagents_agent_sessions)")}
    if "public_id" not in columns:
        db.execute("ALTER TABLE nagents_agent_sessions ADD COLUMN public_id TEXT")
    for row in db.execute("SELECT sequence FROM nagents_agent_sessions WHERE public_id IS NULL").fetchall():
        db.execute(
            "UPDATE nagents_agent_sessions SET public_id = ? WHERE sequence = ?",
            (short_session_id(int(row[0])), row[0]),
        )
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS nagents_agent_public_id ON nagents_agent_sessions(public_id)")
    db.execute("CREATE TABLE IF NOT EXISTS nagents_agent_session_ids (public_id TEXT PRIMARY KEY NOT NULL)")
    db.execute("INSERT OR IGNORE INTO nagents_agent_session_ids SELECT public_id FROM nagents_agent_sessions")


@dataclass(frozen=True)
class ChildIdentity:
    session_id: str
    storage_id: str


class SessionIdentityStore:
    """Reserve IDs before publishing an acknowledgement; never recycle them.

    Opaque reservations survive cleanup and process restarts.
    Existing retained runtimes still expire on shutdown; this does not replay or
    reconstruct workers. Keeping the allocation prevents their IDs being reused.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    async def reserve(self, root_session_id: str, parent_session_id: str, title: str) -> ChildIdentity:
        if not isinstance(root_session_id, str) or not root_session_id.strip():
            raise ValueError("A root conversation identity is required")
        title = validate_title(title)
        if parent_session_id:
            validate_session_id(parent_session_id)

        def allocate() -> ChildIdentity:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                initialize_identity_schema(db)
                for _ in range(5):
                    public_id = "s-" + uuid.uuid4().hex[:8]
                    storage_id = "ngn-" + uuid.uuid4().hex[:16]
                    db.execute("SAVEPOINT identity_candidate")
                    try:
                        db.execute("INSERT INTO nagents_agent_session_ids VALUES (?)", (public_id,))
                        db.execute(
                            "INSERT INTO nagents_agent_sessions(root_session_id, storage_id, parent_session_id, title, public_id) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (root_session_id, storage_id, parent_session_id, title, public_id),
                        )
                    except sqlite3.IntegrityError as error:
                        db.execute("ROLLBACK TO identity_candidate")
                        db.execute("RELEASE identity_candidate")
                        if getattr(error, "sqlite_errorcode", 0) not in {
                            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
                            sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                        }:
                            raise
                        continue
                    db.execute("RELEASE identity_candidate")
                    return ChildIdentity(public_id, storage_id)
                raise RuntimeError("Could not allocate a unique child conversation; no session was started")

        return await write_database(self.path, lambda: asyncio.to_thread(allocate))

    async def save_title(self, root_session_id: str, session_id: str, title: str) -> None:
        session_id, title = validate_session_id(session_id), validate_title(title)

        def save() -> None:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                initialize_identity_schema(db)
                cursor = db.execute(
                    "UPDATE nagents_agent_sessions SET title = ? WHERE public_id = ? AND root_session_id = ?",
                    (title, session_id, root_session_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Child conversation is unavailable in this root tree")

        await write_database(self.path, lambda: asyncio.to_thread(save))
