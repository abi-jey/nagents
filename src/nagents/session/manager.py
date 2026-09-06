"""
SQLite-based session and message history management.
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiosqlite

from ..extensions import _validate_context
from ..migrations.manager import MigrationManager
from ..migrations.sessions import migrations as session_migrations
from ..types import AudioContent
from ..types import ContentPart
from ..types import DocumentContent
from ..types import ImageContent
from ..types import Message
from ..types import TextContent
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
        self._migration_manager: MigrationManager | None = None

    async def initialize(self) -> None:
        """Initialize database and run migrations."""
        if self._initialized:
            return

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create migration manager with session-specific migrations
        self._migration_manager = MigrationManager(
            self.db_path, db_name="sessions", migrations=list(session_migrations)
        )
        await self._migration_manager.initialize()

        self._initialized = True
        logger.debug(f"Session database initialized at {self.db_path}")

    async def get_schema_version(self) -> int:
        """Get current database schema version.

        Returns:
            Current version number, or 0 if no migrations applied
        """
        await self.initialize()
        if self._migration_manager is None:
            return 0
        return await self._migration_manager.get_version()

    async def get_migration_history(self) -> list[dict[str, Any]]:
        """Get history of applied migrations.

        Returns:
            List of migration info dicts
        """
        await self.initialize()
        if self._migration_manager is None:
            return []
        return await self._migration_manager.get_applied_migrations()

    async def rollback_migration(self, steps: int = 1) -> None:
        """Rollback the last N migrations.

        Args:
            steps: Number of migrations to rollback (default: 1)

        Raises:
            ValueError: If not enough migrations to rollback
        """
        await self.initialize()
        if self._migration_manager is None:
            return
        await self._migration_manager.rollback(steps)

    async def migrate_to_version(self, version: int) -> None:
        """Migrate database to a specific version.

        Args:
            version: Target schema version

        Raises:
            ValueError: If target version is invalid
        """
        await self.initialize()
        if self._migration_manager is None:
            return
        await self._migration_manager.migrate_to(version)

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

            # Check for compaction boundary
            boundary_cursor = await db.execute(
                "SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?",
                (session_id,),
            )
            boundary_row = await boundary_cursor.fetchone()
            compaction_boundary: int | None = None
            if boundary_row and boundary_row["compacted_at_message_id"] is not None:
                compaction_boundary = boundary_row["compacted_at_message_id"]

            if limit:
                # Get most recent N messages (respecting compaction boundary)
                if compaction_boundary is not None:
                    cursor = await db.execute(
                        """SELECT * FROM (
                            SELECT * FROM v2_messages
                            WHERE session_id = ? AND id >= ?
                            ORDER BY id DESC
                            LIMIT ?
                        ) ORDER BY id ASC""",
                        (session_id, compaction_boundary, limit),
                    )
                else:
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
                if compaction_boundary is not None:
                    cursor = await db.execute(
                        "SELECT * FROM v2_messages WHERE session_id = ? AND id >= ? ORDER BY id",
                        (session_id, compaction_boundary),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT * FROM v2_messages WHERE session_id = ? ORDER BY id",
                        (session_id,),
                    )

            rows = await cursor.fetchall()
            return [self._row_to_message(row) for row in rows]

    async def add_message(self, session_id: str, message: Message) -> int:
        """
        Add a message to session history.

        Args:
            session_id: Session identifier
            message: Message to add

        Returns:
            The database row ID of the inserted message
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO v2_messages
                   (session_id, role, content, tool_calls, tool_call_id, name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                self._message_values(session_id, message),
            )
            message_id = cursor.lastrowid
            assert message_id is not None
            await db.execute(
                "UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
            await db.commit()

        return message_id

    @staticmethod
    def _message_values(session_id: str, message: Message) -> tuple[str | None, ...]:
        """Shared serialization for ordinary messages and context replacements."""
        return (
            session_id,
            message.role,
            json.dumps([asdict(part) for part in message.content])
            if isinstance(message.content, list)
            else message.content,
            json.dumps([asdict(call) for call in message.tool_calls]) if message.tool_calls else None,
            message.tool_call_id,
            message.name,
        )

    async def replace_context(self, session_id: str, messages: list[Message]) -> None:
        """Append a validated replacement and move the inclusive boundary atomically.

        Raw history is never deleted. Validation and serialization complete
        before any writes; failed inserts/boundary updates roll back together.
        """
        _validate_context(messages)
        values = [self._message_values(session_id, message) for message in messages]
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT 1 FROM v2_sessions WHERE id = ?", (session_id,))
            if await cursor.fetchone() is None:
                raise ValueError(f"Session '{session_id}' not found")
            first_id: int | None = None
            for row in values:
                cursor = await db.execute(
                    """INSERT INTO v2_messages
                       (session_id, role, content, tool_calls, tool_call_id, name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    row,
                )
                if first_id is None:
                    first_id = cursor.lastrowid
            assert first_id is not None
            await db.execute(
                """UPDATE v2_sessions SET compacted_at_message_id = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (first_id, session_id),
            )
            await db.commit()

    async def set_compaction_boundary(self, session_id: str, message_id: int) -> None:
        """
        Record a compaction boundary for a session.

        After compaction, get_history() will only return messages
        starting from this message_id (inclusive), filtering out
        all older pre-compaction messages.

        Args:
            session_id: Session identifier
            message_id: The message ID of the compaction summary
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE v2_sessions SET compacted_at_message_id = ? WHERE id = ?",
                (message_id, session_id),
            )
            await db.commit()
            logger.debug(f"Compaction boundary set: session={session_id}, message_id={message_id}")

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

        # Deserialize content: may be plain str, None, or JSON-encoded list[ContentPart]
        content: str | list[ContentPart] | None = row["content"]
        if isinstance(content, str) and content.startswith("["):
            try:
                parts_data = json.loads(content)
                if isinstance(parts_data, list) and parts_data:
                    content = [self._dict_to_content_part(p) for p in parts_data]
            except (json.JSONDecodeError, TypeError, KeyError):
                pass  # Not valid JSON array, keep as plain string

        return Message(
            role=row["role"],
            content=content,
            tool_calls=tool_calls,
            tool_call_id=row["tool_call_id"],
            name=row["name"],
        )

    @staticmethod
    def _dict_to_content_part(data: dict[str, Any]) -> ContentPart:
        """Reconstruct a ContentPart dataclass from a serialized dict."""
        part_type = data.get("type", "text")
        if part_type == "text":
            return TextContent(text=data.get("text", ""))
        elif part_type == "image":
            return ImageContent(
                base64_data=data.get("base64_data", ""),
                media_type=data.get("media_type", "image/jpeg"),
                detail=data.get("detail"),
            )
        elif part_type == "audio":
            return AudioContent(
                base64_data=data.get("base64_data", ""),
                format=data.get("format", "wav"),
            )
        elif part_type == "document":
            return DocumentContent(
                base64_data=data.get("base64_data", ""),
                media_type=data.get("media_type", "application/pdf"),
                title=data.get("title"),
            )
        else:
            # Fallback: treat unknown types as text
            return TextContent(text=str(data))


class PlaceholderSessionManager(SessionManager):
    """Placeholder session manager that gets replaced before run()."""

    def __init__(self) -> None:
        super().__init__(db_path=Path(":memory:"))
        self._is_placeholder = True

    def __repr__(self) -> str:
        return "<PlaceholderSessionManager>"

    def _raise_placeholder_error(self) -> None:
        raise RuntimeError(
            "PlaceholderSessionManager has not been replaced. "
            "The main agent must inject its session_manager before calling run()."
        )

    async def initialize(self) -> None:
        self._raise_placeholder_error()

    async def get_or_create_session(self, session_id: str, user_id: str = "default") -> str:
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    async def session_exists(self, session_id: str) -> bool:
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    async def add_message(self, session_id: str, message: Message) -> int:
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    async def get_history(self, session_id: str, limit: int | None = None) -> list[Message]:
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    async def set_compaction_boundary(self, session_id: str, message_id: int) -> None:
        self._raise_placeholder_error()

    async def close(self) -> None:
        pass


# NoReturn annotation for methods that always raise
# mypy doesn't understand that _raise_placeholder_error() never returns


# Placeholder instance for DEFAULT_COMPACTOR
PLACEHOLDER_SESSION_MANAGER = PlaceholderSessionManager()
