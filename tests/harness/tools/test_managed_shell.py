"""Managed shell ownership survives normal turns, but not owner interruption."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import ApprovalRequest
    from nagents.tools.shell import ShellInstance
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_posix]


async def allow(request: ApprovalRequest) -> bool:
    return True


async def test_background_receipt_survives_turn_and_idle_completion_resumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "release"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(
                id="bg",
                name="shell",
                arguments={
                    "command": f"while [ ! -f '{gate}' ]; do sleep .01; done; printf finished",
                    "background": True,
                },
            )
        else:
            yield TextDoneEvent(text="Continuing normally")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        events = await collect(harness, "Start background work")
        receipt = next(event.result for event in events if isinstance(event, ToolResultEvent))
        assert isinstance(receipt, dict) and receipt["shell_id"]
        shell_id = str(receipt["shell_id"])
        assert (await harness.shells.get(shell_id))["state"] == "running"
        assert len(providers[0].requests) == 2
        gate.touch()
        async with asyncio.timeout(3):
            while not (await harness.shells.get(shell_id))["output_complete"]:
                await harness.shells.wait_changed()
        assert (await harness.shells.get(shell_id))["output"] == "finished"
        assert [event async for event in harness.resume_shells()]
        assert len(providers[0].requests) == 3
        assert not [event async for event in harness.resume_shells()]
        history = await harness.history()
        assert len([message for message in history if message.name == "ngn_shell_notice"]) == 1
        assert len([message for message in history if message.role == "tool" and message.tool_call_id == "bg"]) == 1
    finally:
        await harness.close()


async def test_idle_stop_persists_cancellation_without_model_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="bg", name="shell", arguments={"command": "sleep 30", "background": True})
        else:
            yield TextDoneEvent(text="Done")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        await collect(harness)
        shell_id = str((await harness.shells.list())[0]["shell_id"])
        stopped = await harness.shells.stop(shell_id)
        assert stopped["state"] == "cancelled" and stopped["reason"] == "user_stop"
        assert not [event async for event in harness.resume_shells()]
        assert len(providers[0].requests) == 2
        assert (await harness.shells.stop(shell_id))["reason"] == "user_stop"
    finally:
        await harness.close()


async def test_generator_close_cancels_background_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="bg", name="shell", arguments={"command": "sleep 30", "background": True})
        else:
            await asyncio.Event().wait()
            yield TextDoneEvent(text="unreachable")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        async with aclosing(harness.run("Start work")) as events:
            async for event in events:
                if isinstance(event, ToolResultEvent):
                    break
        record = (await harness.shells.list())[0]
        assert record["state"] == "cancelled"
        assert record["reason"] == "owner_cancelled"
        assert not [event async for event in harness.resume_shells()]
    finally:
        await harness.close()


async def test_disabled_background_rejects_stale_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.shell.allow_background = False
    try:
        await harness.initialize()
        with pytest.raises(PermissionError, match="disabled"):
            await harness.tools.shell("touch forbidden", background=True)
        assert not (tmp_path / "forbidden").exists()
        assert json.dumps(await harness.shells.list()) == "[]"
    finally:
        await harness.close()


async def test_instant_completion_admitted_only_after_receipt_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="instant", name="shell", arguments={"command": "printf instant", "background": True})
            yield ToolCallEvent(id="second", name="read_file", arguments={"path": "fixture.txt"})
        else:
            yield TextDoneEvent(text="Done")

    (tmp_path / "fixture.txt").write_text("fixture")
    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        await collect(harness)
        async with asyncio.timeout(3):
            while await harness.shells.has_pending():
                await harness.shells.process_notifications()
                _ = [event async for event in harness.resume_shells()]
                if await harness.shells.has_pending():
                    await harness.shells.wait_changed()
        history = await harness.history()
        notice = next(index for index, message in enumerate(history) if message.name == "ngn_shell_notice")
        receipt = next(index for index, message in enumerate(history) if message.tool_call_id == "instant")
        second = next(index for index, message in enumerate(history) if message.tool_call_id == "second")
        assert receipt < notice and second < notice
    finally:
        await harness.close()


async def test_close_flushes_shutdown_notice_before_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="bg", name="shell", arguments={"command": "sleep 30", "background": True})
        else:
            yield TextDoneEvent(text="Done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    await collect(harness)
    shell_id = str((await harness.shells.list())[0]["shell_id"])
    await harness.close()
    record = await harness.shells.store.get(harness.session_id, shell_id)
    assert json.loads(record.terminal_json)["reason"] == "host_shutdown"
    assert record.notice_message_id and record.wake_state == "suppressed"


async def test_cancel_after_claim_before_dispatch_marks_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "release"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(
                id="bg",
                name="shell",
                arguments={"command": f"while [ ! -f '{gate}' ]; do sleep .01; done", "background": True},
            )
        else:
            yield TextDoneEvent(text="Done")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        await collect(harness)
        shell_id = str((await harness.shells.list())[0]["shell_id"])
        gate.touch()
        async with asyncio.timeout(3):
            while not (await harness.shells.get(shell_id))["output_complete"]:
                await harness.shells.wait_changed()
        message = await harness.shells.next_input()
        assert message is not None and message.persisted_notice_id
        assert (await harness.shells.store.get(harness.session_id, shell_id)).wake_state == "claimed"
        await harness.shells.cancel_owner(harness.session_id)
        assert (await harness.shells.store.get(harness.session_id, shell_id)).wake_state == "uncertain"
        assert not [event async for event in harness.resume_shells()]
        assert len(providers[0].requests) == 2
    finally:
        await harness.close()


async def test_two_harnesses_share_workspace_controller_and_close_only_owned_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="bg", name="shell", arguments={"command": "sleep 30", "background": True})
        else:
            yield TextDoneEvent(text="Done")

    first, _ = setup_harness(tmp_path, monkeypatch, script)
    second, _ = setup_harness(tmp_path, monkeypatch, script)
    first.approval_handler = second.approval_handler = allow
    try:
        await collect(first)
        await collect(second)
        assert first.shells.controller is second.shells.controller
        second_job = str((await second.shells.list())[0]["shell_id"])
        await first.close()
        assert (await second.shells.get(second_job))["state"] == "running"
        assert (await second.shells.stop(second_job))["state"] == "cancelled"
    finally:
        await first.close()
        await second.close()


async def test_new_exclusive_lease_reconciles_only_recorded_dead_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.session.shells import ShellRecord
    from nagents.session.shells import ShellRecordOwner
    from nagents.types import Message as SavedMessage

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize()
        await harness.agent.session.add_message(harness.session_id, SavedMessage(role="user", content="prior work"))
        store = harness.shells.store
        for shell_id, host in (("old", "known-dead"), ("other", "not-this-lease")):
            await store.register(
                ShellRecord(
                    shell_id,
                    ShellRecordOwner(str(tmp_path), harness.session_id, harness.session_id, "old-run", shell_id, host),
                    "sleep 30",
                    "",
                )
            )
        root = harness.agent.session.db_path.parent / "shells"
        root.mkdir(mode=0o700)
        (root / ".harness-hosts.json").write_text('["known-dead"]')
        records = await harness.shells.list()
        assert next(record for record in records if record["shell_id"] == "old")["state"] == "interrupted"
        assert next(record for record in records if record["shell_id"] == "other")["state"] == "running"
        assert not [event async for event in harness.resume_shells()]
        assert not providers[0].requests
    finally:
        await harness.close()


async def test_busy_resume_does_not_claim_pending_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    claims: list[str] = []

    async def claim() -> None:
        claims.append("claimed")

    monkeypatch.setattr(harness.shells, "next_input", claim)
    try:
        with harness.operation("existing run"), pytest.raises(RuntimeError, match="busy"):
            _ = [event async for event in harness.resume_shells()]
        assert not claims
    finally:
        await harness.close()


async def test_idle_terminal_failure_retries_without_host_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    gate = tmp_path / "release"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(
                id="bg",
                name="shell",
                arguments={"command": f"while [ ! -f '{gate}' ]; do sleep .01; done", "background": True},
            )
        else:
            yield TextDoneEvent(text="Done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        await collect(harness)
        original = harness.shells._persist_terminal
        failed = asyncio.Event()
        notified = asyncio.Event()
        calls = 0

        async def flaky(shell: ShellInstance) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                failed.set()
                raise OSError("temporary journal failure")
            await original(shell)

        async def pending(session_id: str) -> None:
            notified.set()

        monkeypatch.setattr(harness.shells, "_persist_terminal", flaky)
        harness.shells.on_work_pending = pending
        gate.touch()
        async with asyncio.timeout(3):
            await failed.wait()
            await notified.wait()
        assert calls == 2
        assert (await harness.shells.list())[0]["output_complete"]
    finally:
        await harness.close()


async def test_closed_bridge_releases_shared_owner_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="bg", name="shell", arguments={"command": "sleep 30", "background": True})
        else:
            yield TextDoneEvent(text="Done")

    first, _ = setup_harness(tmp_path, monkeypatch, script)
    second, _ = setup_harness(tmp_path, monkeypatch, script)
    first.approval_handler = second.approval_handler = allow
    try:
        await collect(first)
        await collect(second)
        shared = second.shells._shared
        assert shared is not None
        await first.close()
        assert first.shells not in shared.owners.values()
        assert second.shells in shared.owners.values()
    finally:
        await first.close()
        await second.close()


async def test_shell_close_failure_still_closes_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def broken() -> None:
        raise OSError("shell cleanup failure")

    monkeypatch.setattr(harness.shells, "close", broken)
    with pytest.raises(OSError, match="shell cleanup"):
        await harness.close()
    assert providers[0].closed


async def test_child_background_is_runtime_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness._is_subagent = True
    try:
        assert not harness.shells._policy().allow_background
        with pytest.raises(PermissionError, match="disabled"):
            await harness.tools.shell("sleep 30", background=True)
    finally:
        await harness.close()


async def test_background_spawn_failure_returns_receipt_and_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="bg", name="shell", arguments={"command": "true", "background": True})
        else:
            yield TextDoneEvent(text="Done")

    async def broken_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        raise OSError("forced spawn failure")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    monkeypatch.setattr(asyncio, "create_subprocess_exec", broken_spawn)
    try:
        async with asyncio.timeout(3):
            events = await collect(harness)
            _ = [event async for event in harness.resume_shells()]
        receipt = next(event for event in events if isinstance(event, ToolResultEvent))
        assert not receipt.error and isinstance(receipt.result, dict) and receipt.result["shell_id"]
        assert not await harness.shells.has_pending()
        record = (await harness.shells.list())[0]
        assert record["state"] == "failed" and record["reason"] == "spawn_failed"
    finally:
        await harness.close()


@pytest.mark.parametrize("failure", ["metadata_cancel", "refresh_error", "claim_commit_cancel"])
async def test_notice_admission_setup_failure_reconciles_committed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    gate = tmp_path / "release"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(
                id="bg",
                name="shell",
                arguments={"command": f"while [ ! -f '{gate}' ]; do sleep .01; done", "background": True},
            )
        else:
            yield TextDoneEvent(text="Done")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    harness.approval_handler = allow
    try:
        await collect(harness)
        shell_id = str((await harness.shells.list())[0]["shell_id"])
        gate.touch()
        async with asyncio.timeout(3):
            while not (await harness.shells.get(shell_id))["output_complete"]:
                await harness.shells.wait_changed()
        await harness.shells.process_notifications()
        entered = asyncio.Event()
        if failure == "metadata_cancel":

            async def metadata(sql: str, parameters: tuple[str, ...] = ()) -> None:
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(harness, "_write_session_metadata", metadata)
        elif failure == "refresh_error":

            def refresh() -> None:
                raise RuntimeError("instruction refresh failed")

            harness.agent.system_prompt = harness._generated_system_prompt
            monkeypatch.setattr(harness, "refresh_instructions", refresh)
        else:
            claim = harness.shells.store.claim_wake

            async def committed_cancel(session_id: str) -> int:
                assert await claim(session_id)
                raise asyncio.CancelledError

            monkeypatch.setattr(harness.shells.store, "claim_wake", committed_cancel)

        async def resume() -> None:
            _ = [event async for event in harness.resume_shells()]

        if failure == "metadata_cancel":
            task = asyncio.create_task(resume())
            async with asyncio.timeout(3):
                await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError if failure == "refresh_error" else asyncio.CancelledError):
                await resume()
        record = await harness.shells.store.get(harness.session_id, shell_id)
        assert record.wake_state == "uncertain"
        assert len(providers[0].requests) == 2
    finally:
        await harness.close()
