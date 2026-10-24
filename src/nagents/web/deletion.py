"""Cancel a root's task tree before its owned deletion transaction."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import closing
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException

from nagents.harness.session_identity import session_sequence
from nagents.session.deliveries import cleanup_deliveries

from ._async import finish_on_cancel
from ._async import join_owned
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
    if "ngn_web_agent_sessions" in tables:
        for alias, storage_id in db.execute(
            "SELECT session_id, storage_id FROM ngn_web_agent_sessions WHERE root = ?", (session_id,)
        ).fetchall():
            try:
                sequence = session_sequence(str(alias))
            except ValueError:
                raise HTTPException(409, "Child identity is inconsistent. Deletion was not performed.") from None
            if (
                "nagents_agent_sessions" not in tables
                or db.execute(
                    "SELECT 1 FROM nagents_agent_sessions WHERE sequence = ? AND root_session_id = ? AND storage_id = ?",
                    (sequence, session_id, storage_id),
                ).fetchone()
                is None
            ):
                raise HTTPException(409, "Child history ownership is inconsistent. Deletion was not performed.")
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


def _quarantine_pending(db: sqlite3.Connection, session_id: str, tables: set[str]) -> None:
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
    # A stopped runtime may have failed to journal its last lifecycle event.
    # Restore must never present those saved descendants as still running.
    tasks = (
        db.execute("SELECT session_id, data FROM ngn_web_agent_sessions WHERE root = ?", (session_id,)).fetchall()
        if "ngn_web_agent_sessions" in tables
        else []
    )
    for agent_session_id, data in tasks:
        task = json.loads(data)
        if task.get("status") == "running":
            task.update(status="cancelled", phase="cancelled", error="Session removed before completion.")
            db.execute(
                "UPDATE ngn_web_agent_sessions SET data = ? WHERE root = ? AND session_id = ?",
                (json.dumps(task), session_id, agent_session_id),
            )
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
    children: set[str] = set()
    if "ngn_web_agent_sessions" in tables:
        children.update(
            str(row[0])
            for row in db.execute("SELECT storage_id FROM ngn_web_agent_sessions WHERE root = ?", (session_id,))
        )
    if "nagents_agent_sessions" in tables:
        children.update(
            str(row[0])
            for row in db.execute(
                "SELECT storage_id FROM nagents_agent_sessions WHERE root_session_id = ?", (session_id,)
            )
        )
    for child in children:
        if child == session_id or db.execute("SELECT 1 FROM harness_sessions WHERE id = ?", (child,)).fetchone():
            raise HTTPException(409, "Child mapping points to a root conversation. Deletion was not performed.")
        cleanup_deliveries(db, child)
        db.execute("DELETE FROM v2_messages WHERE session_id = ?", (child,))
        db.execute("DELETE FROM v2_sessions WHERE id = ?", (child,))
    if "ngn_web_agent_sessions" in tables:
        db.execute("DELETE FROM ngn_web_agent_sessions WHERE root = ?", (session_id,))
    if "ngn_web_agent_inputs" in tables:
        db.execute(
            "UPDATE ngn_web_agent_inputs SET text = '', status = CASE WHEN status = 'queued' THEN 'error' ELSE status END, "
            "revision = revision + 1, reason = 'Root session permanently deleted.' WHERE root = ?",
            (session_id,),
        )
    if "nagents_agent_sessions" in tables:
        # AUTOINCREMENT's sqlite_sequence high-water mark deliberately survives this cleanup.
        db.execute("DELETE FROM nagents_agent_sessions WHERE root_session_id = ?", (session_id,))
    if "ngn_web_group_members" in tables:
        db.execute("DELETE FROM ngn_web_group_members WHERE session_id = ?", (session_id,))
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


async def _release_process(state: WebState, session_id: str, *, journal: bool = True) -> None:
    """Join the exact root before deleting data that its producers could recreate.

    The caller owns the host mutation boundary and task-input admission lock.
    Other execution lanes keep running; neither their task managers nor the
    shared credential owner are shut down.
    """
    harness = state.harness_for(session_id)
    run = state.run_for(session_id)
    state.wakeups.cancel_session(session_id)
    if run is not None:
        await state.stop(run)
    if claim := state.channels.work_tasks.get(session_id):
        # The producer can finish before its outer durable-inbox finalizer.
        # Join that writer as well before deleting its session's rows.
        with suppress(asyncio.CancelledError):
            await join_owned(claim)
    await state.channels.join_claim_cleanup(session_id)
    owners = {harness, state.harness, *state.executions.owners.values()}
    released = 0
    for owner in owners:
        # Persist even a child whose startup event had not reached the web
        # journal when cancellation won. Ownership comes from the task manager,
        # never a child-session name prefix.
        if journal:
            for info in tuple(owner.tasks._infos.values()):
                if info.root_session_id == session_id:
                    await state.tasks.store.save(session_id, state.tasks.task(asdict(info), owner), info._storage_id)
        infos = await owner.tasks.release_session(session_id)
        released += len(infos)
        if journal:
            for info in infos:
                await state.tasks.store.save(session_id, state.tasks.task(asdict(info), owner), info._storage_id)
    state.tasks.forget_session(session_id)
    logger.info("Session task tree released: session=%s tasks=%d stopped_run=%s", session_id, released, run is not None)


def _validate_removal(state: WebState, db: sqlite3.Connection, session_id: str, permanent: bool) -> None:
    if not permanent and (item := state.trash._find(db, session_id)) is not None:
        state.trash._generation(db, session_id, item.deletion_id)
    else:
        RoutingStore.root(db, session_id)
    _guard_rows(db, session_id)


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
    # Exclude new scheduler claims and join task-input admissions before stopping
    # the target tree. Keep both boundaries through cancellation and the commit.
    async with state.queued_mutation(), state.tasks.admission:

        async def remove() -> dict[str, object]:
            owner = state.harness_for(session_id)
            if owner.session_id == session_id and owner._busy and state.run_for(session_id) is None:
                raise HTTPException(409, "This chat is updating. Retry deletion when the update finishes.")
            await state.channels.store._transaction(lambda db: _validate_removal(state, db, session_id, permanent))
            await _release_process(state, session_id)
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
            harness = state.harness
            if harness.session_id == session_id:
                harness.session_id = selected
                harness.tools.read_hashes.clear()
            state.bus.delete_session(session_id)
            state.observe_run(None, {"event": "session_deleted", "root_session_id": session_id})
            state.wakeups.forget_session(session_id)
            await state.executions.dispose(session_id)
            result = await state.snapshot(selected)
            state.bus.publish({"type": "sessions", "sessions": result["sessions"]})
            return {**result, "deleted_session_id": session_id, **extra}

        with anyio.CancelScope(shield=True):
            result = await finish_on_cancel(remove())
        return result
