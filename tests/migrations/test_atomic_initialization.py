"""Atomic session upgrades preserve data and serialize independent initializers."""

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from nagents.migrations.base import Migration
from nagents.migrations.manager import MigrationManager
from nagents.migrations.sessions import migrations
from nagents.session.manager import SessionManager


def database_snapshot(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with sqlite3.connect(path) as db:
        schema = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
        versions = db.execute(
            "SELECT version, description, rollback_sql FROM schema_migrations ORDER BY version"
        ).fetchall()
    return schema, versions


@pytest.mark.asyncio
async def test_atomic_sessions_match_legacy_schema_and_metadata(tmp_path: Path) -> None:
    legacy, atomic = tmp_path / "legacy.db", tmp_path / "atomic.db"
    await MigrationManager(legacy, migrations=migrations).initialize()
    await SessionManager(atomic).initialize()
    assert database_snapshot(atomic) == database_snapshot(legacy)
    with sqlite3.connect(atomic) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_atomic_upgrade_preserves_existing_messages(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    await MigrationManager(path, migrations=migrations[:1]).initialize()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO v2_sessions (id,user_id) VALUES ('session','user')")
        db.execute("INSERT INTO v2_messages (session_id,role,content) VALUES ('session','user','keep me')")
    await SessionManager(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT content FROM v2_messages").fetchall() == [("keep me",)]
        assert db.execute("SELECT compacted_at_message_id FROM v2_sessions").fetchone() == (None,)
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]


@pytest.mark.asyncio
async def test_failed_atomic_upgrade_leaves_prior_schema_data_and_versions(tmp_path: Path) -> None:
    path = tmp_path / "failure.db"
    initial = Migration(1, "initial", "CREATE TABLE original (value TEXT); INSERT INTO original VALUES ('keep');")
    await MigrationManager(path, migrations=[initial]).initialize()
    snapshot = database_snapshot(path)
    upgrade = Migration(2, "upgrade", "ALTER TABLE original ADD COLUMN more TEXT; UPDATE original SET value='changed';")
    broken = Migration(3, "broken", "CREATE TABLE partial (value TEXT); SELECT * FROM missing_table;")
    manager = MigrationManager(path, migrations=[initial, upgrade, broken], atomic=True)
    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        await manager.initialize()
    assert not manager._initialized
    assert database_snapshot(path) == snapshot
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM original").fetchall() == [("keep",)]
    manager.migrations = [initial, upgrade]
    await manager.initialize()
    assert await manager.get_version() == 2


@pytest.mark.asyncio
async def test_independent_session_initializers_serialize_fresh_and_upgrade(tmp_path: Path) -> None:
    for version in (0, 1, 3):
        path = tmp_path / f"concurrent-{version}.db"
        if version:
            await MigrationManager(path, migrations=migrations[:version]).initialize()
        await asyncio.gather(*(SessionManager(path).initialize() for _ in range(8)))
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
                (1,),
                (2,),
                (3,),
                (4,),
            ]
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_atomic_script_preserves_trigger_bodies_and_quoted_semicolons(tmp_path: Path) -> None:
    path = tmp_path / "trigger.db"
    script = """
        -- a comment with a ; is not a statement boundary
        CREATE TABLE source (value TEXT);
        CREATE TABLE audit (value TEXT);
        CREATE TRIGGER observed AFTER INSERT ON source BEGIN
            INSERT INTO audit VALUES ('literal;semicolon');
            INSERT INTO audit VALUES (NEW.value);
        END;
        /* another ; comment */ INSERT INTO source VALUES ('input;value');
        INSERT INTO source VALUES ('no final terminator')
    """
    await MigrationManager(path, migrations=[Migration(1, "trigger", script)], atomic=True).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM audit").fetchall() == [
            ("literal;semicolon",),
            ("input;value",),
            ("literal;semicolon",),
            ("no final terminator",),
        ]


@pytest.mark.asyncio
async def test_default_custom_scripts_retain_transaction_control(tmp_path: Path) -> None:
    path = tmp_path / "custom.db"
    script = "BEGIN; CREATE TABLE custom (value TEXT); COMMIT; VACUUM;"
    await MigrationManager(path, migrations=[Migration(1, "custom", script)]).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM custom").fetchall() == []


@pytest.mark.asyncio
async def test_cancelled_atomic_initialization_rolls_back_and_releases_writer(tmp_path: Path) -> None:
    path = tmp_path / "cancelled.db"
    entered = asyncio.Event()

    class PausedManager(MigrationManager):
        async def _apply_migration(
            self, db: aiosqlite.Connection, migration: Migration, *, atomic: bool = False
        ) -> None:
            await super()._apply_migration(db, migration, atomic=atomic)
            entered.set()
            await asyncio.Event().wait()

    manager = PausedManager(path, migrations=migrations, atomic=True)
    task = asyncio.create_task(manager.initialize())
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager._initialized
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    await asyncio.wait_for(SessionManager(path).initialize(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [False, True])
async def test_targeted_migration_persists_versions_independently_of_atomic_initialize(
    tmp_path: Path, atomic: bool
) -> None:
    path = tmp_path / "targeted.db"
    manager = MigrationManager(path, migrations=migrations, atomic=atomic)
    for version in (1, 4, 1, 4):
        await manager.migrate_to(version)
        assert await manager.get_version() == version
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
                (number,) for number in range(1, version + 1)
            ]
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert ("ngn_web_uploads" in names) == (version == 4)
            assert "v2_sessions" in names
