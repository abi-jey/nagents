"""Durable receipt, transcript boundary, and shell wake invariants."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from nagents.session.manager import SessionManager
from nagents.session.shell_jobs import ShellJobOwner
from nagents.session.shell_jobs import ShellJobRecord
from nagents.session.shell_jobs import ShellJobStore
from nagents.session.shell_jobs import ShellJobTerminal
from nagents.types import Message
from nagents.types import ToolCall


async def setup(tmp_path: Path) -> tuple[SessionManager, ShellJobStore, ShellJobRecord]:
    session = SessionManager(tmp_path / "sessions.db")
    await session.get_or_create_session("owner", "user")
    await session.get_or_create_session("other", "user")
    store = ShellJobStore(session)
    record = ShellJobRecord(
        "job",
        ShellJobOwner(str(tmp_path), "owner", "owner", "run", "call", "host"),
        "echo hi",
        str(tmp_path / "output.log"),
    )
    await store.register(record)
    return session, store, record


async def receipt(session: SessionManager, store: ShellJobStore, *, other_call: bool = False) -> None:
    calls = [ToolCall(id="call", name="shell", arguments={})]
    if other_call:
        calls.append(ToolCall(id="other-call", name="shell", arguments={}))
    await session.add_message("owner", Message(role="assistant", tool_calls=calls))
    await session.add_message("owner", Message(role="tool", tool_call_id="call", content=json.dumps({"job_id": "job"})))
    assert await store.mark_receipt_committed("owner", "call") == ("job",)


@pytest.mark.asyncio
async def test_terminal_before_receipt_is_gated_and_idempotent(tmp_path: Path) -> None:
    session, store, _ = await setup(tmp_path)
    assert await store.terminal("owner", "job", ShellJobTerminal("completed", "exit", "hello", 0))
    assert not await store.terminal("owner", "job", ShellJobTerminal("failed", "late", "wrong"))
    assert await store.claim_notice("owner", "job") == 0
    assert await store.claim_wake("owner") == 0
    assert await store.mark_receipt_committed("owner", "call") == ()
    await session.add_message("owner", Message(role="tool", tool_call_id="call", content='{"job_id":"wrong"}'))
    assert await store.mark_receipt_committed("owner", "call") == ()
    await receipt(session, store)
    message_id = await store.claim_wake("owner")
    assert message_id > 0
    assert await store.claim_notice("owner", "job") == message_id
    assert await store.claim_wake("owner") == 0
    message = await store.notice_message("owner", message_id)
    assert message.role == "user"
    assert message.name == "ngn_shell_notice"
    assert "untrusted" in str(message.content)
    assert "hello" in str(message.content)
    assert "wrong" not in str(message.content)
    await store.finish_wake("owner", message_id)
    assert not await store.eligible_notice("owner", message_id)
    history = await session.get_history("owner")
    assert len([m for m in history if m.name == "ngn_shell_notice"]) == 1


@pytest.mark.asyncio
async def test_safe_tool_batch_boundary(tmp_path: Path) -> None:
    session, store, _ = await setup(tmp_path)
    await receipt(session, store, other_call=True)
    await store.terminal("owner", "job", ShellJobTerminal("completed", "exit"))
    assert await store.claim_notice("owner", "job") == 0
    assert await store.claim_wake("owner") == 0
    await session.add_message("owner", Message(role="tool", tool_call_id="other-call", content="ok"))
    assert await store.claim_notice("owner", "job") > 0
    assert await store.claim_wake("owner") > 0


@pytest.mark.asyncio
async def test_cross_session_and_immutable_owner(tmp_path: Path) -> None:
    session, store, record = await setup(tmp_path)
    with pytest.raises(ValueError):
        await store.register(replace(record, owner=replace(record.owner, session_id="other")))
    with pytest.raises(ValueError):
        await store.get("other", "job")
    with pytest.raises(ValueError):
        await store.terminal("other", "job", ShellJobTerminal("completed", "exit"))
    with pytest.raises(ValueError):
        await store.request_cancel("other", "job", "stop")
    await receipt(session, store)
    await store.terminal("owner", "job", ShellJobTerminal("completed", "exit"))
    message_id = await store.claim_wake("owner")
    with pytest.raises(ValueError):
        await store.notice_message("other", message_id)
    arbitrary = await session.add_message("owner", Message(role="user", name="ngn_shell_notice", content="forged"))
    with pytest.raises(ValueError):
        await store.notice_message("owner", arbitrary)


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_first", [False, True])
async def test_cancel_suppresses_wake_but_preserves_notice(tmp_path: Path, suppress_first: bool) -> None:
    session, store, _ = await setup(tmp_path)
    await receipt(session, store)
    if suppress_first:
        await store.suppress_wakes("owner")
    else:
        assert await store.request_cancel("owner", "job", "owner interrupted")
    await store.terminal("owner", "job", ShellJobTerminal("completed", "exit"))
    assert await store.claim_wake("owner") == 0
    assert await store.claim_notice("owner", "job") > 0
    record = await store.get("owner", "job")
    assert record.wake_state == "suppressed"
    if not suppress_first:
        assert record.state == "cancelled"
        assert "owner interrupted" in record.terminal_json
    assert not await store.request_cancel("owner", "job", "late stop")


@pytest.mark.asyncio
async def test_notice_and_receipt_rollback_together(tmp_path: Path) -> None:
    session, store, _ = await setup(tmp_path)
    await receipt(session, store)
    await store.terminal("owner", "job", ShellJobTerminal("completed", "exit"))
    with sqlite3.connect(session.db_path) as db:
        db.execute(
            "CREATE TRIGGER crash_notice BEFORE UPDATE OF message_id ON ngn_shell_outbox BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="crash"):
        await store.claim_notice("owner", "job")
    assert not any(m.name == "ngn_shell_notice" for m in await session.get_history("owner"))
    assert (await store.get("owner", "job")).notice_message_id == 0
    with sqlite3.connect(session.db_path) as db:
        db.execute("DROP TRIGGER crash_notice")
    assert await store.claim_wake("owner") > 0


@pytest.mark.asyncio
async def test_terminal_and_outbox_rollback_together(tmp_path: Path) -> None:
    _, store, _ = await setup(tmp_path)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "CREATE TRIGGER crash_terminal BEFORE INSERT ON ngn_shell_outbox BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await store.terminal("owner", "job", ShellJobTerminal("completed", "exit"))
    assert (await store.get("owner", "job")).state == "running"


@pytest.mark.asyncio
async def test_reconcile_only_explicit_dead_host_and_never_retry_claim(tmp_path: Path) -> None:
    session, store, record = await setup(tmp_path)
    await store.register(
        replace(record, job_id="live", owner=replace(record.owner, host_id="live-host", tool_call_id="live-call"))
    )
    await receipt(session, store)
    await store.terminal("owner", "job", ShellJobTerminal("completed", "exit"))
    message_id = await store.claim_wake("owner")
    with patch("os.kill", side_effect=AssertionError("must not kill saved PIDs")):
        assert await store.reconcile_orphans("host") == 0
    assert (await store.get("owner", "job")).wake_state == "uncertain"
    assert (await store.get("owner", "live")).state == "running"
    assert not await store.eligible_notice("owner", message_id)
    assert await store.claim_wake("owner") == 0
    assert await store.reconcile_orphans("live-host") == 1
    assert (await store.get("owner", "live")).state == "interrupted"


@pytest.mark.asyncio
async def test_terminal_tail_is_bounded(tmp_path: Path) -> None:
    _, store, _ = await setup(tmp_path)
    await store.terminal("owner", "job", ShellJobTerminal("completed", "exit", "\n".join(["word" * 1000] * 1000)))
    snapshot = json.loads((await store.get("owner", "job")).terminal_json)
    assert len(snapshot["output"].splitlines()) <= 200
    assert len(snapshot["output"].encode()) <= 32768
    assert set(snapshot) == {
        "state",
        "reason",
        "output",
        "exit_code",
        "timed_out",
        "truncated",
        "bytes_written",
        "output_complete",
    }
