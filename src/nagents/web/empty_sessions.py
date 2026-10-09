"""Discard only unused, unnamed web drafts at the host's idle boundary."""

from __future__ import annotations

import re
from contextlib import nullcontext
from dataclasses import asdict
from typing import TYPE_CHECKING

from ._async import finish_on_cancel
from .deletion import _guard_rows
from .deletion import _remove_content
from .history import WebHistory
from .routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3

    from .service import WebState

_ROOT = re.compile(r"ngn-[A-Za-z0-9-]{1,76}\Z")
_REFERENCES = (
    ("v2_messages", "session_id = ?"),
    ("ngn_web_inbox", "session_id = ?"),
    ("nagents_channel_inbox", "session_id = ?"),
    ("ngn_web_uploads", "session_id = ?"),
    ("ngn_web_group_members", "session_id = ?"),
    ("ngn_local_deliveries", "root_session_id = ? OR actor_session_id = ?"),
    ("ngn_web_session_owners", "session_id = ?"),
    ("ngn_web_bindings", "session_id = ? OR default_session_id = ?"),
    ("ngn_web_session_trash", "id = ?"),
    ("ngn_design_sessions", "session_id = ? AND (agent != '' OR source != '')"),
)


def _empty_roots(db: sqlite3.Connection, active_voice: str) -> list[str]:
    tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    active_roots = (
        {
            str(row[0])
            for row in db.execute(
                "SELECT session_id FROM ngn_web_live_calls WHERE voice_session_id = ?", (active_voice,)
            )
        }
        if active_voice and "ngn_web_live_calls" in tables
        else set()
    )
    candidates = db.execute(
        "SELECT h.id FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id "
        "WHERE h.title = '' AND s.user_id = 'harness' AND s.compacted_at_message_id IS NULL"
    ).fetchall()
    empty: list[str] = []
    for row in candidates:
        session_id = str(row[0])
        if not _ROOT.fullmatch(session_id) or session_id in active_roots:
            continue
        if any(
            table in tables
            and db.execute(
                f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", (session_id,) * predicate.count("?")
            ).fetchone()
            is not None
            for table, predicate in _REFERENCES
        ):
            continue
        if (
            "ngn_web_live_captions" in tables
            and db.execute(
                "SELECT 1 FROM ngn_web_live_captions c JOIN ngn_web_live_calls l USING (voice_session_id) "
                "WHERE l.session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        ):
            continue
        empty.append(session_id)
    return empty


def _protected(state: WebState) -> set[str]:
    return {
        *(
            info.session_id
            for harness in {state.harness, *state.executions.owners.values()}
            for info in harness.tasks._infos.values()
        ),
        *(item.session_id for item in state.wakeups.pending.values()),
        *(connection.main_session_id for connection in state.channels.catalog.connections.values()),
        *state.executions.runs,
        *state.channels.work_tasks,
    }


def _remove(db: sqlite3.Connection, candidates: list[str], protected: set[str]) -> list[str]:
    removed: list[str] = []
    for session_id in candidates:
        if session_id in protected:
            continue
        RoutingStore.root(db, session_id)
        tables = _guard_rows(db, session_id)
        # A checked but unconfigured main-assistant pin contains no user data.
        if "ngn_design_sessions" in tables:
            db.execute("DELETE FROM ngn_design_sessions WHERE session_id = ?", (session_id,))
        _remove_content(db, session_id, tables)
        removed.append(session_id)
    return removed


async def _invalidate(state: WebState, removed: list[str]) -> None:
    if not removed:
        return
    state.session_revision += 1
    for session_id in removed:
        state.bus.delete_session(session_id)
        state.observe_run(None, {"event": "session_deleted", "session_id": session_id})
        state.wakeups.forget_session(session_id)
    state.bus.publish({"type": "sessions", "sessions": [asdict(item) for item in await state.list_sessions()]})


def _in_use(state: WebState) -> set[str]:
    # A second browser may have unsent text in another otherwise empty draft.
    return {subscriber.session_id for subscriber in state.bus.subscribers if not subscriber.close_code}


async def prune(state: WebState, active_voice: str = "") -> list[str]:
    """Caller holds state.idle(); selection and referenced data are never removed."""
    if (
        state.active is not None
        or state.harness._busy
        or type(state.harness.agent.session) is not WebHistory
        or any(not task.done() for task in state.harness.tasks._workers.values())
    ):
        return []

    async def finish() -> list[str]:
        with state.harness.operation("discard empty sessions"):
            protected = _protected(state) | _in_use(state) | {state.selected_session_id}
            removed = await state.channels.store._transaction(
                lambda db: _remove(db, _empty_roots(db, active_voice), protected)
            )
            await _invalidate(state, removed)
            return removed

    return await finish_on_cancel(finish())


async def continue_session(state: WebState) -> str:
    """Prefer existing meaningful history; keep one draft when no history exists.

    Selection is read-only here. Pruning waits until channel catalog references
    and routing schemas have initialized, before any root can be discarded.
    """
    sessions = await state.harness.list_sessions()
    if not sessions:
        return await state.harness.new_session()
    empty = (
        set(await state.channels.store._transaction(lambda db: _empty_roots(db, "")))
        if type(state.harness.agent.session) is WebHistory
        else set()
    )
    selected = next((item.id for item in sessions if item.id not in empty), sessions[0].id)
    await state.harness.resume(selected)
    return selected


async def new_session(state: WebState, active_voice: str = "") -> str:
    """Reuse the selected draft or atomically create one and discard abandoned drafts."""

    async def finish() -> str:
        harness = state.harness
        if type(harness.agent.session) is not WebHistory:
            state.selected_session_id = await harness.new_session()
            return state.selected_session_id
        with nullcontext() if harness._busy or state.executions.in_use(harness) else harness.operation("new session"):
            protected = _protected(state)
            in_use = _in_use(state)
            workers = bool(state.executions.runs) or any(not task.done() for task in harness.tasks._workers.values())

            def select(db: sqlite3.Connection) -> tuple[str, list[str]]:
                empty = _empty_roots(db, active_voice)
                selected = state.selected_session_id
                if selected not in empty or selected in protected:
                    selected = RoutingStore.new_root(db, "")
                removed = [] if workers else _remove(db, empty, protected | in_use | {selected})
                return selected, removed

            selected, removed = await state.channels.store._transaction(select)
            state.selected_session_id = selected
            if not harness._busy and not state.executions.in_use(harness):
                harness.session_id = selected
                harness._session_created = True
                harness.tools.read_hashes.clear()
            state.session_revision += 1
            await _invalidate(state, removed)
            return selected

    return await finish_on_cancel(finish())
