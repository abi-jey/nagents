"""Permanent removal purges allocated shell data without touching other roots."""

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from nagents.web.sessions.deletion import _purge_shell_jobs

pytestmark = pytest.mark.requires_posix


def fixture(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    db = sqlite3.connect(tmp_path / "sessions.db")
    db.execute(
        "CREATE TABLE ngn_shell_jobs (job_id TEXT PRIMARY KEY, root_session_id TEXT, session_id TEXT, output_path TEXT, terminal_json TEXT)"
    )
    db.execute("CREATE TABLE ngn_shell_outbox (job_id TEXT)")
    root = tmp_path / "shell-jobs"
    root.mkdir(mode=0o700)
    for job_id, owner, session in (
        ("root-job", "root", "root"),
        ("child-job", "root", "child"),
        ("other-job", "other", "other"),
    ):
        directory = root / job_id
        directory.mkdir(mode=0o700)
        output = directory / "output.log"
        output.write_text("PRIVATE SHELL OUTPUT")
        output.chmod(0o600)
        db.execute("INSERT INTO ngn_shell_jobs VALUES (?,?,?,?,?)", (job_id, owner, session, str(output), "{}"))
        db.execute("INSERT INTO ngn_shell_outbox VALUES (?)", (job_id,))
    db.commit()
    return db, root


def test_permanent_purge_removes_root_and_descendant_only(tmp_path: Path) -> None:
    db, root = fixture(tmp_path)
    try:
        with db:
            _purge_shell_jobs(db, "root", {"ngn_shell_jobs", "ngn_shell_outbox"})
        assert not (root / "root-job/output.log").exists()
        assert not (root / "child-job/output.log").exists()
        assert (root / "other-job/output.log").read_text() == "PRIVATE SHELL OUTPUT"
        assert db.execute("SELECT job_id FROM ngn_shell_jobs").fetchall() == [("other-job",)]
        assert db.execute("SELECT job_id FROM ngn_shell_outbox").fetchall() == [("other-job",)]
    finally:
        db.close()


def test_purge_rejects_symlink_and_keeps_external_data(tmp_path: Path) -> None:
    db, root = fixture(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("KEEP")
    output = root / "root-job/output.log"
    output.unlink()
    output.symlink_to(outside)
    try:
        with pytest.raises(HTTPException, match="409"), db:
            _purge_shell_jobs(db, "root", {"ngn_shell_jobs", "ngn_shell_outbox"})
        assert outside.read_text() == "KEEP"
        assert db.execute("SELECT COUNT(*) FROM ngn_shell_jobs").fetchone() == (3,)
    finally:
        db.close()


def test_purge_refuses_running_job_before_any_unlink(tmp_path: Path) -> None:
    db, root = fixture(tmp_path)
    db.execute("UPDATE ngn_shell_jobs SET terminal_json='' WHERE job_id='child-job'")
    db.commit()
    try:
        with pytest.raises(HTTPException):
            _purge_shell_jobs(db, "root", {"ngn_shell_jobs", "ngn_shell_outbox"})
        assert (root / "root-job/output.log").exists()
        assert (root / "child-job/output.log").exists()
    finally:
        db.close()
