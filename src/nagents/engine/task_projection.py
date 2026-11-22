"""Bind public child run IDs to observations from exact Harness activations.

The projection shares the engine SQLite transaction and never starts work.
Only a live root run and an observed child start can create a child binding.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from nagents.protocol.models import IDENTIFIER_PATTERN
from nagents.protocol.models import RunStatus
from nagents.protocol.models import TaskInfo

if TYPE_CHECKING:
    import sqlite3

_SAFE_ID = re.compile(IDENTIFIER_PATTERN)
_FIELDS = (
    "run_id, session_id, parent_run_id, parent_session_id, root_run_id, root_session_id, "
    "call_id, title, depth, read_only, status, actor, activation, request_id"
)
_TIME = "CAST(strftime('%s', 'now') AS INTEGER) * 1000 + CAST(substr(strftime('%f', 'now'), 4, 3) AS INTEGER)"


@dataclass(frozen=True)
class ChildActivation:
    task: TaskInfo
    actor: str
    number: int
    request_id: str


def initialize_in(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_engine_tasks ("
        "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_run_id TEXT NOT NULL, "
        "parent_session_id TEXT NOT NULL, root_run_id TEXT NOT NULL, root_session_id TEXT NOT NULL, "
        "call_id TEXT NOT NULL, title TEXT NOT NULL, depth INTEGER NOT NULL, read_only INTEGER NOT NULL, "
        "status TEXT NOT NULL, actor TEXT NOT NULL, activation INTEGER NOT NULL, request_id TEXT NOT NULL, "
        "UNIQUE(root_run_id, actor, activation))"
    )
    db.execute("CREATE INDEX IF NOT EXISTS ngn_engine_task_session ON ngn_engine_tasks(session_id)")
    db.execute("CREATE INDEX IF NOT EXISTS ngn_engine_task_root ON ngn_engine_tasks(root_session_id)")
    db.execute("CREATE INDEX IF NOT EXISTS ngn_engine_task_parent ON ngn_engine_tasks(parent_run_id)")
    # A killed/cancelled owner might not publish the child's normal completion
    # event. Atomically close every remaining child without replaying work.
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_close_children AFTER UPDATE OF status ON ngn_web_inbox "
        "WHEN old.status = 'running' AND new.status IN ('completed','failed','cancelled','interrupted') "
        "AND new.engine_run_id != '' BEGIN "
        "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
        f"SELECT session_id, run_id, {_TIME}, json_object('type', 'run.finished', 'status', 'interrupted', "
        "'error', 'Parent run ended before this child reported completion.') FROM ngn_engine_tasks "
        "WHERE root_run_id = new.engine_run_id AND status = 'running'; "
        "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) "
        f"SELECT root_session_id, root_run_id, {_TIME}, json_object('type', 'task.finished', "
        "'error', 'Parent run ended before this child reported completion.', 'task', json_object("
        "'run_id', run_id, 'session_id', session_id, 'parent_run_id', parent_run_id, "
        "'parent_session_id', parent_session_id, 'root_run_id', root_run_id, 'root_session_id', root_session_id, "
        "'call_id', call_id, 'title', title, 'depth', depth, 'read_only', "
        "json(CASE read_only WHEN 1 THEN 'true' ELSE 'false' END), 'status', 'interrupted')) "
        "FROM ngn_engine_tasks WHERE root_run_id = new.engine_run_id AND status = 'running'; "
        "UPDATE ngn_engine_tasks SET status = 'interrupted' WHERE root_run_id = new.engine_run_id AND status = 'running'; END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_engine_purge_children AFTER DELETE ON v2_sessions BEGIN "
        "DELETE FROM ngn_engine_events WHERE session_id IN "
        "(SELECT session_id FROM ngn_engine_tasks WHERE root_session_id = old.id); "
        "DELETE FROM ngn_engine_tasks WHERE root_session_id = old.id; END"
    )


def _row(value: tuple[object, ...]) -> ChildActivation:
    (
        run,
        session,
        parent,
        parent_session,
        root_run,
        root,
        call,
        title,
        depth,
        read_only,
        status,
        actor,
        number,
        request,
    ) = value
    assert isinstance(actor, str) and type(number) is int and isinstance(request, str)
    return ChildActivation(
        TaskInfo.model_validate(
            {
                "run_id": run,
                "session_id": session,
                "parent_run_id": parent,
                "parent_session_id": parent_session,
                "root_run_id": root_run,
                "root_session_id": root,
                "call_id": call,
                "title": title,
                "depth": depth,
                "read_only": bool(read_only),
                "status": status,
            }
        ),
        actor,
        number,
        request,
    )


def task_run_in(db: sqlite3.Connection, run_id: str) -> ChildActivation | None:
    row = db.execute(f"SELECT {_FIELDS} FROM ngn_engine_tasks WHERE run_id = ?", (run_id,)).fetchone()
    return _row(row) if row else None


def activation_in(db: sqlite3.Connection, root_run: str, actor: object, activation: object) -> ChildActivation | None:
    if not isinstance(actor, str) or type(activation) is not int:
        return None
    row = db.execute(
        f"SELECT {_FIELDS} FROM ngn_engine_tasks WHERE root_run_id = ? AND actor = ? AND activation = ?",
        (root_run, actor, activation),
    ).fetchone()
    return _row(row) if row else None


def children_in(db: sqlite3.Connection, root: str) -> list[TaskInfo]:
    # Each child conversation appears once. The exact run in the result is
    # the newest observed activation, not a mutable pointer for cancellation.
    rows = db.execute(
        f"SELECT {_FIELDS} FROM ngn_engine_tasks WHERE rowid IN "
        "(SELECT MAX(rowid) FROM ngn_engine_tasks WHERE root_session_id = ? GROUP BY session_id) "
        "ORDER BY rowid DESC LIMIT 1024",
        (root,),
    )
    return [_row(row).task for row in rows]


def root_for_child_in(db: sqlite3.Connection, child_session: str) -> str | None:
    row = db.execute(
        "SELECT root_session_id FROM ngn_engine_tasks WHERE session_id = ? LIMIT 1", (child_session,)
    ).fetchone()
    return str(row[0]) if row else None


def running_descendants_in(db: sqlite3.Connection, run_id: str) -> list[ChildActivation]:
    rows = db.execute(
        "WITH RECURSIVE tree(run_id) AS ("
        "SELECT run_id FROM ngn_engine_tasks WHERE parent_run_id = ? UNION ALL "
        "SELECT child.run_id FROM ngn_engine_tasks child JOIN tree ON child.parent_run_id = tree.run_id) "
        f"SELECT {_FIELDS} FROM ngn_engine_tasks WHERE run_id IN (SELECT run_id FROM tree) AND status = 'running'",
        (run_id,),
    )
    return [_row(row) for row in rows]


def start_in(
    db: sqlite3.Connection,
    *,
    root: str,
    root_run: str,
    request_id: str,
    record: dict[str, object],
) -> ChildActivation | None:
    actor, parent, number = record.get("session_id"), record.get("parent_session_id"), record.get("activation")
    if (
        record.get("root_session_id") != root
        or not isinstance(actor, str)
        or not _SAFE_ID.fullmatch(actor)
        or len(actor) > 200
        or type(number) is not int
        or number < 0
        or activation_in(db, root_run, actor, number) is not None
    ):
        return None
    parent_run, parent_session, depth = root_run, root, 1
    if parent not in ("", root):
        if not isinstance(parent, str) or parent == actor:
            return None
        observed = activation_in(db, root_run, parent, record.get("parent_activation"))
        if observed is None:
            return None  # Never substitute the latest or currently running parent.
        binding = observed.task
        parent_run, parent_session, depth = binding.run_id, binding.session_id, binding.depth + 1
    if record.get("depth") != depth:
        return None
    session = "child-" + hashlib.sha256(f"{root}\0{actor}".encode()).hexdigest()[:32]
    title, call_id = record.get("title"), record.get("activation_call_id")
    task = TaskInfo(
        run_id=f"task-{uuid4().hex}",
        session_id=session,
        parent_run_id=parent_run,
        parent_session_id=parent_session,
        root_run_id=root_run,
        root_session_id=root,
        call_id=call_id if isinstance(call_id, str) else "",
        title=title if isinstance(title, str) else actor,
        depth=depth,
        read_only=record.get("read_only") is not False,
        status="running",
    )
    db.execute(
        f"INSERT INTO ngn_engine_tasks({_FIELDS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task.run_id,
            task.session_id,
            task.parent_run_id,
            task.parent_session_id,
            task.root_run_id,
            task.root_session_id,
            task.call_id,
            task.title,
            task.depth,
            int(task.read_only),
            task.status,
            actor,
            number,
            request_id,
        ),
    )
    return ChildActivation(task, actor, number, request_id)


def finish_in(db: sqlite3.Connection, active: ChildActivation, status: RunStatus) -> TaskInfo | None:
    if status not in {"completed", "failed", "cancelled", "interrupted"}:
        return None
    changed = db.execute(
        "UPDATE ngn_engine_tasks SET status = ? WHERE run_id = ? AND status = 'running'",
        (status, active.task.run_id),
    ).rowcount
    return active.task.model_copy(update={"status": status}) if changed else None
