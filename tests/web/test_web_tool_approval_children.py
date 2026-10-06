"""Saved approvals follow the actual child definition and activation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.web.test_web_tool_approvals import decide
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.service import Run
    from tests.support.providers import FakeProvider


async def parent_replacement(path: str, content: str) -> str:
    """This root-only replacement must not borrow the child's builtin grant."""
    return path + content


@pytest.mark.requires_posix
@pytest.mark.parametrize("changed", ["activation", "depth"])
def test_child_permission_rejects_stale_identity_and_ignores_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate-one", name="delegate", arguments={"prompt": "Write the fixture"})
            elif provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="child-write", name="write", arguments={"path": "child.txt", "content": "child fixture"}
                )
            else:
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            stream = LiveStream(app, headers, state.harness.session_id, "Delegate fixture work")
            pending = await stream.event("approval")
            assert pending["task_id"] and pending["depth"] == 1
            assert pending["allow_tool"] is True and pending["allow_tool_persistent"] is True
            child = state.harness.tasks.root._children[str(pending["task_id"])]
            original_activation, original_depth = child._activation, child.subagent_depth
            if changed == "activation":
                child._activation += 1
            else:
                child.subagent_depth += 1
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
            assert state.tool_approvals.snapshot() == []
            assert not (tmp_path / "child.txt").exists()

            child._activation, child.subagent_depth = original_activation, original_depth
            state.harness.agent.register_tool(parent_replacement, name="write")
            child_binding = state.tool_approvals.binding(child, "write")
            parent_binding = state.tool_approvals.binding(state.harness, "write")
            assert child_binding is not None and parent_binding is not None and child_binding != parent_binding
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 200
            assert (await stream.event("run_finished"))["status"] == "completed"
            await asyncio.wait_for(stream.task, HANG_GUARD)
            assert (tmp_path / "child.txt").read_text() == "child fixture"
            assert state.tool_approvals.allowed(child_binding)
            assert not state.tool_approvals.allowed(parent_binding)

    asyncio.run(scenario())


@pytest.mark.requires_posix
@pytest.mark.parametrize("owner", ["child", "activation"])
def test_cancelling_child_cannot_save_permission_before_answer_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate-one", name="delegate", arguments={"prompt": "Write the fixture"})
            elif provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="child-write", name="write", arguments={"path": "cancelled.txt", "content": "must not write"}
                )
            else:
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            sent, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_send = state.send

            async def gated_send(run: Run, record: dict[str, object]) -> None:
                await original_send(run, record)
                if record["event"] == "approval":
                    sent.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                        raise

            with patch.object(state, "send", gated_send):
                stream = LiveStream(app, headers, state.harness.session_id, "Delegate fixture work")
                pending = await stream.event("approval")
                await asyncio.wait_for(sent.wait(), HANG_GUARD)
                child = state.harness.tasks.root._children[str(pending["task_id"])]
                worker = (
                    child._worker if owner == "child" else state.harness.tasks.root._workers[str(pending["task_id"])]
                )
                assert worker is not None
                worker.cancel()
                try:
                    await asyncio.wait_for(cancelled.wait(), HANG_GUARD)
                    active = state.active
                    assert active is not None and not active.task.cancelling()
                    assert active.pending is not None and not active.pending.answer.done()
                    assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
                    assert state.tool_approvals.snapshot() == []
                    assert not (tmp_path / "cancelled.txt").exists()
                finally:
                    release.set()
                await stream.event("run_finished")
                await asyncio.wait_for(stream.task, HANG_GUARD)
                assert not (tmp_path / "cancelled.txt").exists()

    asyncio.run(scenario())
