"""Migration manager for handling database schema evolution."""

import logging
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

from .base import Migration

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT,
    rollback_sql TEXT
);
"""


class MigrationManager:
    """Manages database schema migrations.

    Provides automatic migration on initialization, version tracking,
    and rollback support for database schema changes.

    Attributes:
        db_path: Path to the SQLite database file
        db_name: Human-readable name for logging (e.g., "sessions", "batch")
        migrations: List of migrations for this database type
    """

    def __init__(
        self,
        db_path: Path,
        db_name: str = "database",
        migrations: list[Migration] | None = None,
        *,
        atomic: bool = False,
    ):
        """Initialize the migration manager.

        Args:
            db_path: Path to the SQLite database file
            db_name: Human-readable name for logging
            migrations: List of migrations for this database type
            atomic: Initialize schema and versions in one transaction. Scripts
                must support execution inside a transaction and must not contain
                transaction-control statements. Legacy script behavior is the default.
        """
        self.db_path = db_path
        self.db_name = db_name
        self.migrations = migrations or []
        self._initialized = False
        self.atomic = atomic

    async def initialize(self) -> None:
        """Initialize the database and run pending migrations."""
        if self._initialized:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            if self.atomic:
                # Reserve the writer before reading versions: another initializer
                # must see our committed schema rather than apply the same upgrade.
                await db.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_schema_migrations_table(db, atomic=self.atomic)
                await self._run_pending_migrations(db, atomic=self.atomic)
                if self.atomic:
                    await db.commit()
            except BaseException:
                if self.atomic:
                    await db.rollback()
                raise

        self._initialized = True
        logger.debug(f"{self.db_name} database initialized with migrations")

    async def _ensure_schema_migrations_table(self, db: aiosqlite.Connection, *, atomic: bool = False) -> None:
        """Create schema_migrations table if it doesn't exist."""
        await self._execute_script(db, SCHEMA_MIGRATIONS_TABLE, atomic=atomic)
        if not atomic:
            await db.commit()

    async def _execute_script(self, db: aiosqlite.Connection, script: str, *, atomic: bool = False) -> None:
        if not atomic:
            await db.executescript(script)
            return
        # executescript() implicitly commits an existing transaction on supported
        # Python versions. SQLite's parser recognizes complete statements, including
        # quoted semicolons, comments and trigger bodies; keep our writer lock intact.
        start = 0
        for position, character in enumerate(script):
            if character == ";" and sqlite3.complete_statement(script[start : position + 1]):
                await db.execute(script[start : position + 1])
                start = position + 1
        if script[start:].strip():
            await db.execute(script[start:])

    async def get_version(self) -> int:
        """Get the current schema version.

        Returns:
            Current version number, or 0 if no migrations applied
        """
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute("SELECT MAX(version) FROM schema_migrations")
                result = await cursor.fetchone()
                return result[0] if result and result[0] is not None else 0
            except Exception:
                return 0

    async def get_applied_migrations(self) -> list[dict[str, Any]]:
        """Get list of applied migrations.

        Returns:
            List of migration info dicts in order applied
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM schema_migrations ORDER BY version")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _run_pending_migrations(self, db: aiosqlite.Connection, *, atomic: bool = False) -> None:
        """Run all pending migrations."""
        current_version = await self._get_version(db)
        pending = [m for m in self.migrations if m.version > current_version]

        if not pending:
            logger.debug(f"{self.db_name} database at latest version {current_version}")
            return

        logger.info(
            f"{self.db_name} database: running {len(pending)} pending migration(s) "
            f"(v{current_version} -> v{pending[-1].version})"
        )

        for migration in pending:
            await self._apply_migration(db, migration, atomic=atomic)

        logger.info(f"{self.db_name} database: migrated to version {pending[-1].version}")

    async def _apply_migration(self, db: aiosqlite.Connection, migration: Migration, *, atomic: bool = False) -> None:
        """Apply a single migration."""
        logger.debug(f"{self.db_name} database: applying migration v{migration.version} - {migration.description}")

        await self._execute_script(db, migration.up_sql, atomic=atomic)

        await db.execute(
            """INSERT INTO schema_migrations (version, description, rollback_sql)
               VALUES (?, ?, ?)""",
            (migration.version, migration.description, migration.down_sql),
        )
        if not atomic:
            await db.commit()

    async def _get_version(self, db: aiosqlite.Connection) -> int:
        """Get current version from database connection."""
        try:
            cursor = await db.execute("SELECT MAX(version) FROM schema_migrations")
            result = await cursor.fetchone()
            return result[0] if result and result[0] is not None else 0
        except Exception:
            return 0

    async def rollback(self, steps: int = 1) -> None:
        """Rollback the last N migrations.

        Args:
            steps: Number of migrations to rollback (default: 1)

        Raises:
            ValueError: If no rollback SQL available or not enough migrations
        """
        async with aiosqlite.connect(self.db_path) as db:
            applied = await self._get_applied_migrations_ordered(db)

            if not applied:
                raise ValueError("No migrations to rollback")

            if steps > len(applied):
                raise ValueError(f"Cannot rollback {steps} migrations, only {len(applied)} applied")

            for _ in range(steps):
                migration_info = applied[-1]
                await self._rollback_migration(db, migration_info)

    async def _get_applied_migrations_ordered(self, db: aiosqlite.Connection) -> list[dict[str, Any]]:
        """Get applied migrations in reverse order (newest first for rollback)."""
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM schema_migrations ORDER BY version DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _rollback_migration(self, db: aiosqlite.Connection, migration_info: dict[str, Any]) -> None:
        """Rollback a single migration."""
        version = migration_info["version"]
        rollback_sql = migration_info["rollback_sql"]

        if not rollback_sql:
            # No rollback SQL stored, try to find migration in our list
            migration = next((m for m in self.migrations if m.version == version), None)
            if migration and migration.down_sql:
                rollback_sql = migration.down_sql
            else:
                raise ValueError(f"Cannot rollback migration v{version}: no rollback SQL available")

        logger.info(f"{self.db_name} database: rolling back migration v{version}")

        await db.executescript(rollback_sql)
        await db.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (version,),
        )
        await db.commit()

        logger.info(f"{self.db_name} database: rolled back to version {version - 1}")

    async def migrate_to(self, target_version: int) -> None:
        """Migrate to a specific target version.

        Args:
            target_version: Target schema version

        Raises:
            ValueError: If target version is invalid
        """
        if target_version < 0:
            raise ValueError("Target version must be >= 0")

        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema_migrations_table(db)
            current_version = await self._get_version(db)

            if target_version > current_version:
                pending = [m for m in self.migrations if current_version < m.version <= target_version]
                for migration in pending:
                    await self._apply_migration(db, migration)
                logger.info(f"{self.db_name} database: migrated to version {target_version}")
            elif target_version < current_version:
                applied = await self._get_applied_migrations_ordered(db)
                to_rollback = [m for m in applied if target_version < m["version"] <= current_version]
                for migration_info in to_rollback:
                    await self._rollback_migration(db, migration_info)
                logger.info(f"{self.db_name} database: rolled back to version {target_version}")
            else:
                logger.debug(f"{self.db_name} database: already at version {target_version}")
