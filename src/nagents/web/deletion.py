"""Root-only deletion at a web idle boundary, with one owned SQLite transaction."""

from __future__ import annotations

import re
from contextlib import closing
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException

from nagents.channels.store import finish_on_cancel

from .routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3

    from .service import WebState


def _delete_rows(db: sqlite3.Connection, session_id: str, selected: str) -> str:
    # Membership is the join used by Harness.list_sessions/resume, not an ID
    # prefix or a raw history row. BEGIN IMMEDIATE also serializes admissions.
    RoutingStore.root(db, session_id)
    with closing(db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")) as cursor:
        tables = {str(row[0]) for row in cursor.fetchall()}

    def exists(sql: str, parameters: tuple[str, ...]) -> bool:
        with closing(db.execute(sql, parameters)) as cursor:
            return cursor.fetchone() is not None

    for table in ("ngn_web_inbox", "nagents_channel_inbox"):
        if table in tables and exists(
            f"SELECT 1 FROM {table} WHERE session_id = ? AND status NOT IN ('completed', 'failed', 'interrupted')",
            (session_id,),
        ):
            raise HTTPException(409, "Session has queued or running inbox work. Let it finish before deleting.")
    if exists(
        "SELECT 1 FROM ngn_web_bindings WHERE session_id = ? OR default_session_id = ?", (session_id, session_id)
    ):
        raise HTTPException(
            409,
            "Session is attached to a channel conversation. In that channel, use /sessions, then /session ID "
            "to attach another root and /session default ID to move its default. Let queued replies finish, then retry.",
        )
    if exists(
        "SELECT 1 FROM ngn_web_message_origins o JOIN ngn_web_inbox i ON i.id = o.inbox_id "
        "JOIN v2_messages m ON m.id = o.history_id WHERE i.session_id = ? AND m.session_id != ?",
        (session_id, session_id),
    ):
        raise HTTPException(409, "Session has inconsistent cross-session history metadata. Deletion was not performed.")

    # Preserve only channel/message identities, never envelopes or conversation
    # content. A late connector redelivery must not re-execute deleted history.
    statements = [
        (
            "INSERT OR IGNORE INTO ngn_web_deleted_messages SELECT channel, message_id FROM ngn_web_inbox "
            "WHERE session_id = ? AND channel != ''"
        ),
        (
            "DELETE FROM ngn_web_message_origins WHERE history_id IN (SELECT id FROM v2_messages WHERE session_id = ?) "
            "OR inbox_id IN (SELECT id FROM ngn_web_inbox WHERE session_id = ?)"
        ),
        "DELETE FROM ngn_web_inbox WHERE session_id = ?",
        "DELETE FROM v2_messages WHERE session_id = ?",
        "DELETE FROM harness_sessions WHERE id = ?",
        "DELETE FROM v2_sessions WHERE id = ?",
    ]
    if "nagents_channel_inbox" in tables:
        # This legacy inbox is root-keyed; its old root can no longer be resumed.
        statements.insert(3, "DELETE FROM nagents_channel_inbox WHERE session_id = ?")
    for sql in statements:
        with closing(db.execute(sql, (session_id,) * sql.count("?"))):
            pass
    with closing(
        db.execute(
            "SELECT h.id FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id "
            "ORDER BY h.title = '', s.updated_at DESC, s.rowid DESC"
        )
    ) as cursor:
        roots = [str(row[0]) for row in cursor.fetchall() if re.fullmatch(r"ngn-[A-Za-z0-9-]{1,76}", str(row[0]))]
    if selected in roots:
        return selected
    return roots[0] if roots else RoutingStore.new_root(db, "")


async def delete_session(state: WebState, session_id: str) -> dict[str, object]:
    with state.idle():
        harness = state.harness
        if harness._busy:
            raise HTTPException(409, "Harness busy. Finish the active operation before deleting.")
        with harness.operation("delete session"):

            async def remove() -> dict[str, object]:
                # Retained handles can resume descendants and carry notification
                # queues. Fail closed rather than partially dismantling the task
                # registry or guessing ownership of raw child rows after restart.
                if any(info.session_id == session_id for info in harness.tasks._infos.values()):
                    raise HTTPException(
                        409,
                        "Session has retained descendant tasks. Finish or cancel them, then restart ngn before "
                        "deleting this root. Retained task handles expire on restart; child history is kept.",
                    )
                if any(not worker.done() for worker in harness.tasks._workers.values()):
                    raise HTTPException(
                        409, "Descendant tasks are still running. Finish or cancel them before deleting."
                    )
                if any(item.session_id == session_id for item in state.wakeups.pending.values()):
                    raise HTTPException(
                        409, "Session has pending wakeups. Let them finish or cancel their run before deleting."
                    )
                if any(item.main_session_id == session_id for item in state.channels.catalog.connections.values()):
                    raise HTTPException(
                        409,
                        "Session is a configured channel main session. Open Channels and save another main session "
                        "first, including for disabled connections. Then reattach any conversations before deleting.",
                    )
                selected = await state.channels.store._transaction(
                    lambda db: _delete_rows(db, session_id, state.selected_session_id)
                )
                # No await between commit acknowledgement and memory invalidation.
                # The enclosing owned task joins this even if HTTP is cancelled.
                state.selected_session_id = selected
                state.session_revision += 1
                if harness.session_id == session_id:
                    harness.session_id = selected
                    harness.tools.read_hashes.clear()
                state.bus.delete_session(session_id)
                state.wakeups.forget_session(session_id)
                result = await state.snapshot(selected)
                state.bus.publish({"type": "sessions", "sessions": result["sessions"]})
                return {**result, "deleted_session_id": session_id}

            with anyio.CancelScope(shield=True):
                result = await finish_on_cancel(remove())
            return result
