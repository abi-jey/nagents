"""Owning-chat decisions target their exact run independently of UI selection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.channels import ChannelApproval
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelMessage
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.web.catalog import Connection
from tests.support.hang_guard import HANG_GUARD
from tests.web.test_web_channel_approvals import ApprovalChannel
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.channels import ChannelSend
    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


class RecordingApprovalChannel(ApprovalChannel):
    async def send(self, message: ChannelSend) -> ChannelDelivery:
        return ChannelDelivery(("notice",))


@pytest.mark.parametrize("allow_b", [True, False])
def test_parallel_chat_decisions_keep_exact_owner_run_and_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_b: bool
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role == "tool":
                yield TextDoneEvent(text=f"Finished {messages[-1].tool_call_id}")
            else:
                room = "room-a" if "request A" in str(messages[-1].content) else "room-b"
                yield ToolCallEvent(id=f"inspect-{room}", name="channel_list", arguments={})

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, _):
            host = state.channels
            host.catalog.connections["fixture"] = Connection(
                plugin="fixture",
                enabled=True,
                auto_reply=False,
                chat_approvals=True,
                config={},
                secrets={},
                main_session_id=state.selected_session_id,
            )
            channel = RecordingApprovalChannel()
            await host.open("fixture", channel)
            host.update_tools()
            for room, prompt in (("room-a", "request A"), ("room-b", "request B")):
                await channel.queue.put(ChannelMessage(f"input-{room}", room, "participant", prompt))
            await until(
                lambda: len(state.executions.runs) == 2 and all(run.pending for run in state.executions.runs.values())
            )
            by_room = {run.source["conversation_id"]: run for run in state.executions.runs.values()}
            a, b = by_room["room-a"], by_room["room-b"]
            pending_a, pending_b = a.pending, b.pending
            assert pending_a is not None and pending_b is not None
            assert pending_a.in_chat and pending_b.in_chat and not state.bus.subscribers
            assert a.harness is not b.harness and len(providers) == 2
            selected = await client.post("/api/sessions/resume", headers=headers, json={"session_id": a.session_id})
            assert selected.status_code == 200 and state.active is a
            decision = ChannelApproval(
                conversation_id="room-b",
                session_id=b.session_id,
                run_id=b.id,
                call_id=pending_b.call_id,
                allow=allow_b,
            )
            for invalid in (
                replace(decision, conversation_id="room-a"),
                replace(decision, session_id=a.session_id),
                replace(decision, run_id=a.id),
                replace(decision, call_id=pending_a.call_id),
                replace(decision, run_id="a-previous-run"),
            ):
                assert not await host.resolve_approval("fixture", invalid)
                assert not pending_a.answer.done() and not pending_b.answer.done()

            async def callback(value: ChannelApproval) -> None:
                await channel.queue.put(
                    ChannelMessage(
                        f"callback-{value.run_id}",
                        value.conversation_id,
                        "participant",
                        metadata={
                            "callback": {
                                "conversation_id": value.conversation_id,
                                "session_id": value.session_id,
                                "run_id": value.run_id,
                                "call_id": value.call_id,
                                "allow": value.allow,
                            }
                        },
                    )
                )

            # The genuine connector source task has no producer-local scope;
            # selecting A must not route B's decision to the selected run.
            await callback(decision)
            async with asyncio.timeout(HANG_GUARD):
                assert await asyncio.shield(pending_b.answer) is allow_b
            await until(lambda: state.run_for(b.session_id) is None)
            assert state.run_for(a.session_id) is a and not pending_a.answer.done()
            assert not await host.resolve_approval("fixture", decision)
            assert not pending_a.answer.done()

            await callback(
                ChannelApproval(
                    conversation_id="room-a",
                    session_id=a.session_id,
                    run_id=a.id,
                    call_id=pending_a.call_id,
                    allow=True,
                )
            )
            await until(lambda: not state.executions.runs and not host.work_tasks)
            for run, call_id in ((a, pending_a.call_id), (b, pending_b.call_id)):
                history = await state.history.snapshot(run.session_id)
                assert len([row for row in history if row["role"] == "user"]) == 1
                results = [row for row in history if row["role"] == "tool"]
                assert len(results) == 1 and results[0]["tool_call_id"] == call_id
            assert not await host.store.has_pending()

    asyncio.run(scenario())
