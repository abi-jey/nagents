"""Atomic, independent root copies; historical data never carries live authority."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from uuid import uuid4

from nagents._async import join_owned
from nagents.channels.delivery_types import DeliveryOrigin
from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.channels.types import ChannelError

from .deliveries import _anchor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class SessionForkError(ValueError):
    """A source root cannot be copied safely at this boundary."""


def title_text(title: str, *, optional: bool = False) -> str:
    title = title.strip()
    if not title and optional:
        return ""
    if not title or len(title) > 80 or not title.isprintable():
        raise ValueError("A session title must contain 1 to 80 printable characters.")
    return title


def root_title(db: sqlite3.Connection, source: str) -> str:
    row = db.execute(
        "SELECT h.title FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id WHERE h.id = ?", (source,)
    ).fetchone()
    if row is None:
        raise SessionForkError("Session not found in this workspace.")
    return str(row[0])


def rename_in(db: sqlite3.Connection, source: str, title: str) -> None:
    title = title_text(title)
    root_title(db, source)
    db.execute("UPDATE harness_sessions SET title = ? WHERE id = ?", (title, source))
    db.execute("UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (source,))


def _web_annotations(db: sqlite3.Connection, source: str, target: str, ids: dict[int, int], tables: set[str]) -> None:
    calls: dict[str, str] = {}
    if "ngn_web_live_calls" in tables:
        for (old,) in db.execute("SELECT voice_session_id FROM ngn_web_live_calls WHERE session_id = ?", (source,)):
            calls[str(old)] = f"fork-{uuid4().hex}"
            db.execute(
                "INSERT INTO ngn_web_live_calls (voice_session_id, session_id, accepting) VALUES (?, ?, 0)",
                (calls[str(old)], target),
            )
        for old_call, new_call in calls.items():
            for sequence, speaker, text, start, end, anchor in db.execute(
                "SELECT caption_seq, speaker, text, start_ms, end_ms, anchor_history_id "
                "FROM ngn_web_live_captions WHERE voice_session_id = ? ORDER BY id",
                (old_call,),
            ):
                if anchor and int(anchor) not in ids:
                    raise SessionForkError("Source caption has no matching conversation message.")
                db.execute(
                    "INSERT INTO ngn_web_live_captions "
                    "(voice_session_id, caption_seq, speaker, text, start_ms, end_ms, anchor_history_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_call, sequence, speaker, text, start, end, ids.get(int(anchor), 0)),
                )
    for old, new in ids.items():
        db.execute(
            "INSERT INTO ngn_fork_message_display SELECT ?, content FROM ngn_fork_message_display WHERE history_id = ?",
            (new, old),
        )
        for upload, position in db.execute(
            "SELECT upload_id, position FROM ngn_fork_message_uploads WHERE history_id = ?", (old,)
        ):
            db.execute(
                "INSERT INTO ngn_fork_message_uploads "
                "SELECT ?, ?, ?, filename, media_type, byte_length, data FROM ngn_fork_message_uploads WHERE upload_id = ?",
                (str(uuid4()), new, position, upload),
            )
        if "ngn_web_voice_messages" in tables:
            db.execute(
                "INSERT INTO ngn_web_voice_messages SELECT ?, transcript FROM ngn_web_voice_messages WHERE history_id = ?",
                (new, old),
            )
        if "ngn_web_voice_origins" in tables:
            voice = db.execute(
                "SELECT voice_session_id FROM ngn_web_voice_origins WHERE history_id = ?", (old,)
            ).fetchone()
            if voice and voice[0] in calls:
                db.execute("INSERT INTO ngn_web_voice_origins VALUES (?, ?)", (new, calls[voice[0]]))
        if "ngn_web_message_origins" not in tables:
            continue
        origin = db.execute(
            "SELECT i.id, i.session_id, i.channel, i.prompt, i.attachments FROM ngn_web_message_origins o "
            "JOIN ngn_web_inbox i ON i.id = o.inbox_id WHERE o.history_id = ?",
            (old,),
        ).fetchone()
        if origin is None:
            continue
        inbox, owner, channel, prompt, attachments = origin
        if owner != source:
            raise SessionForkError("Source history contains inconsistent message ownership.")
        if not channel and attachments != "[]":
            db.execute("INSERT OR REPLACE INTO ngn_fork_message_display VALUES (?, ?)", (new, prompt))
            for upload, position in db.execute(
                "SELECT upload_id, position FROM ngn_web_uploads WHERE inbox_id = ? AND session_id = ? ORDER BY position",
                (inbox, source),
            ):
                db.execute(
                    "INSERT INTO ngn_fork_message_uploads "
                    "SELECT ?, ?, ?, filename, media_type, byte_length, data FROM ngn_web_uploads WHERE upload_id = ?",
                    (str(uuid4()), new, position, upload),
                )
        elif channel:
            # Keep the caller's visible text without manufacturing an ingress
            # receipt or copying the external chat's automatic-reply authority.
            try:
                envelope = json.loads(str(prompt).removeprefix(_INBOUND_PREFIX))
            except (ValueError, RecursionError):
                continue
            if isinstance(envelope, dict) and isinstance(envelope.get("text"), str):
                db.execute("INSERT OR REPLACE INTO ngn_fork_message_display VALUES (?, ?)", (new, envelope["text"]))


def _deliveries(db: sqlite3.Connection, source: str, target: str, ids: dict[int, int]) -> None:
    for row in db.execute(
        "SELECT delivery_id, root_session_id, actor_session_id, host_run_id, turn_id, task_id, activation, "
        "invocation_id, anchor_message_id, call_position, call_id, tool_name "
        "FROM ngn_local_deliveries WHERE root_session_id = ? ORDER BY sequence",
        (source,),
    ):
        delivery, anchor = str(row[0]), int(row[8])
        try:
            _anchor(db, DeliveryOrigin(*row[1:]))
        except ChannelError:
            raise SessionForkError("Source delivery has an inconsistent conversation anchor.") from None
        if int(anchor) not in ids:
            raise SessionForkError("Source delivery has no matching conversation message.")
        new = uuid4().hex
        db.execute(
            "INSERT INTO ngn_local_deliveries "
            "(delivery_id, channel, root_session_id, actor_session_id, host_run_id, turn_id, task_id, activation, "
            "invocation_id, anchor_message_id, call_position, call_id, tool_name, text, created_at) "
            "SELECT ?, channel, ?, ?, '', '', '', 0, ?, ?, call_position, call_id, tool_name, text, created_at "
            "FROM ngn_local_deliveries WHERE delivery_id = ?",
            (new, target, target, f"fork-{uuid4().hex}", ids[int(anchor)], delivery),
        )
        for (asset,) in db.execute("SELECT asset_id FROM ngn_local_delivery_assets WHERE delivery_id = ?", (delivery,)):
            db.execute(
                "INSERT INTO ngn_local_delivery_assets "
                "SELECT ?, ?, position, filename, media_type, byte_length, data FROM ngn_local_delivery_assets WHERE asset_id = ?",
                (uuid4().hex, new, asset),
            )


def fork_in(db: sqlite3.Connection, source: str, title: str = "") -> str:
    """Use the caller's BEGIN IMMEDIATE transaction; never commit partially."""
    previous = root_title(db, source)
    title = title_text(title, optional=True)
    tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if (
        "ngn_web_inbox" in tables
        and db.execute(
            "SELECT 1 FROM ngn_web_inbox WHERE session_id = ? AND status IN ('queued', 'running') LIMIT 1", (source,)
        ).fetchone()
    ):
        raise SessionForkError("Finish or cancel queued work in this chat before forking it.")
    # Allocate in the same transaction as the copy: concurrent forks serialize,
    # failed copies consume no number, and renamed/deleted children cannot reset it.
    db.execute(
        "INSERT INTO ngn_session_fork_counters VALUES (?, 1) "
        "ON CONFLICT(session_id) DO UPDATE SET last_number = last_number + 1",
        (source,),
    )
    number = db.execute(
        "SELECT last_number FROM ngn_session_fork_counters WHERE session_id = ?",
        (source,),
    ).fetchone()[0]
    if not title:
        suffix = f" · fork {number}"
        title = (previous or "New session")[: 80 - len(suffix)].rstrip() + suffix
    boundary = db.execute("SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?", (source,)).fetchone()[0]
    target = f"ngn-{uuid4().hex[:16]}"
    db.execute("INSERT INTO v2_sessions (id, user_id) VALUES (?, 'harness')", (target,))
    db.execute("INSERT INTO harness_sessions (id, title) VALUES (?, ?)", (target, title))
    db.execute("INSERT INTO ngn_session_forks VALUES (?, ?)", (target, source))
    if "ngn_web_group_members" in tables:
        copied_group = db.execute(
            "INSERT INTO ngn_web_group_members SELECT ?, group_id FROM ngn_web_group_members WHERE session_id = ?",
            (target, source),
        ).rowcount
        if copied_group and "ngn_web_group_revision" in tables:
            db.execute("UPDATE ngn_web_group_revision SET revision = ? WHERE id = 1", (uuid4().hex,))
    ids: dict[int, int] = {}
    for (old,) in db.execute("SELECT id FROM v2_messages WHERE session_id = ? ORDER BY id", (source,)):
        inserted = db.execute(
            "INSERT INTO v2_messages (session_id, role, content, tool_calls, tool_call_id, name, created_at) "
            "SELECT ?, role, content, tool_calls, tool_call_id, name, created_at FROM v2_messages WHERE id = ?",
            (target, old),
        ).lastrowid
        assert inserted is not None
        ids[int(old)] = inserted
    if boundary is not None:
        if int(boundary) not in ids:
            raise SessionForkError("Source context boundary has no matching conversation message.")
        db.execute("UPDATE v2_sessions SET compacted_at_message_id = ? WHERE id = ?", (ids[int(boundary)], target))
    _web_annotations(db, source, target, ids, tables)
    _deliveries(db, source, target, ids)
    return target


async def transaction(path: Path, change: Callable[[sqlite3.Connection], str]) -> str:
    def apply() -> str:
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return change(db)

    return await join_owned(asyncio.create_task(asyncio.to_thread(apply)))
