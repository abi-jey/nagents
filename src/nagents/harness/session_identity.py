"""Private, durable allocation of compact child-conversation identities.

The workspace database owns a monotonic sequence shared by every root tree.
Aliases never resolve by title, profile, display text, or a provider's task ID.
The storage conversation ID is an implementation detail, not a tool input.
"""

from __future__ import annotations

import asyncio
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
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise ValueError("Child conversation sequence must fit a positive signed 64-bit integer")
    digits: list[str] = []
    while sequence:
        sequence, digit = divmod(sequence, 36)
        digits.append(_DIGITS[digit])
    return "s" + "".join(reversed(digits))


def session_sequence(session_id: str) -> int:
    if (
        not isinstance(session_id, str)
        or not 2 <= len(session_id) <= 14
        or not session_id.startswith("s")
        or session_id[1] == "0"
        or any(character not in _DIGITS for character in session_id[1:])
    ):
        raise ValueError("Use the short session_id returned by to_agent, such as s1")
    sequence = int(session_id[1:], 36)
    if sequence > 2**63 - 1:
        raise ValueError("Child session_id is outside the supported range")
    return sequence


@dataclass(frozen=True)
class ChildIdentity:
    session_id: str
    storage_id: str


class SessionIdentityStore:
    """Reserve IDs before publishing an acknowledgement; never recycle them.

    SQLite's AUTOINCREMENT high-water mark survives cleanup and process restarts.
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
            session_sequence(parent_session_id)

        def allocate() -> ChildIdentity:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS nagents_agent_sessions ("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, root_session_id TEXT NOT NULL, "
                    "storage_id TEXT NOT NULL UNIQUE, parent_session_id TEXT NOT NULL, title TEXT NOT NULL)"
                )
                for _ in range(5):
                    storage_id = "ngn-" + uuid.uuid4().hex[:16]
                    try:
                        cursor = db.execute(
                            "INSERT INTO nagents_agent_sessions(root_session_id, storage_id, parent_session_id, title) "
                            "VALUES (?, ?, ?, ?)",
                            (root_session_id, storage_id, parent_session_id, title),
                        )
                    except sqlite3.IntegrityError as error:
                        if getattr(error, "sqlite_errorcode", 0) != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                            raise
                        continue
                    sequence = cursor.lastrowid
                    if sequence is None:
                        raise RuntimeError("Child conversation allocation returned no identity")
                    return ChildIdentity(short_session_id(sequence), storage_id)
                raise RuntimeError("Could not allocate a unique child conversation; no session was started")

        return await write_database(self.path, lambda: asyncio.to_thread(allocate))

    async def save_title(self, root_session_id: str, session_id: str, title: str) -> None:
        sequence, title = session_sequence(session_id), validate_title(title)

        def save() -> None:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                cursor = db.execute(
                    "UPDATE nagents_agent_sessions SET title = ? WHERE sequence = ? AND root_session_id = ?",
                    (title, sequence, root_session_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Child conversation is unavailable in this root tree")

        await write_database(self.path, lambda: asyncio.to_thread(save))
