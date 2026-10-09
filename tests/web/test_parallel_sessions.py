"""Independent chat owners, voice detachment and exact run-scoped decisions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.live.delegation import ClientDelegationRequest
from nagents.web.executions import MAX_PARALLEL_RUNS
from nagents.web.live_bridge import MainAgentBridge
from tests.support.hang_guard import HANG_GUARD
from tests.web.test_web_subscription_lifecycle import Peer
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.live.delegation import LiveAppendKind
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


def test_switching_voice_chat_keeps_original_work_and_other_root_progresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        sent: list[str] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if "work A" in str(messages[-1].content):
                entered.set()
                await release.wait()
                yield TextDoneEvent(text="A finished in its original chat")
            else:
                assert messages[-1].content == "work B"
                assert not any("work A" in str(message.content) for message in messages)
                yield TextDoneEvent(text="B finished independently")

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            sent.append(content)
            return f"session.{kind}.append"

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, providers, _):
            first = state.selected_session_id
            second = await state.harness.new_session()
            bridge = MainAgentBridge(state, first)
            bridge.attach(append)
            voice_open = True
            live = app.state.live
            monkeypatch.setattr(
                type(live), "active_session_id", property(lambda _: "voice-fixture" if voice_open else "")
            )

            async def close(identifier: str) -> dict[str, object]:
                nonlocal voice_open
                assert identifier == "voice-fixture"
                await bridge.close()
                voice_open = False
                return {"session_id": identifier, "status": "closed"}

            monkeypatch.setattr(live, "close", close)
            try:
                await bridge.handle_request(ClientDelegationRequest("a", "work A"))
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                original = state.run_for(first)
                assert original is not None
                selected = await client.post("/api/sessions/resume", headers=headers, json={"session_id": second})
                assert selected.status_code == 200 and selected.json()["active_run"] is None
                assert not voice_open and bridge.updates.detached
                before = list(sent)
                assert state.run_for(first) is original and not original.task.cancelling()
                response = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": second,
                        "message_id": "22222222-2222-2222-2222-222222222222",
                        "prompt": "work B",
                    },
                )
                assert response.status_code == 200
                await until(lambda: len(providers) == 2 and state.run_for(second) is None)
                rows = await state.history.snapshot(second)
                assert any(row["content"] == "B finished independently" for row in rows)
                assert state.run_for(first) is original and not original.task.done()
                assert providers[1].closed
                release.set()
                await until(lambda: state.run_for(first) is None)
                first_rows = await state.history.snapshot(first)
                assert any(row["content"] == "A finished in its original chat" for row in first_rows)
                assert not any("B finished" in str(row["content"]) for row in first_rows)
                assert sent == before
                assert state.selected_session_id == second
            finally:
                release.set()
                await bridge.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_pending_approval_survives_navigation_and_other_root_has_independent_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            if prompt == "A needs approval":
                yield ToolCallEvent(id="write-a", name="write", arguments={"path": "approved.txt", "content": "A"})
            elif prompt == "B keeps working":
                entered.set()
                await release.wait()
                yield TextDoneEvent(text="B done")
            else:
                yield TextDoneEvent(text="A done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            first = state.selected_session_id
            second = await state.harness.new_session()
            peer = Peer()
            connection = await peer.start(state.bus, state.snapshot, state.disconnected)
            peer.subscribe(first)
            await peer.frame()
            try:
                for root, text, identifier in (
                    (first, "A needs approval", "11111111-1111-1111-1111-111111111111"),
                    (second, "B keeps working", "22222222-2222-2222-2222-222222222222"),
                ):
                    response = await client.post(
                        "/api/messages",
                        headers=headers,
                        json={"session_id": root, "message_id": identifier, "prompt": text},
                    )
                    assert response.status_code == 200
                await until(lambda: state.run_for(first) is not None and state.run_for(first).pending is not None)  # type: ignore[union-attr]
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                a, b = state.run_for(first), state.run_for(second)
                assert a is not None and b is not None and a.pending is not None
                pending = a.pending
                peer.subscribe(second)
                await until(lambda: state.bus.listening(second) and not state.bus.listening(first))
                assert not pending.answer.done()
                assert a.harness is not b.harness and a.harness is not None and b.harness is not None
                decision = {"run_id": b.id, "approval_id": pending.id, "call_id": pending.call_id, "decision": "allow"}
                assert (await client.post("/api/approval", headers=headers, json=decision)).status_code == 409
                assert not (tmp_path / "approved.txt").exists()
                assert (await client.post("/api/cancel", headers=headers, json={"run_id": b.id})).status_code == 200
                await until(lambda: state.run_for(second) is None)
                assert state.run_for(first) is a and not pending.answer.done()
                peer.subscribe(first)
                await until(lambda: state.bus.listening(first))
                decision["run_id"] = a.id
                assert (await client.post("/api/approval", headers=headers, json=decision)).status_code == 200
                await until(lambda: state.run_for(first) is None)
                assert (tmp_path / "approved.txt").read_text() == "A"
                assert not any(row["content"] == "B done" for row in await state.history.snapshot(second))
            finally:
                release.set()
                peer.disconnect()
                await connection

    asyncio.run(scenario())


def test_parallel_limit_keeps_extra_root_durable_and_releases_one_slot_without_stopping_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered = {str(index): asyncio.Event() for index in range(MAX_PARALLEL_RUNS + 1)}
        release = {key: asyncio.Event() for key in entered}

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            key = str(messages[-1].content)
            entered[key].set()
            await release[key].wait()
            yield TextDoneEvent(text=f"Answer {key}")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, _):
            roots = [state.selected_session_id]
            for _ in range(MAX_PARALLEL_RUNS):
                roots.append(await state.harness.new_session())
            try:
                for index, root in enumerate(roots):
                    response = await client.post(
                        "/api/messages",
                        headers=headers,
                        json={
                            "session_id": root,
                            "message_id": f"00000000-0000-0000-0000-{index:012d}",
                            "prompt": str(index),
                        },
                    )
                    assert response.status_code == 200
                async with asyncio.timeout(HANG_GUARD):
                    await asyncio.gather(*(entered[str(index)].wait() for index in range(MAX_PARALLEL_RUNS)))
                assert len(state.executions.runs) == MAX_PARALLEL_RUNS
                assert not entered[str(MAX_PARALLEL_RUNS)].is_set()
                status = await state.channels.store._transaction(
                    lambda db: db.execute(
                        "SELECT status FROM ngn_web_inbox WHERE session_id = ?", (roots[-1],)
                    ).fetchone()[0]
                )
                assert status == "queued"
                snapshot = (await client.get("/api/bootstrap")).json()
                assert len(snapshot["active_runs"]) == MAX_PARALLEL_RUNS
                assert all(set(item) == {"id", "session_id", "status"} for item in snapshot["active_runs"])
                release["0"].set()
                async with asyncio.timeout(HANG_GUARD):
                    await entered[str(MAX_PARALLEL_RUNS)].wait()
                assert all(state.run_for(root) is not None for root in roots[1:])
                assert len({id(run.harness) for run in state.executions.runs.values()}) == MAX_PARALLEL_RUNS
            finally:
                for event in release.values():
                    event.set()
                await until(lambda: not state.executions.runs and not state.channels.work_tasks)
                assert all(provider.closed for provider in providers[1:])
                assert not state.channels.runtime_instructions

    asyncio.run(scenario())
