"""Permanent removal purges allocated shell data without touching other roots."""

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from nagents.web.sessions.deletion import _purge_shells

pytestmark = pytest.mark.requires_posix


def fixture(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    db = sqlite3.connect(tmp_path / "sessions.db")
    db.execute(
        "CREATE TABLE ngn_shells (shell_id TEXT PRIMARY KEY, root_session_id TEXT, session_id TEXT, output_path TEXT, terminal_json TEXT)"
    )
    db.execute("CREATE TABLE ngn_shell_outbox (shell_id TEXT)")
    root = tmp_path / "shells"
    root.mkdir(mode=0o700)
    for shell_id, owner, session in (
        ("root-shell", "root", "root"),
        ("child-shell", "root", "child"),
        ("other-shell", "other", "other"),
    ):
        directory = root / shell_id
        directory.mkdir(mode=0o700)
        output = directory / "output.log"
        output.write_text("PRIVATE SHELL OUTPUT")
        output.chmod(0o600)
        db.execute("INSERT INTO ngn_shells VALUES (?,?,?,?,?)", (shell_id, owner, session, str(output), "{}"))
        db.execute("INSERT INTO ngn_shell_outbox VALUES (?)", (shell_id,))
    db.commit()
    return db, root


def test_permanent_purge_removes_root_and_descendant_only(tmp_path: Path) -> None:
    db, root = fixture(tmp_path)
    try:
        with db:
            _purge_shells(db, "root", {"ngn_shells", "ngn_shell_outbox"})
        assert not (root / "root-shell/output.log").exists()
        assert not (root / "child-shell/output.log").exists()
        assert (root / "other-shell/output.log").read_text() == "PRIVATE SHELL OUTPUT"
        assert db.execute("SELECT shell_id FROM ngn_shells").fetchall() == [("other-shell",)]
        assert db.execute("SELECT shell_id FROM ngn_shell_outbox").fetchall() == [("other-shell",)]
    finally:
        db.close()


def test_purge_rejects_symlink_and_keeps_external_data(tmp_path: Path) -> None:
    db, root = fixture(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("KEEP")
    output = root / "root-shell/output.log"
    output.unlink()
    output.symlink_to(outside)
    try:
        with pytest.raises(HTTPException, match="409"), db:
            _purge_shells(db, "root", {"ngn_shells", "ngn_shell_outbox"})
        assert outside.read_text() == "KEEP"
        assert db.execute("SELECT COUNT(*) FROM ngn_shells").fetchone() == (3,)
    finally:
        db.close()


def test_purge_refuses_running_job_before_any_unlink(tmp_path: Path) -> None:
    db, root = fixture(tmp_path)
    db.execute("UPDATE ngn_shells SET terminal_json='' WHERE shell_id='child-shell'")
    db.commit()
    try:
        with pytest.raises(HTTPException):
            _purge_shells(db, "root", {"ngn_shells", "ngn_shell_outbox"})
        assert (root / "root-shell/output.log").exists()
        assert (root / "child-shell/output.log").exists()
    finally:
        db.close()
