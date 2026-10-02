"""Durable speech observations for the web chat, separate from model history."""

from __future__ import annotations

import re
import sqlite3

from fastapi import HTTPException

from .routing import RoutingStore

MAX_CAPTION_CHARACTERS = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
Payload = dict[str, object]


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_web_live_calls ("
        "voice_session_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES v2_sessions(id), "
        "accepting INTEGER NOT NULL DEFAULT 1)"
    )
    if "accepting" not in {row[1] for row in db.execute("PRAGMA table_info(ngn_web_live_calls)")}:
        db.execute("ALTER TABLE ngn_web_live_calls ADD COLUMN accepting INTEGER NOT NULL DEFAULT 1")
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_web_live_captions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, voice_session_id TEXT NOT NULL REFERENCES ngn_web_live_calls(voice_session_id), "
        "caption_seq INTEGER NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL, "
        "start_ms REAL NOT NULL, end_ms REAL NOT NULL, anchor_history_id INTEGER NOT NULL, "
        "UNIQUE(voice_session_id, caption_seq))"
    )
    db.execute("CREATE INDEX IF NOT EXISTS ngn_web_live_calls_root ON ngn_web_live_calls(session_id)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_web_voice_origins ("
        "history_id INTEGER PRIMARY KEY REFERENCES v2_messages(id), voice_session_id TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_web_voice_origin_delete BEFORE DELETE ON v2_messages BEGIN "
        "DELETE FROM ngn_web_voice_origins WHERE history_id = OLD.id; END"
    )
    # Clear also works for a voice-only chat with no v2_messages to delete. The
    # core clear operation resets this boundary in the same SQLite transaction.
    db.execute("DROP TRIGGER IF EXISTS ngn_web_live_caption_clear")
    for name, event, identifier in (
        (
            "clear",
            "AFTER UPDATE OF compacted_at_message_id ON v2_sessions WHEN NEW.compacted_at_message_id IS NULL",
            "NEW.id",
        ),
        ("delete", "BEFORE DELETE ON v2_sessions", "OLD.id"),
    ):
        invalidate = (
            f"UPDATE ngn_web_live_calls SET accepting = 0 WHERE session_id = {identifier};"
            if name == "clear"
            else f"DELETE FROM ngn_web_live_calls WHERE session_id = {identifier};"
        )
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS ngn_web_live_caption_{name} {event} BEGIN "
            "DELETE FROM ngn_web_live_captions WHERE voice_session_id IN "
            f"(SELECT voice_session_id FROM ngn_web_live_calls WHERE session_id = {identifier}); "
            f"{invalidate} END"
        )


def register(db: sqlite3.Connection, session_id: str, voice_session_id: str) -> bool:
    """Reserve call provenance before any media; clearing never reopens an old ID."""
    if not _IDENTIFIER.fullmatch(voice_session_id):
        raise ValueError("Invalid voice session identity")
    RoutingStore.root(db, session_id)
    existing = db.execute(
        "SELECT session_id, accepting FROM ngn_web_live_calls WHERE voice_session_id = ?", (voice_session_id,)
    ).fetchone()
    if existing is not None:
        if existing[0] != session_id:
            raise ValueError("Voice captions cannot change their admitted chat")
        return bool(existing[1])
    db.execute(
        "INSERT INTO ngn_web_live_calls (voice_session_id, session_id) VALUES (?, ?)", (voice_session_id, session_id)
    )
    return True


def _project(row: sqlite3.Row) -> Payload:
    return {
        "role": "live_caption",
        "source": "live_caption",
        "history_id": f"live-caption:{row['id']}",
        "voice_session_id": row["voice_session_id"],
        "caption_seq": row["caption_seq"],
        "speaker": row["speaker"],
        "content": row["text"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "anchor_history_id": str(row["anchor_history_id"]) if row["anchor_history_id"] else "",
        "name": "",
        "tool_call_id": "",
        "tool_calls": [],
        "parts": [],
    }


def append(db: sqlite3.Connection, session_id: str, voice_session_id: str, event: Payload) -> Payload:
    """Insert once under the admitted root; return only a newly committed caption."""
    if not _IDENTIFIER.fullmatch(voice_session_id):
        raise ValueError("Invalid voice session identity")
    speaker, text, sequence = event.get("speaker"), event.get("text"), event.get("seq")
    start, end = event.get("start_ms"), event.get("end_ms")
    if (
        event.get("type") != "transcript"
        or not isinstance(speaker, str)
        or speaker not in {"user", "assistant"}
        or not isinstance(text, str)
        or type(sequence) is not int
        or not 0 < sequence < 2**53
    ):
        raise ValueError("Invalid voice caption")
    if (
        not isinstance(start, int | float)
        or isinstance(start, bool)
        or not isinstance(end, int | float)
        or isinstance(end, bool)
        or not 0 <= start <= end <= 1e12
    ):
        return {}  # Diagnostic-only fragments must never acquire invented timing.
    text = _CONTROLS.sub("", text[:MAX_CAPTION_CHARACTERS])
    if not text:
        return {}
    try:
        if not register(db, session_id, voice_session_id):
            return {}
    except HTTPException:
        return {}  # A deleted/trashed root cannot receive late voice observations.
    anchor = int(
        db.execute("SELECT COALESCE(MAX(id), 0) FROM v2_messages WHERE session_id = ?", (session_id,)).fetchone()[0]
    )
    cursor = db.execute(
        "INSERT OR IGNORE INTO ngn_web_live_captions "
        "(voice_session_id, caption_seq, speaker, text, start_ms, end_ms, anchor_history_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (voice_session_id, sequence, speaker, text, start, end, anchor),
    )
    if not cursor.rowcount:
        return {}
    db.execute("UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM ngn_web_live_captions WHERE id = ?", (cursor.lastrowid,)).fetchone()
    assert row is not None
    return _project(row)


def merge(db: sqlite3.Connection, session_id: str, records: list[Payload]) -> list[Payload]:
    """Place captions after their observed model-history anchor without feeding models."""
    rows = db.execute(
        "SELECT c.* FROM ngn_web_live_captions c JOIN ngn_web_live_calls l USING (voice_session_id) "
        "WHERE l.session_id = ? AND c.anchor_history_id >= COALESCE("
        "(SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?), 0) "
        "ORDER BY c.anchor_history_id, c.id",
        (session_id, session_id),
    ).fetchall()
    result: list[Payload] = []
    position = 0
    for record in records:
        history_id = int(str(record["history_id"]))
        while position < len(rows) and rows[position]["anchor_history_id"] < history_id:
            result.append(_project(rows[position]))
            position += 1
        result.append(record)
    result.extend(_project(row) for row in rows[position:])
    return result
