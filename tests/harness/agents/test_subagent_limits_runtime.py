"""Live quota reloads preserve shared accounting and occupied task slots."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.agents.subagent_settings import SubagentLimits
from nagents.harness.types import TaskNotification
from nagents.harness.types import TaskStarted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import assert_balanced
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.runtime.core import Harness
    from nagents.harness.types import ApprovalRequest
    from nagents.types import Message


pytestmark = pytest.mark.usefixtures("workspace_discovery_only")


async def save_limits(harness: Harness, limits: SubagentLimits) -> None:
    current = await harness.tasks.root.settings_host.read("workspace")
    await harness.tasks.root.settings_host.save("workspace", current.revision, limits)


@pytest.mark.asyncio
async def test_live_turn_reload_raises_limits_without_resetting_usage_or_occupied_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_children = asyncio.Event()
    raised_turn = asyncio.Event()
    full_tree_turn = asyncio.Event()
    harness: Harness

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            await release_children.wait()
            yield TextDoneEvent(text="Child completed")
            return
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="parent-a", name="to_agent", arguments={"prompt": "First child"})
            yield ToolCallEvent(id="change-limits", name="raise_limits", arguments={})
        elif len(provider.requests) == 2:
            manager = harness.tasks.root
            assert manager.limits == SubagentLimits(2, 3, 2)
            assert manager._used == 1
            assert manager.usage()["occupied_slots"] == 1
            assert "to_agent" in provider.schemas[-1]
            raised_turn.set()
            yield ToolCallEvent(id="parent-b", name="to_agent", arguments={"prompt": "Second child"})
        elif len(provider.requests) == 3:
            manager = harness.tasks.root
            assert manager._used == 2
            assert manager.usage()["occupied_slots"] == 2
            full_tree_turn.set()
            yield TextDoneEvent(text="Both children were admitted under the raised limits.")
        else:
            yield TextDoneEvent(text="No more delegated work")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def raise_limits() -> str:
        await save_limits(harness, SubagentLimits(2, 3, 2))
        return "Saved limit increase for the next model turn."

    harness.tools.builtins["raise_limits"] = raise_limits
    harness.agent.register_tool(raise_limits)
    try:
        await save_limits(harness, SubagentLimits(1, 1, 2))
        await harness.tasks.refresh_limits()
        run = asyncio.create_task(collect(harness))
        await asyncio.wait_for(raised_turn.wait(), HANG_GUARD)
        await asyncio.wait_for(full_tree_turn.wait(), HANG_GUARD)
        assert harness.tasks._used == 2
        release_children.set()
        events = await asyncio.wait_for(run, HANG_GUARD)
        assert any(isinstance(event, DoneEvent) for event in events)
        assert len(harness.tasks.list()) == 2
        assert len(providers[0].requests) >= 3
        assert harness.tasks._used == 2
        await harness.tasks.refresh_limits()
        assert harness.tasks._used == 2, "ordinary settings refresh must not reset the root execution count"

        events = await asyncio.wait_for(collect(harness, "A new external root turn"), HANG_GUARD)
        assert harness.tasks._used == 0, "only a new external human root turn resets execution usage"
        assert not any(isinstance(event, DoneEvent) and "Both children" in event.final_text for event in events)
        assert len(providers) == 3
    finally:
        release_children.set()
        await harness.close()


@pytest.mark.asyncio
async def test_lowering_limits_preserves_running_children_and_blocks_new_admissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    two_started = asyncio.Event()
    three_started = asyncio.Event()
    started = 0

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        nonlocal started
        if provider.index:
            started += 1
            if started == 2:
                two_started.set()
            elif started == 3:
                three_started.set()
            await release.wait()
            yield TextDoneEvent(text="Child completed")
        else:
            yield TextDoneEvent(text="Root idle")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize()
        await save_limits(harness, SubagentLimits(3, 8, 2))
        await harness.tasks.refresh_limits()
        harness.tasks.begin(harness.session_id)
        first = await harness.tasks.to_agent("First running child")
        second = await harness.tasks.to_agent("Second running child")
        await asyncio.wait_for(two_started.wait(), HANG_GUARD)
        assert harness.tasks._used == 2

        await save_limits(harness, SubagentLimits(1, 1, 2))
        await harness.tasks.refresh_limits()
        lowered = harness.tasks.usage()
        assert lowered["used_executions"] == 2
        assert lowered["remaining_executions"] == 0
        assert lowered["occupied_slots"] == 2
        assert lowered["available_slots"] == 0
        assert all(
            not harness.tasks._workers[session_id].done() for session_id in (first["session_id"], second["session_id"])
        )
        with pytest.raises(ValueError, match="budget exhausted"):
            await harness.tasks.to_agent("Blocked by lower execution limit")
        assert harness.tasks._used == 2

        await save_limits(harness, SubagentLimits(1, 5, 2))
        await harness.tasks.refresh_limits()
        with pytest.raises(ValueError, match="concurrently"):
            await harness.tasks.to_agent("Still blocked by occupied slots")
        assert harness.tasks._used == 2

        await save_limits(harness, SubagentLimits(3, 5, 2))
        await harness.tasks.refresh_limits()
        third = await harness.tasks.to_agent("Allowed after raising the limits")
        await asyncio.wait_for(three_started.wait(), HANG_GUARD)
        assert third["session_id"] in harness.tasks._workers
        assert harness.tasks._used == 3
        assert harness.tasks.usage()["occupied_slots"] == 3
    finally:
        release.set()
        await harness.tasks.end()
        await harness.close()


@pytest.mark.asyncio
async def test_five_by_two_tree_runs_in_waves_with_one_shared_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness: Harness
    default_limits = SubagentLimits()
    proposed_limits = SubagentLimits(3, 15, 2)
    approvals = []
    peak_occupancy = 0
    expected_revision = ""

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        nonlocal expected_revision, peak_occupancy
        occupied = harness.tasks.usage()["occupied_slots"]
        assert isinstance(occupied, int)
        peak_occupancy = max(peak_occupancy, occupied)
        if provider.index == 0:
            request_number = len(provider.requests)
            if request_number == 1:
                yield ToolCallEvent(id="read-limits", name="subagent_configuration", arguments={"scope": "workspace"})
            elif request_number == 2:
                settings_message = next(
                    message
                    for message in reversed(messages)
                    if message.role == "tool" and message.name == "subagent_configuration"
                )
                current = json.loads(str(settings_message.content))
                expected_revision = current["revision"]
                assert current["scope"] == "workspace"
                assert current["limits"] == default_limits.values()
                assert current["usage"]["used_executions"] == 0
                yield ToolCallEvent(
                    id="propose-limits",
                    name="subagent_configure",
                    arguments={
                        "revision": current["revision"],
                        "max_concurrent_subagents": proposed_limits.max_concurrent_subagents,
                        "max_subagent_executions": proposed_limits.max_subagent_executions,
                        "max_subagent_depth": proposed_limits.max_subagent_depth,
                        "scope": "workspace",
                    },
                )
            elif request_number == 3:
                assert harness.tasks.limits == proposed_limits
                assert harness.tasks._used == 0
                assert "to_agent" in provider.schemas[-1]
                yield ToolCallEvent(id="parent-1", name="to_agent", arguments={"prompt": "Parent 1"})
            else:
                parents = [info for info in harness.tasks.list() if info.depth == 1]
                completed = sum(info.status == "completed" for info in parents)
                running = any(info.status == "running" for info in parents)
                assert harness.tasks.limits == proposed_limits
                if running:
                    yield TextDoneEvent(text="Waiting for this branch to complete.")
                elif completed < 5:
                    failed_parent = next((info for info in parents if info.status == "failed"), None)
                    parent_history = (
                        await harness.task_history(failed_parent.session_id) if failed_parent is not None else []
                    )
                    assert harness.tasks._used == completed * 3, [
                        (info.depth, info.status, info.error, info.prompt) for info in harness.tasks.list()
                    ] + [
                        (
                            "failed parent history",
                            [(message.role, message.name, message.content) for message in parent_history],
                        ),
                    ]
                    yield ToolCallEvent(
                        id=f"parent-{completed + 1}",
                        name="to_agent",
                        arguments={"prompt": f"Parent {completed + 1}"},
                    )
                else:
                    yield TextDoneEvent(text="All five branches are complete.")
            return
        if provider.index % 3 == 1 and len(provider.requests) == 1:
            parent = str((provider.index - 1) // 3 + 1)
            for leaf in range(1, 3):
                yield ToolCallEvent(
                    id=f"leaf-{parent}-{leaf}", name="to_agent", arguments={"prompt": f"Leaf {parent}-{leaf}"}
                )
        else:
            yield TextDoneEvent(text="Branch finished.")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        approvals.append(request)
        return True

    harness.approval_handler = approve
    try:
        events = await asyncio.wait_for(collect(harness, "Build five parent branches with two leaves each"), HANG_GUARD)
        all_tasks = harness.tasks.list()
        parents = [info for info in all_tasks if info.depth == 1]
        leaves = [info for info in all_tasks if info.depth == 2]
        assert len(parents) == 5 and len(leaves) == 10
        assert {parent.session_id for parent in parents} == {leaf.parent_session_id for leaf in leaves}
        assert all(sum(leaf.parent_session_id == parent.session_id for leaf in leaves) == 2 for parent in parents)
        assert all(info.status == "completed" and info.activation == 0 and info.followups == 0 for info in all_tasks)
        assert harness.tasks._used == proposed_limits.max_subagent_executions == 15
        assert harness.tasks.usage()["occupied_slots"] == 0
        assert peak_occupancy == proposed_limits.max_concurrent_subagents
        assert len([event for event in events if isinstance(event, TaskStarted)]) == 15
        assert (
            len(
                [
                    event
                    for event in events
                    if isinstance(event, TaskNotification)
                    and not event.recipient_session_id
                    and event.cause == "completion"
                ]
            )
            == 5
        )
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval.tool == "subagent_configure" and approval.mandatory is True
        assert approval.arguments["scope"] == "workspace"
        assert approval.arguments["revision"] == expected_revision
        assert approval.arguments["current"] == default_limits.values()
        assert approval.arguments["proposed"] == proposed_limits.values()
        assert approval.arguments["apply_at"] == "next_model_turn"
        assert "next model turn" in approval.description
        assert not any(isinstance(event, ToolResultEvent) and event.error for event in events)
        assert_balanced(await harness.history())
        for parent in parents:
            history = await harness.task_history(parent.session_id)
            delivered = [
                task
                for notice in notifications(history)
                for task in json.loads(str(notice.content).partition("\n")[2])["sessions"]
            ]
            expected = {leaf.session_id for leaf in leaves if leaf.parent_session_id == parent.session_id}
            assert len(delivered) == 2
            assert {task["session_id"] for task in delivered} == expected
            assert_balanced(history)
        for task in all_tasks:
            assert_balanced(await harness.task_history(task.session_id))
        assert all(provider.closed for provider in providers[1:])
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_limit_proposal_is_denied_to_reviewer_and_child_and_never_grants_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer_workspace = tmp_path / "reviewer"
    reviewer_workspace.mkdir()
    reviewer, _ = setup_harness(reviewer_workspace, monkeypatch, lambda provider, messages: _done(), agent="reviewer")
    try:
        await reviewer.initialize()
        before = await reviewer.tasks.settings_host.read("workspace")
        with pytest.raises(PermissionError, match="root build agent"):
            await reviewer.tasks.subagent_configure(before.revision, 4, 20, 3, scope="workspace")
        assert await reviewer.tasks.settings_host.read("workspace") == before
    finally:
        await reviewer.close()

    build_workspace = tmp_path / "build"
    build_workspace.mkdir()
    harness, _ = setup_harness(build_workspace, monkeypatch, lambda provider, messages: _done())
    try:
        await harness.initialize()
        before = await harness.tasks.settings_host.read("workspace")
        child = harness.tasks._create_child("assistant")
        try:
            child_snapshot = await child.tasks.settings_host.read("workspace")
            with pytest.raises(PermissionError, match="root build agent"):
                await child.tasks.subagent_configure(child_snapshot.revision, 4, 20, 3, scope="workspace")
        finally:
            await child.close()

        assert await harness.tasks.settings_host.read("workspace") == before
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_denied_limit_proposal_does_not_write_and_approved_quota_does_not_enable_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, _ = setup_harness(tmp_path, monkeypatch, lambda provider, messages: _done())
    approvals: list[ApprovalRequest] = []

    async def deny(request: ApprovalRequest) -> bool:
        approvals.append(request)
        return False

    harness.approval_handler = deny
    try:
        await harness.initialize()
        before = await harness.tasks.settings_host.read("workspace")
        with pytest.raises(PermissionError, match="Approval denied"):
            await harness.tasks.subagent_configure(before.revision, 4, 20, 3, scope="workspace")
        assert len(approvals) == 1 and approvals[0].mandatory is True
        assert await harness.tasks.settings_host.read("workspace") == before

        async def allow(request: ApprovalRequest) -> bool:
            approvals.append(request)
            return True

        harness.approval_handler = allow
        harness.allow_subagents = False
        saved = await harness.tasks.subagent_configure(before.revision, 4, 20, 3, scope="workspace")
        assert saved["status"] == "saved"
        assert harness.allow_subagents is False and harness.can_delegate is False
        assert len(approvals) == 2 and all(request.mandatory for request in approvals)
    finally:
        await harness.close()


async def _done() -> AsyncIterator[Event]:
    yield TextDoneEvent(text="Unused provider script")
