"""
SQLite-based session and message history management.
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiosqlite

from ..types import Message
from ..types import ToolCall

logger = logging.getLogger(__name__)


class SessionManager:
    """
    SQLite-based session and message history management.

    Stores conversation history in a SQLite database, allowing for
    persistent sessions across restarts.

    Example:
        session = SessionManager(Path("sessions.db"))
        await session.initialize()

        await session.add_message("session-1", "user-1", Message(role="user", content="Hello"))
        history = await session.get_history("session-1")
    """

    def __init__(self, db_path: Path):
        """
        Initialize the session manager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._initialized = False

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        if self._initialized:
            return

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS v2_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS v2_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES v2_sessions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_v2_messages_session
                ON v2_messages(session_id);

                CREATE INDEX IF NOT EXISTS idx_v2_sessions_user
                ON v2_sessions(user_id);
            """
            )
            await db.commit()

        self._initialized = True
        logger.debug(f"Session database initialized at {self.db_path}")

    async def get_or_create_session(self, session_id: str, user_id: str) -> str:
        """
        Get or create a session.

        Args:
            session_id: Unique session identifier
            user_id: User identifier

        Returns:
            The session ID
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT id FROM v2_sessions WHERE id = ?", (session_id,))
            if await cursor.fetchone() is None:
                await db.execute(
                    "INSERT INTO v2_sessions (id, user_id) VALUES (?, ?)",
                    (session_id, user_id),
                )
                await db.commit()
                logger.debug(f"Created new session: {session_id}")

        return session_id

    async def get_history(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[Message]:
        """
        Get all messages for a session.

        Args:
            session_id: Session identifier
            limit: Optional limit on number of messages (most recent)

        Returns:
            List of Message objects in chronological order
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if limit:
                # Get most recent N messages
                cursor = await db.execute(
                    """SELECT * FROM (
                        SELECT * FROM v2_messages
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                    ) ORDER BY id ASC""",
                    (session_id, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM v2_messages WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )

            rows = await cursor.fetchall()
            return [self._row_to_message(row) for row in rows]

    async def add_message(self, session_id: str, message: Message) -> None:
        """
        Add a message to session history.

        Args:
            session_id: Session identifier
            message: Message to add
        """
        await self.initialize()

        tool_calls_json = None
        if message.tool_calls:
            tool_calls_json = json.dumps([asdict(tc) for tc in message.tool_calls])

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO v2_messages
                   (session_id, role, content, tool_calls, tool_call_id, name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    message.role,
                    message.content,
                    tool_calls_json,
                    message.tool_call_id,
                    message.name,
                ),
            )
            await db.execute(
                "UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
            await db.commit()

    async def clear_session(self, session_id: str) -> None:
        """
        Clear all messages from a session.

        Args:
            session_id: Session identifier
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM v2_messages WHERE session_id = ?", (session_id,))
            await db.commit()
            logger.debug(f"Cleared session: {session_id}")

    async def delete_session(self, session_id: str) -> None:
        """
        Delete a session and all its messages.

        Args:
            session_id: Session identifier
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM v2_messages WHERE session_id = ?", (session_id,))
            await db.execute("DELETE FROM v2_sessions WHERE id = ?", (session_id,))
            await db.commit()
            logger.debug(f"Deleted session: {session_id}")

    async def list_sessions(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """
        List all sessions, optionally filtered by user.

        Args:
            user_id: Optional user ID to filter by

        Returns:
            List of session info dicts
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if user_id:
                cursor = await db.execute(
                    "SELECT * FROM v2_sessions WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                )
            else:
                cursor = await db.execute("SELECT * FROM v2_sessions ORDER BY updated_at DESC")

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_message_count(self, session_id: str) -> int:
        """
        Get the number of messages in a session.

        Args:
            session_id: Session identifier

        Returns:
            Number of messages
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM v2_messages WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def session_exists(self, session_id: str) -> bool:
        """
        Check if a session exists.

        Args:
            session_id: Session identifier

        Returns:
            True if session exists, False otherwise
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM v2_sessions WHERE id = ?",
                (session_id,),
            )
            return await cursor.fetchone() is not None

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """
        Get session info by ID.

        Args:
            session_id: Session identifier

        Returns:
            Session info dict or None if not found
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM v2_sessions WHERE id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    def _row_to_message(self, row: aiosqlite.Row) -> Message:
        """Convert database row to Message."""
        tool_calls = []
        if row["tool_calls"]:
            try:
                tool_calls_data = json.loads(row["tool_calls"])
                tool_calls = [ToolCall(**tc) for tc in tool_calls_data]
            except (json.JSONDecodeError, TypeError):
                pass

        return Message(
            role=row["role"],
            content=row["content"],
            tool_calls=tool_calls,
            tool_call_id=row["tool_call_id"],
            name=row["name"],
        )
