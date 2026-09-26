"""Root-only deletion at a web idle boundary, with one owned SQLite transaction."""

from __future__ import annotations

import logging
import re
from contextlib import closing
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException

from nagents.session.deliveries import cleanup_deliveries

from ._async import finish_on_cancel
from .routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3

    from .service import WebState

logger = logging.getLogger(__name__)


def _guard_rows(db: sqlite3.Connection, session_id: str) -> set[str]:
    """Reject only genuine cross-session corruption before removing content.

    Channel attachments and pending inbox work are no longer refusals: the UI
    wins, and the deletion paths release bindings and quarantine pending work
    instead of asking the user to reattach or wait.
    """
    with closing(db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")) as cursor:
        tables = {str(row[0]) for row in cursor.fetchall()}

    with closing(
        db.execute(
            "SELECT 1 FROM ngn_web_message_origins o JOIN ngn_web_inbox i ON i.id = o.inbox_id "
            "JOIN v2_messages m ON m.id = o.history_id WHERE i.session_id = ? AND m.session_id != ?",
            (session_id, session_id),
        )
    ) as cursor:
        if cursor.fetchone() is not None:
            raise HTTPException(
                409, "Session has inconsistent cross-session history metadata. Deletion was not performed."
            )
    return tables


def _release_bindings(db: sqlite3.Connection, session_id: str) -> None:
    """Re-root channel chats that referenced the deleted root.

    Removing the conversation binding lets ``RoutingStore.receive`` create a
    fresh chat root on its next inbound through the existing ``row is None``
    branch, with no recovery notice and no lost message. When only the chat's
    default was deleted, keep it on its surviving root. Ownership rows are
    deliberately retained; deleting an ID never transfers historical ownership.
    """
    with closing(db.execute("DELETE FROM ngn_web_bindings WHERE session_id = ?", (session_id,))):
        pass
    with closing(
        db.execute(
            "UPDATE ngn_web_bindings SET default_session_id = session_id WHERE default_session_id = ?",
            (session_id,),
        )
    ):
        pass


def _quarantine_pending(db: sqlite3.Connection, session_id: str) -> None:
    """Make a soft-deleted root's pending work terminal and dedup-safe.

    Rows stay for restore/history joins, but a late connector redelivery of the
    same ``(channel, message_id)`` must not re-execute deleted work.
    """
    db.execute("DELETE FROM ngn_web_uploads WHERE session_id = ? AND inbox_id = 0", (session_id,))
    db.execute("DELETE FROM ngn_web_upload_scopes WHERE session_id = ?", (session_id,))
    with closing(
        db.execute(
            "INSERT OR IGNORE INTO ngn_web_deleted_messages "
            "SELECT channel, message_id FROM ngn_web_inbox "
            "WHERE session_id = ? AND channel != '' AND status NOT IN ('completed', 'failed', 'interrupted')",
            (session_id,),
        )
    ):
        pass
    with closing(
        db.execute(
            "UPDATE ngn_web_inbox SET status = 'interrupted' "
            "WHERE session_id = ? AND status NOT IN ('completed', 'failed', 'interrupted')",
            (session_id,),
        )
    ):
        pass


def _remove_content(db: sqlite3.Connection, session_id: str, tables: set[str]) -> None:
    cleanup_deliveries(db, session_id)
    db.execute("DELETE FROM ngn_web_uploads WHERE session_id = ?", (session_id,))
    db.execute("DELETE FROM ngn_web_upload_scopes WHERE session_id = ?", (session_id,))

    # Preserve only channel/message identities, never envelopes or conversation
    # content. A late connector redelivery must not re-execute deleted history.
    # ngn_web_session_owners is deliberately retained: deleting/recreating an ID
    # cannot transfer its historical chat ownership.
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


def _selection(db: sqlite3.Connection, selected: str) -> str:
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


def _delete_rows(db: sqlite3.Connection, session_id: str, selected: str) -> str:
    # Root membership, never an ID prefix or raw history, authorizes destruction.
    RoutingStore.root(db, session_id)
    tables = _guard_rows(db, session_id)
    _release_bindings(db, session_id)
    _remove_content(db, session_id, tables)
    return _selection(db, selected)


def _guard_process(state: WebState, session_id: str) -> None:
    harness = state.harness
    if any(info.session_id == session_id for info in harness.tasks._infos.values()):
        raise HTTPException(
            409,
            "Session has retained descendant tasks. Finish or cancel them, then restart ngn before "
            "deleting this root. Retained task handles expire on restart; child history is kept.",
        )
    if any(not worker.done() for worker in harness.tasks._workers.values()):
        raise HTTPException(409, "Descendant tasks are still running. Finish or cancel them before deleting.")
    if any(item.session_id == session_id for item in state.wakeups.pending.values()):
        raise HTTPException(409, "Session has pending wakeups. Let them finish or cancel their run before deleting.")


def _repoint_mains(state: WebState, session_id: str, selected: str) -> None:
    """Keep configured connectors pointed at a live root after deletion.

    The replacement root is the same selection the delete transaction committed.
    The live catalog is updated first, so routing never observes a deleted main
    even if persistence is unavailable (demo) or fails. Persistence is best
    effort: a stale on-disk main is tolerated at startup and re-derived on the
    next configure.
    """
    catalog = state.channels.catalog
    affected = [key for key, connection in catalog.connections.items() if connection.main_session_id == session_id]
    if not affected:
        return
    connections = dict(catalog.connections)
    for key in affected:
        connections[key] = connections[key].model_copy(update={"main_session_id": selected})
    catalog.connections = connections
    if not catalog.allow_plugins:
        return
    try:
        catalog.save(connections)
    except Exception:
        logger.warning("Channel catalog main repoint was not persisted; it will be re-derived on next configure")


async def delete_session(state: WebState, session_id: str, *, permanent: bool = False) -> dict[str, object]:
    with state.idle():
        harness = state.harness
        if harness._busy:
            raise HTTPException(409, "Harness busy. Finish the active operation before deleting.")
        with harness.operation("delete session"):

            async def remove() -> dict[str, object]:
                # Retained handles can resume descendants and carry notification
                # queues. Fail closed rather than partially dismantling the task
                # registry or guessing ownership of raw child rows after restart.
                _guard_process(state, session_id)
                extra: dict[str, object] = {}
                if permanent:
                    selected = await state.channels.store._transaction(
                        lambda db: _delete_rows(db, session_id, state.selected_session_id)
                    )
                else:
                    selected, item = await state.channels.store._transaction(
                        lambda db: state.trash.remove_rows(db, session_id, state.selected_session_id)
                    )
                    extra["trash"] = item.wire()
                # Repoint connector mains before memory invalidation so no reader
                # ever sees a configured main that no longer exists.
                _repoint_mains(state, session_id, selected)
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
                return {**result, "deleted_session_id": session_id, **extra}

            with anyio.CancelScope(shield=True):
                result = await finish_on_cancel(remove())
            return result
