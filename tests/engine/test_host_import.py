"""Offline legacy archive import, idempotence and isolation from execution."""

from __future__ import annotations

import asyncio
import json
import select
import sqlite3
import subprocess
import sys
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from nagents.engine.cli import main
from nagents.engine.host_import import import_host
from nagents.engine.registration import EngineStateError
from nagents.engine.registration import own_state
from nagents.harness.runtime.core import session_database_path
from nagents.protocol.events import RunFinished
from nagents.protocol.events import RunStarted
from nagents.protocol.events import TextCompleted
from nagents.protocol.events import ToolStarted
from tests.engine.test_runtime import BASE
from tests.engine.test_runtime import _app
from tests.engine.test_runtime import _history
from tests.engine.test_runtime import _input
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


def _make_snapshot(path: Path, *, corrupt: bool = False) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "PRAGMA user_version=5;"
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, updated_at_ms INTEGER);"
            "CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT, prompt TEXT, status TEXT);"
            "CREATE TABLE events(sequence INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, "
            "timestamp_ms INTEGER, payload TEXT);"
            "CREATE TABLE subagents(child_session_id TEXT PRIMARY KEY);"
            "INSERT INTO sessions VALUES ('root', 'Legacy history', 1700000000000), "
            "('child', 'Private child', 1700000000000);"
            "INSERT INTO subagents VALUES ('child');"
            "INSERT INTO runs VALUES ('r1', 'root', 'old-1', 'Old user prompt', 'completed');"
            "INSERT INTO runs VALUES ('r2', 'root', 'old-2', 'Old write request', 'running');"
            "INSERT INTO runs VALUES ('r3', 'child', 'old-3', 'Private child prompt', 'completed');"
            "INSERT INTO runs VALUES ('pending', 'root', 'old-receipt', 'unstarted private prompt', 'queued');"
        )
        for run, session, payload in [
            ("r1", "root", {"event": "run_started", "prompt": "Old user prompt"}),
            ("r1", "root", {"event": "text_completed", "text": "Old model response"}),
            ("r1", "root", {"event": "run_finished", "status": "completed", "error": ""}),
            ("pending", "root", {"event": "run_queued", "prompt": "unstarted private prompt"}),
            ("r2", "root", {"event": "run_started", "prompt": "Old write request"}),
            ("r2", "root", {"event": "tool_started", "call": {"name": "write", "call_id": "c", "arguments": {}}}),
            ("r2", "root", {"event": "approval_requested", "approval_id": "stale", "preview": "must not import"}),
            ("r3", "child", {"event": "text_completed", "text": "Private child output"}),
        ]:
            if corrupt and run == "r2":
                payload = {"event": "run_started", "prompt": 42}
            db.execute(
                "INSERT INTO events(run_id, session_id, timestamp_ms, payload) VALUES (?, ?, 1700000000000, ?)",
                (run, session, json.dumps(payload)),
            )
    path.chmod(0o600)


def test_snapshot_import_is_one_way_and_not_a_runnable_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "snapshot.sqlite3"
    _make_snapshot(source)
    before = source.read_bytes()
    state = tmp_path / "state"
    argv = ["import-host", "--workspace", str(tmp_path), "--state-dir", str(state), "--host-snapshot", str(source)]
    assert main(argv) == 0
    assert "Imported: 1 read-only sessions, 6 events." in capsys.readouterr().out
    assert main(argv) == 0
    assert "Already imported" in capsys.readouterr().out

    async def scenario() -> None:
        async with _app(tmp_path) as (http, _, _):
            (archive,) = [row for row in (await http.get(f"{BASE}/sessions")).json() if row["archived"]]
            root = archive["id"]
            assert archive["title"] == "Legacy history"
            assert (await http.get(f"{BASE}/sessions/{root}")).json() == archive
            history = await _history(http, root)
            assert [type(item.event) for item in history.events] == [
                RunStarted,
                TextCompleted,
                RunFinished,
                RunStarted,
                ToolStarted,
                RunFinished,
            ]
            terminal = history.events[-1]
            assert isinstance(terminal.event, RunFinished) and terminal.event.status == "interrupted"
            assert (await http.post(f"{BASE}/runs/{terminal.run_id}/cancel")).status_code == 404
            assert (
                await http.post(f"{BASE}/sessions/{root}/runs", json=_input("not a runnable session"))
            ).status_code == 404
            assert (await http.post(f"{BASE}/sessions/{root}/fork", json={})).status_code == 404
            assert (await http.get(f"{BASE}/sessions/{root}/queue")).json()["entries"] == []
            assert (await http.get(f"{BASE}/sessions/{root}/tasks")).json() == []
            assert (await http.get(f"{BASE}/state")).json()["approvals"] == []
            assert not (tmp_path / "host.sqlite3").exists()

    asyncio.run(scenario())
    assert source.read_bytes() == before
    assert sorted(path.name for path in tmp_path.glob("snapshot.sqlite3*")) == [source.name]
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("UPDATE sessions SET title = 'Changed source'")
    with pytest.raises(ValueError, match="already imported a different"):
        import_host(source, state, session_database_path(tmp_path, state))


def test_invalid_snapshot_cannot_commit_marker_or_partial_history(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.sqlite3"
    _make_snapshot(source, corrupt=True)
    state = tmp_path / "state"
    database = session_database_path(tmp_path, state)
    with pytest.raises(ValueError, match="no archive was committed"):
        import_host(source, state, database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'ngn_engine_%'").fetchall() == []
    repaired = tmp_path / "repaired.sqlite3"
    _make_snapshot(repaired)
    assert import_host(repaired, state, database).sessions == 1


def test_import_refuses_live_sidecars_aliases_and_an_owned_destination(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.sqlite3"
    _make_snapshot(source)
    state = tmp_path / "state"
    database = session_database_path(tmp_path, state)
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        import_host(alias, state, database)
    companion = tmp_path / "snapshot.sqlite3-wal"
    companion.touch()
    with pytest.raises(ValueError, match="standalone SQLite backup"):
        import_host(source, state, database)
    companion.unlink()
    with own_state(state), pytest.raises(EngineStateError, match="already owns"):
        import_host(source, state, database)
    assert not database.exists()


def test_import_retries_after_kill_between_writes_and_commit(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.sqlite3"
    _make_snapshot(source)
    original = source.read_bytes()
    state = tmp_path / "state"
    database = session_database_path(tmp_path, state)
    command = """
import os
import sys
from pathlib import Path
from nagents.engine import host_import
write = host_import._archive_in
def hold(*args):
    result = write(*args)
    print("Rows prepared; transaction not committed", flush=True)
    os.read(0, 1)
    return result
host_import._archive_in = hold
host_import.import_host(*map(Path, sys.argv[1:]))
"""
    with subprocess.Popen(
        [sys.executable, "-c", command, str(source), str(state), str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        try:
            assert select.select([child.stdout], [], [], HANG_GUARD)[0], "Import did not reach its transaction"
            assert child.stdout.readline().strip() == "Rows prepared; transaction not committed"
        finally:
            child.kill()
            child.wait(timeout=HANG_GUARD)
    report = import_host(source, state, database)
    assert not report.already_imported and report.events == 6
    assert import_host(source, state, database).already_imported
    assert source.read_bytes() == original
