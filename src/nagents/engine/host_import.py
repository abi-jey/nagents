"""One-way import of display evidence from the retired Rust host's database.

The engine imports a workspace's old `host.sqlite3` automatically, once, on
its first start (`import_legacy_once`). `nagents import-host` remains for
backups taken from a non-default location.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from pydantic import ValidationError

from nagents.protocol.events import EngineEvent
from nagents.protocol.events import RunFinished

from .archives import initialize_in
from .registration import own_state

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_FILE = 1024**3
_MAX_FRAME = 1024**2
_MAX_SESSIONS = 10_000
_MAX_RUNS = 100_000
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_DISPLAY_TYPES = {
    "run_started": "run.started",
    "run_finished": "run.finished",
    "notice": "notice",
    "text_completed": "text.completed",
    "tool_started": "tool.started",
    "tool_completed": "tool.completed",
}
_EVENT = TypeAdapter(EngineEvent)


@dataclass(frozen=True)
class ImportReport:
    sessions: int
    events: int
    ignored_children: int
    already_imported: bool = False


def _offline(path: Path) -> None:
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(os.path.lexists(candidate) for candidate in [path.parent / "host.lock", *sidecars]):
        raise ValueError("Use a standalone SQLite backup outside the legacy state directory, without sidecars.")


@contextmanager
def _snapshot(source: Path) -> Iterator[tuple[sqlite3.Connection, str]]:
    source = source.expanduser().absolute()
    _offline(source)
    digest = hashlib.sha256()
    # Open the original only as a regular file. SQLite sees the private copy,
    # never the source, and cannot open live WAL or journal files beside it.
    with TemporaryDirectory(prefix="nagents-host-import-") as temporary:
        staged = Path(temporary) / "host.sqlite3"
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as data, staged.open("xb") as copy:
            original = os.fstat(data.fileno())
            if (
                not stat.S_ISREG(original.st_mode)
                or original.st_nlink != 1
                or original.st_uid != os.getuid()
                or stat.S_IMODE(original.st_mode) & 0o077
                or not 0 < original.st_size <= _MAX_FILE
            ):
                raise ValueError("The host snapshot must be an owner-only, single-link SQLite file up to 1 GiB.")
            remaining = original.st_size
            while remaining and (page := data.read(min(remaining, _MAX_FRAME))):
                digest.update(page)
                copy.write(page)
                remaining -= len(page)
            final = os.fstat(data.fileno())
            if (
                remaining
                or final.st_size != original.st_size
                or final.st_mtime_ns != original.st_mtime_ns
                or final.st_ctime_ns != original.st_ctime_ns
            ):
                raise ValueError("The host snapshot changed while being read.")
        _offline(source)
        with closing(sqlite3.connect(staged.as_uri() + "?mode=ro&immutable=1", uri=True)) as snapshot:
            snapshot.execute("PRAGMA trusted_schema=OFF")
            snapshot.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * _MAX_FRAME)
            yield snapshot, digest.hexdigest()


def _identifier(prefix: str, *pieces: str) -> str:
    key = json.dumps(pieces, ensure_ascii=False, separators=(",", ":")).encode()
    return f"{prefix}-{hashlib.sha256(key).hexdigest()}"


def _archive_in(destination: sqlite3.Connection, snapshot: sqlite3.Connection, digest: str) -> ImportReport:
    initialize_in(destination)
    previous = destination.execute(
        "SELECT snapshot, sessions, events, ignored_children FROM ngn_engine_host_import WHERE id = 1"
    ).fetchone()
    if previous:
        if previous[0] != digest:
            raise ValueError("This engine has already imported a different Rust host snapshot.")
        return ImportReport(previous[1], previous[2], previous[3], already_imported=True)
    if snapshot.execute("PRAGMA user_version").fetchone() != (5,):
        raise ValueError("The host snapshot must use Rust host schema 5 (ngn 0.100.46).")
    schema = {
        row[0]
        for row in snapshot.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND sql LIKE 'CREATE TABLE %'")
    }
    if not {"sessions", "runs", "events", "subagents"} <= schema:
        raise ValueError("Required Rust host tables are missing.")
    if snapshot.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
        raise ValueError("The Rust host snapshot did not pass SQLite's integrity check.")

    ignored_children = snapshot.execute("SELECT count(DISTINCT child_session_id) FROM subagents").fetchone()[0]
    sessions: dict[str, str] = {}
    for source, title, timestamp in snapshot.execute(
        "SELECT s.id, s.title, s.updated_at_ms FROM sessions s "
        "WHERE NOT EXISTS (SELECT 1 FROM subagents t WHERE t.child_session_id = s.id) "
        "ORDER BY s.id LIMIT ?",
        (_MAX_SESSIONS + 1,),
    ):
        if len(sessions) >= _MAX_SESSIONS or not source or not 0 <= timestamp < 253_402_300_800_000:
            raise ValueError("Invalid or excessive session records in the host snapshot.")
        session = _identifier("host-archive", source)
        destination.execute(
            "INSERT INTO ngn_engine_archives(id, source_id, title, updated_at_ms) VALUES (?,?,?,?)",
            (session, source, title[:160] or "Imported session", timestamp),
        )
        sessions[source] = session

    # Keep only display events. Do not import receipts, user IDs, task/run
    # authority, pending prompt text, approvals, steering or provider state.
    events = 0
    running: dict[tuple[str, str], tuple[str, int]] = {}
    seen: set[tuple[str, str]] = set()
    for source, old_run, timestamp, payload, linked_run in snapshot.execute(
        "SELECT e.session_id, e.run_id, e.timestamp_ms, e.payload, r.id FROM events e "
        "LEFT JOIN runs r ON r.id = e.run_id AND r.session_id = e.session_id "
        "WHERE NOT EXISTS (SELECT 1 FROM subagents t WHERE t.child_session_id = e.session_id) "
        "ORDER BY e.sequence"
    ):
        if (
            source not in sessions
            or old_run != linked_run
            or not 0 <= timestamp < 253_402_300_800_000
            or len(payload.encode()) > _MAX_FRAME
        ):
            raise ValueError("Invalid display event in the host snapshot.")
        record = json.loads(payload)
        tag = record.get("event") if isinstance(record, dict) else None
        if not isinstance(tag, str) or tag not in _DISPLAY_TYPES:
            continue
        key = (source, old_run or "")
        session = sessions[source]
        run = _identifier("archive-run", *key) if old_run else ""
        event = _EVENT.validate_python({**record, "type": _DISPLAY_TYPES[tag]})
        if tag == "run_started":
            if not old_run or key in seen or len(seen) >= _MAX_RUNS:
                raise ValueError("Duplicate or excessive started runs in the host snapshot.")
            seen.add(key)
            running[key] = run, timestamp
        elif tag == "run_finished":
            if key not in running or not isinstance(event, RunFinished) or event.status not in _TERMINAL:
                continue
            del running[key]
        elif tag != "notice" and key not in running:
            # Queued cancellations, unrelated events and missing identities
            # must not be mistaken for a claimed run or a performed action.
            continue
        elif key in running:
            running[key] = run, timestamp
        destination.execute(
            "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) VALUES (?,?,?,?)",
            (session, run, timestamp, event.model_dump_json()),
        )
        events += 1
    unfinished = RunFinished(status="interrupted", error="The legacy host did not record completion. Not replayed.")
    for (source, _), (run, timestamp) in running.items():
        destination.execute(
            "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) VALUES (?,?,?,?)",
            (sessions[source], run, timestamp, unfinished.model_dump_json()),
        )
        events += 1
    destination.execute(
        "INSERT INTO ngn_engine_host_import(id, snapshot, sessions, events, ignored_children) VALUES (1,?,?,?,?)",
        (digest, len(sessions), events, ignored_children),
    )
    return ImportReport(len(sessions), events, ignored_children)


def import_host(source: Path, state_dir: Path, database: Path) -> ImportReport:
    if os.name != "posix":
        raise ValueError("Rust host import requires a POSIX host.")
    if database.name != "sessions.db" or database.parent.parent != state_dir:
        raise ValueError("Rust host import requires a dedicated engine database.")
    if any(os.path.lexists(state_dir / name) for name in ("host.sqlite3", "host.lock", "nagents.sqlite3")):
        raise ValueError("The engine state directory cannot be a legacy state directory.")
    if source.expanduser().resolve().is_relative_to(state_dir):
        raise ValueError("Keep the source snapshot outside the engine state directory.")
    try:
        with _snapshot(source) as (snapshot, digest), own_state(state_dir):
            database.parent.mkdir(mode=0o700, exist_ok=True)
            parent = database.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise ValueError("The engine database directory must be private and not a symlink.")
            fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, "rb") as owned:
                info = os.fstat(owned.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise ValueError("The engine database must be an owner-only, single-link file.")
                with closing(sqlite3.connect(database, timeout=5)) as destination, destination:
                    destination.execute("BEGIN IMMEDIATE")
                    return _archive_in(destination, snapshot, digest)
    except (sqlite3.Error, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ValueError(
            "Invalid or unreadable Rust host snapshot or engine database; no archive was committed."
        ) from error


def legacy_host_database(workspace: Path) -> Path:
    """The default location the Rust host (ngn <= 0.100.46) used for `workspace`."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    digest = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    return Path(base) / "ngn-rust" / "workspaces" / digest / "host.sqlite3"


def _imported(database: Path) -> bool:
    if not database.is_file():
        return False
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        try:
            return connection.execute("SELECT 1 FROM ngn_engine_host_import WHERE id = 1").fetchone() is not None
        except sqlite3.OperationalError:
            return False


def import_legacy_once(workspace: Path, state_dir: Path, database: Path) -> str:
    """Archive this workspace's Rust host history the first time the engine starts.

    Returns a one-line notice for stderr, or "" when there was nothing to do.
    The legacy file is only read, through a private SQLite backup beside it.
    """
    legacy = legacy_host_database(workspace)
    if os.name != "posix" or not legacy.is_file() or _imported(database):
        return ""
    root = legacy.parents[2]
    try:
        with TemporaryDirectory(prefix=".import-", dir=root) as temporary:
            snapshot = Path(temporary) / "host.sqlite3"
            snapshot.touch(mode=0o600)
            with (
                closing(sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)) as live,
                closing(sqlite3.connect(snapshot)) as copy,
            ):
                live.backup(copy)
            report = import_host(snapshot, state_dir, database)
    except (ValueError, OSError, sqlite3.Error) as error:
        return (
            f"Could not import earlier ngn history from {legacy}: {error} "
            "Retry with `nagents import-host --host-snapshot <backup>`."
        )
    return f"Imported {report.sessions} earlier ngn sessions as read-only history."
