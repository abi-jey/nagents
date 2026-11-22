"""Engine admission metadata on the web inbox, not a second execution queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from fastapi import HTTPException

from nagents.engine import archives
from nagents.engine import task_projection

if TYPE_CHECKING:
    import sqlite3

ENGINE_QUEUE_LIMIT = 32
ENGINE_STEERING_LIMIT = 32
_CLOCK = "CAST(strftime('%s', 'now') AS INTEGER) * 1000 + CAST(substr(strftime('%f', 'now'), 4, 3) AS INTEGER)"


@dataclass(frozen=True)
class EngineWork:
    request_id: str
    run_id: str
    interactive: bool


@dataclass(frozen=True)
class EngineAdmission:
    work: EngineWork
    mode: Literal["idle", "queue"]
    lane_busy: bool
    provider_unavailable: bool

    def check(self, db: sqlite3.Connection, session_id: str) -> None:
        if self.provider_unavailable:
            raise HTTPException(409, "Select a named provider connection before sending a message.")
        if self.mode == "idle" and (
            self.lane_busy
            or paused(db, session_id)
            or db.execute(
                "SELECT 1 FROM ngn_web_inbox WHERE session_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (session_id,),
            ).fetchone()
        ):
            raise HTTPException(409, "This session is busy or paused. Queue the input or try again when idle.")
        count = db.execute(
            "SELECT COUNT(*) FROM ngn_web_inbox WHERE session_id = ? AND status = 'queued' AND engine_run_id != ''",
            (session_id,),
        ).fetchone()[0]
        if count >= ENGINE_QUEUE_LIMIT:
            raise HTTPException(429, "Session queue limit reached. Retry with the same request ID.")


@dataclass(frozen=True)
class SteeringWork:
    request_id: str
    run_id: str
    input_id: str


@dataclass(frozen=True)
class SteeringAdmission:
    work: SteeringWork
    accepting: bool

    def check(self, db: sqlite3.Connection, session_id: str) -> None:
        if (
            not self.accepting
            or not db.execute(
                "SELECT 1 FROM ngn_web_inbox WHERE engine_run_id = ? AND session_id = ? AND status = 'running'",
                (self.work.run_id, session_id),
            ).fetchone()
        ):
            raise HTTPException(409, "This run is not accepting steering input.")
        total = db.execute(
            "SELECT COUNT(*) FROM ngn_web_inbox WHERE engine_steer_run = ?", (self.work.run_id,)
        ).fetchone()[0]
        if total >= ENGINE_STEERING_LIMIT:
            raise HTTPException(429, "Run steering limit reached. Retry with the same request ID.")


def eligible(inbox: str) -> str:
    """A pause holds only engine inputs; existing browser/channel behavior is unchanged."""
    return (
        f"({inbox}.engine_run_id = '' OR NOT EXISTS (SELECT 1 FROM ngn_engine_queues q "
        f"WHERE q.session_id = {inbox}.session_id AND q.paused = 1))"
    )


def paused(db: sqlite3.Connection, session_id: str) -> bool:
    row = db.execute("SELECT paused FROM ngn_engine_queues WHERE session_id = ?", (session_id,)).fetchone()
    return bool(row[0]) if row else False


def set_paused(db: sqlite3.Connection, session_id: str, value: bool) -> None:
    db.execute(
        "INSERT INTO ngn_engine_queues(session_id, paused) VALUES (?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET paused = excluded.paused",
        (session_id, value),
    )


def pause_pending(db: sqlite3.Connection) -> None:
    """A restart never authorizes previously accepted, unstarted engine inputs."""
    db.execute(
        "INSERT INTO ngn_engine_queues(session_id, paused) "
        "SELECT DISTINCT session_id, 1 FROM ngn_web_inbox WHERE engine_run_id != '' AND status = 'queued' "
        "ON CONFLICT(session_id) DO UPDATE SET paused = 1"
    )
    db.execute("UPDATE ngn_web_inbox SET status = 'cancelled' WHERE engine_steer_run != '' AND status = 'queued'")


def initialize(db: sqlite3.Connection) -> None:
    fields = {row[1] for row in db.execute("PRAGMA table_info(ngn_web_inbox)")}
    for name, declaration in (
        ("engine_request_id", "TEXT NOT NULL DEFAULT ''"),
        ("engine_run_id", "TEXT NOT NULL DEFAULT ''"),
        ("engine_mode", "TEXT NOT NULL DEFAULT ''"),
        ("engine_interactive", "INTEGER NOT NULL DEFAULT 0"),
        ("engine_cancel", "INTEGER NOT NULL DEFAULT 0"),
        ("engine_steer_id", "TEXT NOT NULL DEFAULT ''"),
        ("engine_steer_run", "TEXT NOT NULL DEFAULT ''"),
        ("engine_steer_state", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in fields:
            db.execute(f"ALTER TABLE ngn_web_inbox ADD COLUMN {name} {declaration}")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ngn_engine_input_run ON ngn_web_inbox(engine_run_id) "
        "WHERE engine_run_id != ''"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS ngn_engine_input_queue ON ngn_web_inbox(session_id, status, id) "
        "WHERE engine_run_id != ''"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS ngn_engine_steering_run ON ngn_web_inbox(engine_steer_run, id) "
        "WHERE engine_steer_run != ''"
    )
    db.execute("CREATE TABLE IF NOT EXISTS ngn_engine_queues (session_id TEXT PRIMARY KEY, paused INTEGER NOT NULL)")
    archives.initialize_in(db)
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_event_queued AFTER INSERT ON ngn_web_inbox "
        "WHEN new.engine_run_id != '' BEGIN "
        "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
        f"VALUES (new.session_id, new.engine_run_id, {_CLOCK}, json_object('type', 'run.queued', 'prompt', new.prompt)); "
        "END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_event_started AFTER UPDATE OF status ON ngn_web_inbox "
        "WHEN new.engine_run_id != '' AND old.status = 'queued' AND new.status = 'running' BEGIN "
        "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
        f"VALUES (new.session_id, new.engine_run_id, {_CLOCK}, json_object('type', 'run.started', 'prompt', new.prompt)); "
        "END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_event_finished AFTER UPDATE OF status ON ngn_web_inbox "
        "WHEN new.engine_run_id != '' AND old.status IN ('queued', 'running') "
        "AND new.status IN ('completed', 'failed', 'cancelled', 'interrupted') BEGIN "
        "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
        f"VALUES (new.session_id, new.engine_run_id, {_CLOCK}, json_object('type', 'run.finished', "
        "'status', new.status, 'error', CASE new.status "
        "WHEN 'failed' THEN 'Run failed. Completed actions were not rolled back.' "
        "WHEN 'cancelled' THEN CASE old.status WHEN 'queued' THEN 'Cancelled before execution.' "
        "ELSE 'Run cancelled. Completed actions were not rolled back.' END "
        "WHEN 'interrupted' THEN 'Run interrupted; it was not replayed.' ELSE '' END)); "
        "INSERT INTO ngn_engine_queues(session_id, paused) SELECT new.session_id, 1 "
        "WHERE old.status = 'running' AND new.status IN ('failed', 'cancelled', 'interrupted') AND EXISTS ("
        "SELECT 1 FROM ngn_web_inbox i WHERE i.session_id = new.session_id AND i.engine_run_id != '' "
        "AND i.status = 'queued') ON CONFLICT(session_id) DO UPDATE SET paused = 1; "
        "END"
    )
    _steering_triggers(db)
    for transition, value_changed in (
        ("INSERT", "new.paused = 1"),
        ("UPDATE", "old.paused != new.paused"),
    ):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS ngn_engine_queue_{transition.lower()} AFTER {transition} "
            f"ON ngn_engine_queues WHEN {value_changed} BEGIN "
            "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
            f"VALUES (new.session_id, '', {_CLOCK}, json_object('type', 'queue.updated', "
            "'paused', json(CASE new.paused WHEN 1 THEN 'true' ELSE 'false' END))); "
            "END"
        )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_purge AFTER DELETE ON v2_sessions BEGIN "
        "DELETE FROM ngn_engine_events WHERE session_id = old.id; "
        "DELETE FROM ngn_engine_queues WHERE session_id = old.id; "
        "END"
    )
    task_projection.initialize_in(db)


def _steering_triggers(db: sqlite3.Connection) -> None:
    # Scope withdrawal to the exact terminating activation. A pending steer
    # is never ordinary queued work, even if that session starts another run.
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_steering_retire AFTER UPDATE OF status ON ngn_web_inbox "
        "WHEN new.engine_run_id != '' AND old.status = 'running' "
        "AND new.status IN ('completed', 'failed', 'cancelled', 'interrupted') BEGIN "
        "UPDATE ngn_web_inbox SET status = 'cancelled' WHERE engine_steer_run = new.engine_run_id AND status = 'queued'; "
        "END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_steering_transition AFTER UPDATE OF status ON ngn_web_inbox "
        "WHEN new.engine_steer_id != '' AND old.status != new.status BEGIN "
        "UPDATE ngn_web_inbox SET engine_steer_state = CASE WHEN new.status = 'running' THEN 'offered' "
        "WHEN engine_steer_state = 'pending' THEN 'undelivered' ELSE 'uncertain' END "
        "WHERE id = new.id AND engine_steer_state IN ('pending','offered'); "
        "END"
    )
    for transition, changed in (
        ("INSERT", ""),
        ("UPDATE OF engine_steer_state", " AND old.engine_steer_state != new.engine_steer_state"),
    ):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS ngn_engine_steering_{transition.split()[0].lower()} AFTER {transition} "
            f"ON ngn_web_inbox WHEN new.engine_steer_id != ''{changed} BEGIN "
            "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
            f"VALUES (new.session_id, new.engine_steer_run, {_CLOCK}, "
            "json_object('type', 'steering.updated', 'input_id', new.engine_steer_id)); END"
        )
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ngn_web_message_origins'").fetchone():
        # WebHistory attaches this origin in the very transaction that
        # persists the accepted user input, before the next model request.
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS ngn_engine_steering_applied AFTER INSERT ON ngn_web_message_origins BEGIN "
            "UPDATE ngn_web_inbox SET engine_steer_state = 'applied' WHERE id = new.inbox_id "
            "AND engine_steer_id != '' AND status = 'running' AND engine_steer_state = 'offered' AND EXISTS ("
            "SELECT 1 FROM v2_messages m WHERE m.id = new.history_id AND m.session_id = ngn_web_inbox.session_id "
            "AND m.role = 'user'); END"
        )
