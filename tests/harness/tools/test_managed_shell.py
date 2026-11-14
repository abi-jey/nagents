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
        assert isinstance(receipt, dict) and receipt["job_id"]
        job_id = str(receipt["job_id"])
        assert (await harness.shell_jobs.get(job_id))["state"] == "running"
        assert len(providers[0].requests) == 2
        gate.touch()
        async with asyncio.timeout(3):
            while not (await harness.shell_jobs.get(job_id))["output_complete"]:
                await harness.shell_jobs.wait_changed()
        assert (await harness.shell_jobs.get(job_id))["output"] == "finished"
        assert [event async for event in harness.resume_shell_jobs()]
        assert len(providers[0].requests) == 3
        assert not [event async for event in harness.resume_shell_jobs()]
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
        job_id = str((await harness.shell_jobs.list())[0]["job_id"])
        stopped = await harness.shell_jobs.stop(job_id)
        assert stopped["state"] == "cancelled" and stopped["reason"] == "user_stop"
        assert not [event async for event in harness.resume_shell_jobs()]
        assert len(providers[0].requests) == 2
        assert (await harness.shell_jobs.stop(job_id))["reason"] == "user_stop"
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
        record = (await harness.shell_jobs.list())[0]
        assert record["state"] == "cancelled"
        assert record["reason"] == "owner_cancelled"
        assert not [event async for event in harness.resume_shell_jobs()]
    finally:
        await harness.close()


async def test_disabled_background_rejects_stale_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.allow_background_shell = False
    try:
        await harness.initialize()
        with pytest.raises(PermissionError, match="disabled"):
            await harness.tools.shell("touch forbidden", background=True)
        assert not (tmp_path / "forbidden").exists()
        assert json.dumps(await harness.shell_jobs.list()) == "[]"
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
            while await harness.shell_jobs.has_pending():
                await harness.shell_jobs.process_notifications()
                _ = [event async for event in harness.resume_shell_jobs()]
                if await harness.shell_jobs.has_pending():
                    await harness.shell_jobs.wait_changed()
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
    job_id = str((await harness.shell_jobs.list())[0]["job_id"])
    await harness.close()
    record = await harness.shell_jobs.store.get(harness.session_id, job_id)
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
        job_id = str((await harness.shell_jobs.list())[0]["job_id"])
        gate.touch()
        async with asyncio.timeout(3):
            while not (await harness.shell_jobs.get(job_id))["output_complete"]:
                await harness.shell_jobs.wait_changed()
        message = await harness.shell_jobs.next_input()
        assert message is not None and message.persisted_notice_id
        assert (await harness.shell_jobs.store.get(harness.session_id, job_id)).wake_state == "claimed"
        await harness.shell_jobs.cancel_owner(harness.session_id)
        assert (await harness.shell_jobs.store.get(harness.session_id, job_id)).wake_state == "uncertain"
        assert not [event async for event in harness.resume_shell_jobs()]
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
        assert first.shell_jobs.controller is second.shell_jobs.controller
        second_job = str((await second.shell_jobs.list())[0]["job_id"])
        await first.close()
        assert (await second.shell_jobs.get(second_job))["state"] == "running"
        assert (await second.shell_jobs.stop(second_job))["state"] == "cancelled"
    finally:
        await first.close()
        await second.close()


async def test_new_exclusive_lease_reconciles_only_recorded_dead_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.session.shell_jobs import ShellJobOwner
    from nagents.session.shell_jobs import ShellJobRecord
    from nagents.types import Message as SavedMessage

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize()
        await harness.agent.session.add_message(harness.session_id, SavedMessage(role="user", content="prior work"))
        store = harness.shell_jobs.store
        for job_id, host in (("old", "known-dead"), ("other", "not-this-lease")):
            await store.register(
                ShellJobRecord(
                    job_id,
                    ShellJobOwner(str(tmp_path), harness.session_id, harness.session_id, "old-run", job_id, host),
                    "sleep 30",
                    "",
                )
            )
        root = harness.agent.session.db_path.parent / "shell-jobs"
        root.mkdir(mode=0o700)
        (root / ".harness-hosts.json").write_text('["known-dead"]')
        records = await harness.shell_jobs.list()
        assert next(record for record in records if record["job_id"] == "old")["state"] == "interrupted"
        assert next(record for record in records if record["job_id"] == "other")["state"] == "running"
        assert not [event async for event in harness.resume_shell_jobs()]
        assert not providers[0].requests
    finally:
        await harness.close()
