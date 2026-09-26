"""Session database migrations.

This module defines all migrations for the sessions database, from initial
schema through all version updates.
"""

from .base import Migration

# Version 1: Initial schema (v2_sessions and v2_messages tables)
# This represents the original schema that was created on first initialize()
MIGRATION_001_INITIAL = Migration(
    version=1,
    description="Initial schema with v2_sessions and v2_messages tables",
    up_sql="""
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
    """,
    down_sql="""
        DROP INDEX IF EXISTS idx_v2_sessions_user;
        DROP INDEX IF EXISTS idx_v2_messages_session;
        DROP TABLE IF EXISTS v2_messages;
        DROP TABLE IF EXISTS v2_sessions;
    """,
)

# Version 2: Add compacted_at_message_id column for compaction boundary
# This allows tracking where compaction summary was stored
MIGRATION_002_COMPACTION = Migration(
    version=2,
    description="Add compacted_at_message_id column for compaction boundary",
    up_sql="""
        ALTER TABLE v2_sessions ADD COLUMN compacted_at_message_id INTEGER;
    """,
    down_sql="""
        -- SQLite doesn't support DROP COLUMN directly, so we recreate the table
        CREATE TABLE v2_sessions_backup AS
        SELECT id, user_id, created_at, updated_at FROM v2_sessions;

        DROP TABLE v2_sessions;

        CREATE TABLE v2_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO v2_sessions SELECT * FROM v2_sessions_backup;

        DROP TABLE v2_sessions_backup;

        CREATE INDEX IF NOT EXISTS idx_v2_sessions_user ON v2_sessions(user_id);
    """,
)


MIGRATION_003_DELIVERIES = Migration(
    version=3,
    description="Durable local channel deliveries and attachment bytes",
    up_sql="""
        CREATE TABLE ngn_local_deliveries (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL,
            root_session_id TEXT NOT NULL,
            actor_session_id TEXT NOT NULL,
            host_run_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            activation INTEGER NOT NULL CHECK(activation >= 0),
            invocation_id TEXT NOT NULL,
            anchor_message_id INTEGER NOT NULL CHECK(anchor_message_id > 0),
            call_position INTEGER NOT NULL CHECK(call_position >= 0),
            call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX ngn_local_deliveries_root ON ngn_local_deliveries
            (root_session_id, anchor_message_id, call_position, sequence);
        CREATE INDEX ngn_local_deliveries_actor ON ngn_local_deliveries(actor_session_id);
        CREATE TABLE ngn_local_delivery_assets (
            asset_id TEXT PRIMARY KEY,
            delivery_id TEXT NOT NULL REFERENCES ngn_local_deliveries(delivery_id),
            position INTEGER NOT NULL CHECK(position >= 0),
            filename TEXT NOT NULL,
            media_type TEXT NOT NULL,
            byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
            data BLOB NOT NULL CHECK(typeof(data) = 'blob' AND length(data) = byte_length),
            UNIQUE(delivery_id, position)
        );
    """,
    down_sql="""
        DROP TABLE ngn_local_delivery_assets;
        DROP TABLE ngn_local_deliveries;
    """,
)


MIGRATION_004_UPLOADS = Migration(
    version=4,
    description="Session-scoped staged browser uploads and immutable inbox attachment references",
    up_sql="""
        CREATE TABLE ngn_web_upload_scopes (
            session_id TEXT PRIMARY KEY,
            generation TEXT NOT NULL
        );
        CREATE TABLE ngn_web_uploads (
            upload_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            media_type TEXT NOT NULL,
            byte_length INTEGER NOT NULL CHECK(byte_length > 0 AND byte_length <= 8388608),
            data BLOB NOT NULL CHECK(typeof(data) = 'blob' AND length(data) = byte_length),
            expires_at REAL NOT NULL,
            inbox_id INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX ngn_web_uploads_session ON ngn_web_uploads(session_id, inbox_id);
        CREATE INDEX ngn_web_uploads_expiry ON ngn_web_uploads(inbox_id, expires_at);
    """,
    down_sql="DROP TABLE ngn_web_uploads; DROP TABLE ngn_web_upload_scopes;",
)


migrations = [
    MIGRATION_001_INITIAL,
    MIGRATION_002_COMPACTION,
    MIGRATION_003_DELIVERIES,
    MIGRATION_004_UPLOADS,
]

__all__ = [
    "MIGRATION_001_INITIAL",
    "MIGRATION_002_COMPACTION",
    "MIGRATION_003_DELIVERIES",
    "MIGRATION_004_UPLOADS",
    "migrations",
]
