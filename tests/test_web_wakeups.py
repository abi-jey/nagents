"""Process-local scheduling, real Harness execution, and subscriber-free activity."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextlib import nullcontext
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.harness import runtime
from nagents.harness.config import HarnessConfig
from nagents.web import wakeups
from nagents.web.app import WebState
from nagents.web.wakeups import Chain
from nagents.web.wakeups import Wakeup
from nagents.web.wakeups import Wakeups
from tests.test_subagents import FakeProvider
from tests.test_web import LiveStream
from tests.test_web import client_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Coroutine
    from pathlib import Path

    import httpx
    from fastapi import FastAPI

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.app import Run
    from tests.test_subagents import Script


@dataclass
class Clock:
    now: float = 100

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def scheduled_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Script, *, start: bool = False
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, dict[str, str], WebState, list[FakeProvider], Clock]]:
    providers: list[FakeProvider] = []
    states: list[WebState] = []
    clock = Clock()

    def provider(config: HarnessConfig) -> FakeProvider:
        instance = FakeProvider(config, len(providers), script)
        providers.append(instance)
        return instance

    def state(harness: Harness) -> WebState:
        instance = WebState(harness)
        instance.wakeups.clock = clock
        states.append(instance)
        harness.agent.compactor = None
        return instance

    monkeypatch.setattr(runtime, "HarnessProvider", provider)
    config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key", model="fake-model")
    with (
        patch("nagents.web.app.WebState", side_effect=state),
        nullcontext() if start else patch.object(Wakeups, "start"),
    ):
        async with client_app(tmp_path, controlled=False, config=config) as (app, client, headers, _):
            yield app, client, headers, states[0], providers, clock
    assert all(provider.closed for provider in providers)
    assert states[0].active is None and not states[0].wakeups.pending


def test_scheduler_limits_monotonic_claims_and_chain_cancellation() -> None:
    async def check() -> None:
        clock = Clock()
        ready = False
        fired: list[Wakeup] = []

        async def execute(wakeup: Wakeup) -> None:
            fired.append(wakeup)

        scheduler = Wakeups(lambda: ready, execute, clock=clock)
        chain = Chain()
        for delay in (True, 0, -1, float("nan"), float("inf"), 7 * 86400 + 1):
            with pytest.raises(ValueError, match="delay"):
                scheduler.schedule("ngn-first", "human", chain, "", delay, "reason")
        for reason in ("", " ", "x" * 2001):
            with pytest.raises(ValueError, match="reason"):
                scheduler.schedule("ngn-first", "human", chain, "", 1, reason)
        assert not scheduler.pending and scheduler.cursor == 0
        ack = scheduler.schedule("ngn-first", "human", chain, "", 10, "reason")
        assert ack["status"] == "scheduled" and ack["due_at"].endswith("+00:00")
        clock.now += 9.999
        ready = True
        await scheduler.tick()
        assert not fired
        ready = False
        clock.now += 10
        await scheduler.tick()
        assert not fired  # Due is not enough: the Harness must be idle.
        ready = True
        await asyncio.gather(scheduler.tick(), scheduler.tick())
        assert len(fired) == 1 and not scheduler.pending
        for _ in range(wakeups.MAX_ACTIVATIONS - 1):
            scheduler.schedule("ngn-first", "automatic", chain, "", 1, "again")
            clock.now += 1
            await scheduler.tick()
        with pytest.raises(ValueError, match="budget"):
            scheduler.schedule("ngn-first", "automatic", chain, "", 1, "again")
        second = Chain()
        for _ in range(wakeups.MAX_PENDING):
            scheduler.schedule("ngn-second", "human2", second, "", 1, "later")
        with pytest.raises(ValueError, match="32"):
            scheduler.schedule("ngn-second", "human2", second, "", 1, "later")
        scheduler.cancel(chain)
        assert len(scheduler.pending) == wakeups.MAX_PENDING
        scheduler.cancel(second)
        assert not scheduler.pending
        await scheduler.close()
        with pytest.raises(RuntimeError, match="active"):
            scheduler.schedule("ngn-first", "human", Chain(), "", 1, "closed")

    asyncio.run(check())


def test_service_owns_timer_loop_and_bounds_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        clock = Clock()
        executed = asyncio.Event()

        async def execute(wakeup: Wakeup) -> None:
            executed.set()

        scheduler = Wakeups(lambda: True, execute, clock=clock)
        scheduler.schedule("ngn-first", "human", Chain(), "", 10, "later")
        scheduler.start()
        clock.now += 10
        scheduler.changed.set()
        await asyncio.wait_for(executed.wait(), 5)
        scheduler.schedule("ngn-first", "human", Chain(), "", 100, "cancel on shutdown")
        await scheduler.close()
        assert all(task.done() for task in scheduler._tasks)
        assert not scheduler.pending
        monkeypatch.setattr(wakeups, "MAX_EVENTS", 2)
        for index in range(4):
            scheduler.publish({"event": "notice", "session_id": "ngn-first", "text": str(index)})
        result = scheduler.activity("ngn-first", 0, "")
        assert result["truncated"] is True
        assert isinstance(result["events"], list) and len(result["events"]) == 2
        assert scheduler.activity("ngn-other", 0, "")["events"] == []
        assert scheduler.activity("ngn-first", scheduler.cursor, "")["events"] == []
        scheduler.publish({"session_id": "ngn-first", "text": "x" * (wakeups.MAX_ACTIVITY_BYTES + 1)})
        assert not scheduler._events and scheduler._bytes == 0

    asyncio.run(check())


@pytest.mark.requires_posix
def test_root_ack_finishes_stream_then_wakes_once_without_subscriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "check later"})
            else:
                for _ in range(100):
                    yield TextChunkEvent(chunk="text")
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            session_id = state.harness.session_id
            response = await client.post(
                "/api/run", json={"session_id": session_id, "prompt": "start"}, headers=headers
            )
            events = [json.loads(line) for line in response.text.splitlines()]
            ack = next(event["result"] for event in events if event["event"] == "tool_result")
            assert ack["status"] == "scheduled" and ack["task_id"] == ""
            assert all(isinstance(value, str) for value in ack.values())
            assert events[-1]["status"] == "completed" and state.active is None
            assert len(providers[0].requests) == 2 and not state.harness.tasks.list()
            before = (await client.get(f"/api/activity/{session_id}/0", headers=headers)).json()
            assert [event["event"] for event in before["events"]] == ["wakeup"]
            assert before["pending_wakeups"][0]["wakeup_id"] == ack["wakeup_id"]
            await state.wakeups.tick()
            assert len(providers[0].requests) == 2
            clock.now += 10
            with state.idle():
                await state.wakeups.tick()
                assert len(providers[0].requests) == 2
            await state.wakeups.tick()
            await state.wakeups.tick()
            assert len(providers[0].requests) == 3
            activity = (await client.get(f"/api/activity/{session_id}/{before['cursor']}", headers=headers)).json()
            background = activity["events"]
            assert background[0]["event"] == "run_started"
            assert background[-1]["event"] == "run_finished" and background[-1]["status"] == "completed"
            assert len(background) > 64  # No subscriber is draining an HTTP queue.
            assert len({event["run_id"] for event in background}) == 1
            assert background[0]["run_id"] != events[0]["run_id"]
            assert all(event["session_id"] == session_id and event["schema_version"] == 1 for event in background)
            assert not activity["pending_wakeups"] and not activity["active_run_id"]
            snapshot = (await client.get("/api/sessions", headers=headers)).json()
            assert snapshot["activity_cursor"] == activity["cursor"]
            assert (
                await client.get(f"/api/activity/{session_id}/{snapshot['activity_cursor']}", headers=headers)
            ).json()["events"] == []

    asyncio.run(check())


@pytest.mark.requires_posix
def test_activity_security_paths_and_workspace_membership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, _):
            session_id = state.harness.session_id
            path = f"/api/activity/{session_id}/0"
            for bad in (
                {},
                {**headers, "X-Ngn-Token": "wrong"},
                {**headers, "Origin": "http://evil.test"},
                {**headers, "Host": "evil.test"},
            ):
                assert (await client.get(path, headers=bad)).status_code == 403
            assert (await client.get(path + "?token=secret", headers=headers)).status_code == 400
            for suffix in ("-1", "x", str(2**53), "1.5"):
                assert (await client.get(f"/api/activity/{session_id}/{suffix}", headers=headers)).status_code == 422
            for bad_id in ("invalid", "ngn-" + "x" * 80):
                assert (await client.get(f"/api/activity/{bad_id}/0", headers=headers)).status_code == 422
            await state.harness.agent.session.get_or_create_session("ngn-private-child", "harness")
            for hidden in ("ngn-private-child", "ngn-unknown"):
                assert (await client.get(f"/api/activity/{hidden}/0", headers=headers)).status_code == 404
            response = await client.get(path, headers=headers)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert not providers[0].requests

    asyncio.run(check())


@pytest.mark.requires_posix
def test_snapshot_retained_tasks_are_selected_root_and_process_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate", name="delegate", arguments={"prompt": "retained child"})
            else:
                yield TextDoneEvent(text="recorded result")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, _):
            first = state.harness.session_id
            assert (await client.get("/api/sessions", headers=headers)).json()["retained_tasks"] == []
            await client.post("/api/run", json={"session_id": first, "prompt": "start"}, headers=headers)
            snapshot = (await client.get("/api/sessions", headers=headers)).json()
            retained = snapshot["retained_tasks"]
            assert retained == [asdict(info) for info in state.harness.tasks.list()]
            assert len(retained) == 1 and retained[0]["session_id"] == first
            assert retained[0]["status"] == "completed" and retained[0]["result"] == "recorded result"
            assert retained[0]["activation"] == 0 and retained[0]["trigger"] == "delegation"
            assert any(
                message["content"].startswith("BACKGROUND TASK NOTIFICATION:") for message in snapshot["history"]
            )
            requests = [len(provider.requests) for provider in providers]
            new = (await client.post("/api/sessions/new", json={}, headers=headers)).json()
            assert new["session_id"] != first and new["retained_tasks"] == [] and new["history"] == []
            assert (await client.get("/api/sessions", headers=headers)).json()["retained_tasks"] == []
            resumed = (await client.post("/api/sessions/resume", json={"session_id": first}, headers=headers)).json()
            assert resumed["retained_tasks"] == retained and resumed["history"] == snapshot["history"]
            assert [len(provider.requests) for provider in providers] == requests

        # Persisted history is unchanged, but a new process has no retained task handles.
        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, _, providers, _):
            resumed = (await client.post("/api/sessions/resume", json={"session_id": first}, headers=headers)).json()
            assert resumed["session_id"] == first and resumed["retained_tasks"] == []
            assert resumed["history"] == snapshot["history"]
            assert not providers[0].requests

    asyncio.run(check())


@pytest.mark.requires_posix
def test_nested_wakeup_reactivates_immediate_parent_before_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index < 2 and len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate", name="delegate", arguments={"prompt": "child"})
            elif provider.index == 2 and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="timer", name="schedule_wakeup", arguments={"seconds": 10, "reason": "nested wake"}
                )
            else:
                yield TextDoneEvent(text=f"result from {provider.index}")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            session_id = state.harness.session_id
            response = await client.post(
                "/api/run", json={"session_id": session_id, "prompt": "start"}, headers=headers
            )
            assert json.loads(response.text.splitlines()[-1])["status"] == "completed"
            infos = sorted(state.harness.tasks.list(), key=lambda item: item.depth)
            assert len(infos) == 2
            parent, child = infos
            assert next(iter(state.wakeups.pending.values())).task_id == child.id
            cursor = state.wakeups.cursor
            used = state.harness.tasks._used
            second = (await client.post("/api/sessions/new", json={}, headers=headers)).json()["session_id"]
            clock.now += 10
            await state.wakeups.tick()
            snapshot = (await client.get("/api/sessions", headers=headers)).json()
            assert snapshot["session_id"] == second and snapshot["retained_tasks"] == []
            resumed = (
                await client.post("/api/sessions/resume", json={"session_id": session_id}, headers=headers)
            ).json()
            assert resumed["retained_tasks"] == [asdict(info) for info in state.harness.tasks.list()]
            assert all(info["activation"] == 1 for info in resumed["retained_tasks"])
            events = (await client.get(f"/api/activity/{session_id}/{cursor}", headers=headers)).json()["events"]
            assert events[-1]["status"] == "completed"
            starts = [event for event in events if event["event"] == "task_started"]
            assert [(event["task_id"], event["trigger"]) for event in starts] == [
                (child.id, "wakeup"),
                (parent.id, "notification"),
            ]
            assert all(event["activation"] == 1 and event["followup"] == 0 for event in starts)
            notices = [event for event in events if event["event"] == "task_notification"]
            assert [(event["source_task_id"], event["recipient_task_id"], event["cause"]) for event in notices] == [
                (child.id, child.id, "wakeup"),
                (child.id, parent.id, "completion"),
                (parent.id, "", "completion"),
            ]
            assert len({event["notification_id"] for event in notices}) == 3
            assert state.harness.tasks._used == used + 2
            assert all(info.followups == 0 for info in state.harness.tasks.list())
            assert len(providers) == 5
            assert "result from 3" in str(providers[4].requests[-1])
            assert "result from 3" not in str(providers[0].requests[-1])

    asyncio.run(check())


@pytest.mark.requires_posix
def test_reused_call_ids_keep_grandchild_approval_and_wakeup_activation_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        (tmp_path / "readable.txt").write_text("Root-only read result")

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            first = len(provider.requests) == 1
            if provider.index == 0 and first:
                yield ToolCallEvent(id="shared-call", name="read_file", arguments={"path": "readable.txt"})
                yield ToolCallEvent(id="delegate-parent", name="delegate", arguments={"prompt": "parent"})
            elif provider.index == 1 and first:
                yield ToolCallEvent(id="delegate-child", name="delegate", arguments={"prompt": "grandchild"})
            elif provider.index in {2, 3} and first:
                yield ToolCallEvent(
                    id="shared-call", name="write", arguments={"path": "denied.txt", "content": "Must not be written"}
                )
                if provider.index == 2:
                    yield ToolCallEvent(
                        id="timer", name="schedule_wakeup", arguments={"seconds": 10, "reason": "child follow-up"}
                    )
            else:
                yield TextDoneEvent(text=f"result from {provider.index}")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, clock):
            session_id = state.harness.session_id
            stream = LiveStream(app, headers, session_id, "start")
            foreground: list[dict[str, object]] = []
            async with asyncio.timeout(5):
                while True:
                    approval = await stream.output.get()
                    foreground.append(approval)
                    if approval["event"] == "approval":
                        break
            child = next(info for info in state.harness.tasks.list() if info.depth == 2)
            assert approval["task_id"] == child.id and approval["activation"] == 0
            assert approval["id"] == "shared-call" and approval["tool"] == "write"
            decision = {
                "run_id": approval["run_id"],
                "approval_id": approval["approval_id"],
                "call_id": approval["id"],
                "decision": "deny",
            }
            assert (await client.post("/api/approval", json=decision, headers=headers)).status_code == 200
            assert (await client.post("/api/approval", json=decision, headers=headers)).status_code == 409
            await asyncio.wait_for(stream.task, 5)
            while not stream.output.empty():
                foreground.append(stream.output.get_nowait())
            reads = [event for event in foreground if event["event"] == "tool_result" and event["id"] == "shared-call"]
            assert len(reads) == 1 and reads[0]["name"] == "read_file" and not reads[0]["error"]
            assert "Root-only read result" in str(reads[0]["result"])
            assert reads[0].get("task_id", "") == ""
            closed = next(event for event in foreground if event["event"] == "approval_closed")
            assert (closed["task_id"], closed["activation"], closed["call_id"]) == (child.id, 0, "shared-call")
            assert foreground[-1]["status"] == "completed"
            assert next(iter(state.wakeups.pending.values())).task_id == child.id
            cursor = state.wakeups.cursor
            clock.now += 10
            await state.wakeups.tick()
            activity = (await client.get(f"/api/activity/{session_id}/{cursor}", headers=headers)).json()
            notices = [
                event for event in activity["events"] if event["event"] == "notice" and "Unattended" in event["text"]
            ]
            assert len(notices) == 1
            assert (notices[0]["task_id"], notices[0]["activation"], notices[0]["call_id"]) == (
                child.id,
                1,
                "shared-call",
            )
            assert notices[0]["run_id"] != approval["run_id"] and notices[0]["tool"] == "write"
            completions = [event for event in activity["events"] if event["event"] == "task_completed"]
            assert any(
                event["task_id"] == child.id and event["activation"] == 1 and event["trigger"] == "wakeup"
                for event in completions
            )
            assert not any(event["event"] == "approval" for event in activity["events"])
            assert activity["events"][-1]["status"] == "completed"
            assert not (tmp_path / "denied.txt").exists()

    asyncio.run(check())


@pytest.mark.requires_posix
def test_large_real_tool_results_are_byte_bounded_without_subscribers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        output_size = 256 * 1024
        (tmp_path / "large.txt").write_text("x" * output_size)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="timer", name="schedule_wakeup", arguments={"seconds": 10, "reason": "read later"}
                )
            elif len(provider.requests) == 3:
                for index in range(12):
                    yield ToolCallEvent(id=f"read-{index}", name="read_file", arguments={"path": "large.txt"})
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            state.harness.config.max_output = output_size
            session_id = state.harness.session_id
            await client.post("/api/run", json={"session_id": session_id, "prompt": "start"}, headers=headers)
            clock.now += 10
            await asyncio.wait_for(state.wakeups.tick(), 5)
            assert len(providers[0].requests) == 4 and state.active is None
            activity = (await client.get(f"/api/activity/{session_id}/0", headers=headers)).json()
            events = activity["events"]
            retained = [event for event in events if event["event"] == "tool_result"]
            assert 0 < len(retained) < 12 and retained[-1]["id"] == "read-11"
            assert all(not event["error"] and len(event["result"]["content"]) == output_size for event in retained)
            assert activity["truncated"] is True and events[-1]["event"] == "run_finished"
            assert events[-1]["status"] == "completed" and not activity["active_run_id"]
            assert state.wakeups._bytes == sum(len(json.dumps(event, ensure_ascii=True)) for event in events)
            assert state.wakeups._bytes <= wakeups.MAX_ACTIVITY_BYTES
            assert len(events) < wakeups.MAX_EVENTS  # Eviction was byte-driven, not count-driven.
            cursor = activity["cursor"]
            assert (await client.get(f"/api/activity/{session_id}/{cursor}", headers=headers)).json()["events"] == []
            await state.wakeups.tick()
            assert len(providers[0].requests) == 4

    asyncio.run(check())


@pytest.mark.requires_posix
@pytest.mark.parametrize("cancel_background", [False, True])
def test_due_wakeup_defers_busy_session_restores_selection_and_can_be_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_background: bool
) -> None:
    async def check() -> None:
        busy, waking, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            latest = next(str(message.content) for message in reversed(messages) if message.role == "user")
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "wake first"})
            elif latest == "busy second":
                busy.set()
                await asyncio.Event().wait()
            elif "wake first" in latest and messages[-1].role == "user":
                yield ToolCallEvent(
                    id="dependent-timer", name="wake_up_in", arguments={"seconds": 20, "reason": "dependent wake"}
                )
            elif "wake first" in latest:
                waking.set()
                try:
                    await release.wait()
                finally:
                    cleaned.set()
                yield TextDoneEvent(text="first session wake result")
            else:
                yield TextDoneEvent(text="scheduled")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, clock):
            first = state.harness.session_id
            await client.post("/api/run", json={"session_id": first, "prompt": "start"}, headers=headers)
            second = (await client.post("/api/sessions/new", json={}, headers=headers)).json()["session_id"]
            stream = LiveStream(app, headers, second, "busy second")
            started = await stream.event("run_started")
            await asyncio.wait_for(busy.wait(), 5)
            clock.now += 10
            await state.wakeups.tick()
            assert not waking.is_set() and len(state.wakeups.pending) == 1
            assert (
                await client.post("/api/cancel", json={"run_id": started["run_id"]}, headers=headers)
            ).status_code == 200
            await stream.task
            assert len(state.wakeups.pending) == 1  # Unrelated human chain cancellation.
            cursor = state.wakeups.cursor
            tick = asyncio.create_task(state.wakeups.tick())
            await asyncio.wait_for(waking.wait(), 5)
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["active_session_id"] == first and bootstrap["active_run_background"] is True
            active = state.active
            assert active is not None and active.queue.empty()
            assert len(state.wakeups.pending) == 1
            for path, body in (
                ("/api/sessions/new", {}),
                ("/api/sessions/resume", {"session_id": second}),
                ("/api/run", {"session_id": second, "prompt": "conflict"}),
            ):
                assert (await client.post(path, json=body, headers=headers)).status_code == 409
            settings = (await client.get("/api/settings", headers=headers)).json()
            assert (
                await client.post(
                    "/api/settings",
                    json={"revision": settings["revision"], "values": settings["values"]},
                    headers=headers,
                )
            ).status_code == 409
            assert (await client.get("/api/sessions", headers=headers)).status_code == 409
            other = (await client.get(f"/api/activity/{second}/{cursor}", headers=headers)).json()
            assert other["events"] == [] and other["active_run_id"] == ""
            if cancel_background:
                result = await client.post("/api/cancel", json={"run_id": active.id}, headers=headers)
                assert result.status_code == 200
            else:
                release.set()
            await asyncio.wait_for(tick, 5)
            assert cleaned.is_set() and active.task.done() and state.active is None
            assert len(state.wakeups.pending) == (0 if cancel_background else 1)
            assert state.harness.session_id == second
            history = await state.harness.history()
            assert "first session wake result" not in str(history)
            activity = (await client.get(f"/api/activity/{first}/{cursor}", headers=headers)).json()
            assert activity["events"][-1]["status"] == ("cancelled" if cancel_background else "completed")
            await state.wakeups.tick()
            assert state.active is None

    asyncio.run(check())


@pytest.mark.requires_posix
@pytest.mark.parametrize("action", ["cancel", "disconnect", "shutdown"])
def test_originating_foreground_cancellation_removes_timers_and_joins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async def check() -> None:
        blocked, cleaned = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "pending"})
            else:
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, clock):
            stream = LiveStream(app, headers, state.harness.session_id, "start")
            started = await stream.event("run_started")
            await asyncio.wait_for(blocked.wait(), 5)
            assert len(state.wakeups.pending) == 1
            if action == "cancel":
                assert (
                    await client.post("/api/cancel", json={"run_id": started["run_id"]}, headers=headers)
                ).status_code == 200
            elif action == "disconnect":
                await stream.disconnect()
            if action != "shutdown":
                await stream.task
                assert cleaned.is_set() and not state.wakeups.pending
                clock.now += 10
                await state.wakeups.tick()
                assert state.active is None
        await stream.task
        assert cleaned.is_set()
        activity = state.wakeups.activity(state.harness.session_id, 0, "")
        assert isinstance(activity["events"], list)
        assert [event["status"] for event in activity["events"]] == ["scheduled", "cancelled"]

    asyncio.run(check())


@pytest.mark.requires_posix
def test_unattended_write_shell_and_custom_approvals_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        called = False

        async def custom() -> str:
            """A custom side effect that must never run unattended."""
            nonlocal called
            called = True
            return "should not execute"

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "later"})
            elif len(provider.requests) == 3:
                yield ToolCallEvent(id="write", name="write", arguments={"path": "blocked.txt", "content": "blocked"})
                yield ToolCallEvent(id="shell", name="shell", arguments={"command": "exit 0"})
                yield ToolCallEvent(id="custom", name="custom", arguments={})
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, clock):
            state.harness.agent.register_tool(custom)
            session_id = state.harness.session_id
            await client.post("/api/run", json={"session_id": session_id, "prompt": "start"}, headers=headers)
            cursor = state.wakeups.cursor
            clock.now += 10
            with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Must not start shell")) as shell:
                await asyncio.wait_for(state.wakeups.tick(), 5)
                shell.assert_not_called()
            events = (await client.get(f"/api/activity/{session_id}/{cursor}", headers=headers)).json()["events"]
            results = [event for event in events if event["event"] == "tool_result"]
            assert len(results) == 3 and all("Approval denied" in event["error"] for event in results)
            assert sum(event["event"] == "notice" and "Unattended" in event["text"] for event in events) == 3
            assert not any(event["event"] == "approval" for event in events)
            assert not called and not (tmp_path / "blocked.txt").exists()
            assert events[-1]["status"] == "completed"

    asyncio.run(check())


@pytest.mark.requires_posix
@pytest.mark.parametrize("failure", ["exception", "event", "session"])
def test_background_failures_are_safe_one_shot_and_restore_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "later"})
            elif len(provider.requests) == 3:
                if failure == "exception":
                    raise RuntimeError("SECRET-provider-error")
                yield ErrorEvent(message="SECRET-provider-error", recoverable=False)
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            first = state.harness.session_id
            await client.post("/api/run", json={"session_id": first, "prompt": "start"}, headers=headers)
            second = (await client.post("/api/sessions/new", json={}, headers=headers)).json()["session_id"]
            clock.now += 10
            context = (
                patch.object(state.harness, "resume", side_effect=RuntimeError("SECRET-session-error"))
                if failure == "session"
                else nullcontext()
            )
            with context:
                await state.wakeups.tick()
            requests = len(providers[0].requests)
            await state.wakeups.tick()
            assert len(providers[0].requests) == requests and not state.wakeups.pending
            assert state.harness.session_id == second and state.active is None
            response = await client.get(f"/api/activity/{first}/0", headers=headers)
            assert "SECRET" not in response.text
            events = response.json()["events"]
            assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "failed"
            assert any(event["event"] == "wakeup" and event["status"] == "failed" for event in events)

    asyncio.run(check())


@pytest.mark.requires_posix
def test_automatic_rescheduling_keeps_original_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role == "user":
                yield ToolCallEvent(
                    id=f"timer-{len(provider.requests)}", name="wake_up_in", arguments={"seconds": 1, "reason": "again"}
                )
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            await client.post(
                "/api/run", json={"session_id": state.harness.session_id, "prompt": "start"}, headers=headers
            )
            chain = next(iter(state.wakeups.pending.values())).chain
            for _ in range(wakeups.MAX_ACTIVATIONS):
                assert len(state.wakeups.pending) == 1
                assert next(iter(state.wakeups.pending.values())).chain is chain
                clock.now += 1
                await state.wakeups.tick()
            assert chain.activations == wakeups.MAX_ACTIVATIONS and not state.wakeups.pending
            requests = len(providers[0].requests)
            await state.wakeups.tick()
            assert len(providers[0].requests) == requests
            assert "could not be scheduled" in str(await state.harness.history())

    asyncio.run(check())


@pytest.mark.requires_posix
def test_shutdown_joins_active_scheduler_and_its_background_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            count = len(provider.requests)
            if count in {1, 3}:
                yield ToolCallEvent(
                    id=f"timer-{count}", name="wake_up_in", arguments={"seconds": 10, "reason": "later"}
                )
            elif count == 4:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script, start=True) as (_, client, headers, state, _, clock):
            await client.post(
                "/api/run", json={"session_id": state.harness.session_id, "prompt": "start"}, headers=headers
            )
            clock.now += 10
            state.wakeups.changed.set()
            await asyncio.wait_for(started.wait(), 5)
            active = state.active
            assert active is not None and active.background
            assert len(state.wakeups.pending) == 1
        assert cleaned.is_set() and active.task.done()
        assert all(task.done() for task in state.wakeups._tasks)
        assert not [
            task for task in asyncio.all_tasks() if task.get_name().startswith(("ngn-web", "ngn-run", "ngn-subagent"))
        ]

    asyncio.run(check())


@pytest.mark.requires_posix
def test_cancellation_before_background_producer_starts_is_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "later"})
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            session_id = state.harness.session_id
            await client.post("/api/run", json={"session_id": session_id, "prompt": "start"}, headers=headers)
            original = state._wake

            def cancel_before_start(run: Run, wakeup: Wakeup) -> Coroutine[object, object, None]:
                asyncio.get_running_loop().call_soon(lambda: run.task.cancel())
                return original(run, wakeup)

            clock.now += 10
            with patch.object(state, "_wake", cancel_before_start):
                await state.wakeups.tick()
            assert len(providers[0].requests) == 2 and state.active is None
            events = (await client.get(f"/api/activity/{session_id}/0", headers=headers)).json()["events"]
            assert [event["status"] for event in events if event["event"] == "wakeup"] == ["scheduled", "cancelled"]
            assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "cancelled"

    asyncio.run(check())


@pytest.mark.requires_posix
@pytest.mark.parametrize("downgrade", ["reviewer", "depth"])
def test_child_wakeup_obeys_current_root_permission_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downgrade: str
) -> None:
    async def check() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate", name="delegate", arguments={"prompt": "child"})
            elif provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="wake_up_in", arguments={"seconds": 10, "reason": "later"})
            else:
                yield TextDoneEvent(text="finished")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, clock):
            first = state.harness.session_id
            await client.post("/api/run", json={"session_id": first, "prompt": "start"}, headers=headers)
            settings = (await client.get("/api/settings", headers=headers)).json()
            values = {
                **settings["values"],
                **({"agent": "reviewer"} if downgrade == "reviewer" else {"max_subagent_depth": 0}),
            }
            response = await client.post(
                "/api/settings", json={"revision": settings["revision"], "values": values}, headers=headers
            )
            assert response.status_code == 200
            cursor = state.wakeups.cursor
            clock.now += 10
            await state.wakeups.tick()
            events = (await client.get(f"/api/activity/{first}/{cursor}", headers=headers)).json()["events"]
            if downgrade == "reviewer":
                assert events[-1]["status"] == "completed" and len(providers) == 3
                assert not {"write", "edit", "shell"}.intersection(providers[-1].schemas[0])
                assert state.harness.tasks.list()[0].mode == "reviewer"
            else:
                assert events[-1]["status"] == "failed" and len(providers) == 2
            assert not state.wakeups.pending

    asyncio.run(check())
