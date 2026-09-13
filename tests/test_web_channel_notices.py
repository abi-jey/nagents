"""Shared execution hooks: real SQLite ownership, offline connector, live-only delivery."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

import pytest

from nagents.channels import Channel
from nagents.channels import ChannelActivity
from nagents.channels import ChannelCommand
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelExecutionEvent
from nagents.channels import ChannelMessage
from nagents.channels import ChannelSend
from nagents.channels import dispatch_channel_execution_event
from nagents.channels.runtime import _envelope
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.config import HarnessConfig
from nagents.harness.types import ApprovalRequest
from nagents.web import channel_notices
from nagents.web.catalog import Connection
from nagents.web.channel_notices import ChannelNotices
from nagents.web.service import Pending
from nagents.web.service import Run
from nagents.web.service import WebState
from tests.test_web import ControlledHarness

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.channels import ChannelReceiver
    from nagents.harness.types import HarnessEvent
    from nagents.web.routing import ChatOwner


class NoticeChannel(Channel):
    name = "fixture"

    def __init__(self) -> None:
        self.events: list[ChannelExecutionEvent] = []
        self.activities: list[ChannelActivity] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.exited = asyncio.Event()
        self.block = False
        self.failure = ""

    async def listen(self, receive: ChannelReceiver) -> None:
        await asyncio.Event().wait()

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        raise AssertionError("Host execution observer must not send or format text")

    async def on_event(self, event: ChannelExecutionEvent) -> None:
        self.events.append(event)
        self.entered.set()
        try:
            if self.block:
                await self.release.wait()
            if self.failure == "cancel":
                raise asyncio.CancelledError
            if self.failure:
                raise RuntimeError(self.failure)
        finally:
            self.exited.set()

    async def activity(self, event: ChannelActivity) -> None:
        self.activities.append(event)


@dataclass
class Fixture:
    state: WebState
    run: Run
    channel: NoticeChannel
    observers: list[ChannelNotices] = field(default_factory=list)

    def notices(self) -> ChannelNotices:
        observer = ChannelNotices(self.state, self.run)
        self.observers.append(observer)
        return observer


@asynccontextmanager
async def fixture(path: Path) -> AsyncIterator[Fixture]:
    harness = ControlledHarness(HarnessConfig(workspace=path, data_dir=path / "data", demo=True))
    await harness.initialize()
    state = WebState(harness)
    host = state.channels
    await host.store.initialize()
    await host.store.receive(
        "fixture",
        _envelope("fixture", ChannelMessage("ingress", "room-a", "participant", "hello")),
        harness.session_id,
        None,
    )
    work = await host.store.claim_work()
    assert work is not None
    await host.store.finish_work(work, "completed")
    await harness.resume(work.session_id)
    harness.config.demo = False
    host.catalog.allow_plugins = True
    host.catalog.connections["fixture"] = Connection(
        plugin="fixture", enabled=True, auto_reply=True, config={}, secrets={}, main_session_id=work.session_id
    )
    host.catalog.status["fixture"] = ("running", "")
    channel = NoticeChannel()
    host.channels["fixture"] = channel

    async def source() -> None:
        await asyncio.Event().wait()

    polling = asyncio.create_task(source())
    host.sources["fixture"] = polling
    run = Run(work.session_id)
    task = asyncio.current_task()
    assert task is not None
    run.task = task
    state.active = run
    value = Fixture(state, run, channel)
    try:
        yield value
    finally:
        state.active = None
        for observer in value.observers:
            await observer.finish()
        polling.cancel()
        with suppress(asyncio.CancelledError):
            await polling
        await host.activities.close()
        await harness.close()


def call(id: str = "call-1", **fields: object) -> dict[str, object]:
    return {
        "event": "tool_call",
        "id": id,
        "name": "read_file",
        "arguments": {"path": "README.md", "limit": 3},
        **fields,
    }


@pytest.mark.parametrize("conversation,thread", [("room-a", "thread-4"), ("room-b", "")])
def test_source_thread_is_retained_only_for_the_permanent_owner(tmp_path: Path, conversation: str, thread: str) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            f.run.source = {"channel": "fixture", "conversation_id": conversation, "thread_id": "thread-4"}
            f.run.message_id = "ingress-proof"
            notices = f.notices()
            await notices.start(live=True)
            event = f.channel.events[-1]
            assert event.conversation_id == "room-a"
            assert event.thread_id == thread and event.message_id == "ingress-proof"

    asyncio.run(scenario())


@pytest.mark.parametrize("background", [False, True])
def test_historical_owner_after_new_and_foreign_bindings_do_not_change_destination(
    tmp_path: Path, background: bool
) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            host = f.state.channels
            f.run.background = background
            f.run.source = {"channel": "foreign", "conversation_id": "room-b", "thread_id": "unverified"}
            await host.store.receive(
                "fixture",
                _envelope("fixture", ChannelMessage("new", "room-a", "participant", "/new")),
                f.run.session_id,
                ChannelCommand("new"),
            )

            def foreign(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_bindings VALUES ('fixture', 'room-b', ?, ?)",
                    (f.run.session_id, f.run.session_id),
                )

            await host.store._transaction(foreign)
            notices = f.notices()
            await notices.start(live=True)
            await notices.observe(ToolCallEvent(id="live", name="inspect", arguments={"command": "ls -l"}), live=True)
            await notices.finish()
            assert [event.phase for event in f.channel.events] == ["run_started", "tool_requested", "completed"]
            assert {
                (event.destination, event.session_id, event.run_id, event.thread_id) for event in f.channel.events
            } == {("room-a", f.run.session_id, f.run.id, "")}
            assert f.channel.events[1].tool_arguments == {"command": "ls -l"}
            assert not f.channel.activities

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason",
    [
        "unowned",
        "conflicted",
        "missing-root",
        "missing-history",
        "opt-out",
        "disabled",
        "offline",
        "missing-channel",
        "missing-source",
        "closed-host",
        "demo",
        "plugins-disabled",
        "different-active",
        "finished",
        "different-harness",
        "mutated-run-id",
        "replaced-producer",
    ],
)
def test_fail_closed_permanent_owner_and_genuine_active_run(tmp_path: Path, reason: str) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            host = f.state.channels
            notices = f.notices()

            def mutate(db: sqlite3.Connection) -> None:
                if reason == "unowned":
                    db.execute("DELETE FROM ngn_web_session_owners WHERE session_id = ?", (f.run.session_id,))
                elif reason == "conflicted":
                    db.execute(
                        "UPDATE ngn_web_session_owners SET conflicted = 1 WHERE session_id = ?", (f.run.session_id,)
                    )
                elif reason == "missing-root":
                    db.execute("DELETE FROM harness_sessions WHERE id = ?", (f.run.session_id,))
                elif reason == "missing-history":
                    db.execute("DELETE FROM v2_sessions WHERE id = ?", (f.run.session_id,))

            await host.store._transaction(mutate)
            if reason == "opt-out":
                host.catalog.connections["fixture"].auto_reply = False
            elif reason == "disabled":
                host.catalog.connections["fixture"].enabled = False
            elif reason == "offline":
                host.catalog.status["fixture"] = ("error", "opaque")
            elif reason == "missing-channel":
                host.channels.clear()
            elif reason == "missing-source":
                host.sources.clear()
            elif reason == "closed-host":
                host.closed = True
            elif reason == "demo":
                f.state.harness.config.demo = True
            elif reason == "plugins-disabled":
                host.catalog.allow_plugins = False
            elif reason == "different-active":
                f.state.active = Run(f.state.selected_session_id)
            elif reason == "finished":
                f.run.finished = True
            elif reason == "different-harness":
                f.state.harness.session_id = f.state.selected_session_id
            elif reason == "mutated-run-id":
                f.run.id = "replacement"
            elif reason == "replaced-producer":
                f.run.task = host.sources["fixture"]
            await notices.start(live=True)
            await notices.observe(call(), live=True)
            await notices.finish()
            assert not f.channel.events

    asyncio.run(scenario())


def test_full_original_credentials_checked_before_sdk_sanitizer(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            secret = "fixture-credential-" + "x" * 100
            f.state.channels.catalog.protection.remember({"opaque": secret})
            notices = f.notices()
            await notices.start(live=True)
            await notices.observe(call(arguments={"nested": {"secret": secret}, "limit": 3}), live=True)
            assert f.channel.events[-1].tool_arguments == {}
            await notices.observe(
                call("safe", arguments={"path": "README.md", "command": "ls -l", "limit": 3}), live=True
            )
            assert f.channel.events[-1].tool_arguments == {"path": "README.md", "command": "ls -l", "limit": 3}
            f.state.channels.catalog.protection.remember({"opaque": "read_file"})
            await notices.observe(call("private-name"), live=True)
            assert len(f.channel.events) == 3

    asyncio.run(scenario())


def test_no_replay_broadcast_raw_results_or_recursive_transport_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            notices = f.notices()
            await notices.start()
            await asyncio.create_task(notices.start(live=True))
            assert not f.channel.events
            await notices.start(live=True)
            for record in (call(), call(run_id="foreign"), call(session_id="foreign")):
                await notices.observe(record)
            await notices.observe(call(session_id="foreign"), live=True)
            await asyncio.create_task(notices.observe(call(), live=True))
            for event in (
                DoneEvent(final_text="private-final"),
                ErrorEvent(message="private-error"),
                call(name="channel_send"),
                call(name="channel_list"),
                call(name="channel_action"),
                {"event": "tool_output", "text": "private-output"},
            ):
                await notices.observe(event, live=True)
            assert len(f.channel.events) == 1
            await notices.observe(
                ToolResultEvent(id="result", name="inspect", result="private-output", error="private-error"), live=True
            )
            assert f.channel.events[-1].phase == "tool_completed" and f.channel.events[-1].tool_failed
            assert "private-output" not in repr(f.channel.events) and "private-error" not in repr(f.channel.events)
            await notices.finish("failed")
            await notices.finish()
            await notices.observe(call(), live=True)
            assert [event.phase for event in f.channel.events] == ["run_started", "tool_completed", "failed"]

    asyncio.run(scenario())


def test_scoped_dedup_budgets_and_terminal_without_time_throttling(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            notices = f.notices()
            await notices.start(live=True)
            records = [
                call(),
                call(extra={"task_id": "child-a", "activation": 0}),
                call(task_id="child-b", activation=0),
                call(task_id="child-a", activation=1),
            ]
            for record in records:
                await notices.observe(record, live=True)
                await notices.observe(record, live=True)
            assert len(f.channel.events) == 5
            for index in range(channel_notices.MAX_TOOL_EVENTS + 5):
                await notices.observe(call(f"id-{index}"), live=True)
            assert len(notices._seen) == channel_notices.MAX_TOOL_EVENTS
            await notices.finish("cancelled")
            assert len(f.channel.events) == channel_notices.MAX_TOOL_EVENTS + 2
            assert f.channel.events[-1].phase == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid",
    [
        {"id": ""},
        {"task_id": []},
        {"activation": True},
        {"activation": -1},
        {"extra": None},
        {"task_id": "a", "extra": {"task_id": "b"}},
        {"name": "unsafe\nname"},
    ],
)
def test_malformed_scopes_and_names_are_omitted(tmp_path: Path, invalid: dict[str, object]) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            notices = f.notices()
            await notices.start(live=True)
            await notices.observe({**call(), **invalid}, live=True)
            assert len(f.channel.events) == 1

    asyncio.run(scenario())


def test_only_actual_pending_approval_from_harness_worker_and_not_auto_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            notices = f.notices()
            await notices.start(live=True)
            request = ApprovalRequest(
                id="call-1", tool="read_file", arguments={"path": "README.md"}, description="Private description"
            )
            await notices.observe(call(), live=True)
            await notices.waiting_for_approval(request, live=True)
            pending = Pending("approval", request.id, asyncio.get_running_loop().create_future())
            pending.record = {**asdict(request), "approval_id": pending.id, "run_id": f.run.id}
            f.run.pending = pending
            # Correct-looking record in the web producer still isn't the approval handler.
            await notices.waiting_for_approval(request, live=True)

            async def approve() -> None:
                f.state.harness._worker = asyncio.current_task()
                await notices.waiting_for_approval(request, live=True)
                await notices.waiting_for_approval(request, live=True)
                pending.answer.set_result(True)
                await notices.waiting_for_approval(request, live=True)

            task = asyncio.create_task(approve())
            await task
            f.state.harness._worker = None
            f.run.pending = None
            await notices.finish()
            assert [event.phase for event in f.channel.events] == [
                "run_started",
                "tool_requested",
                "waiting_for_approval",
                "completed",
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["opaque-private-error", "cancel", "timeout"])
def test_shared_helper_isolates_failures_and_joins_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    async def short_dispatch(channel: Channel, event: ChannelExecutionEvent, *, timeout: float) -> None:
        # Exercise the connector deadline after real owner/membership reads.
        # A 30ms budget for those Windows filesystem reads is not the contract.
        assert timeout == channel_notices.NOTICE_TIMEOUT
        await dispatch_channel_execution_event(channel, event, timeout=0.03)

    monkeypatch.setattr(channel_notices, "dispatch_channel_execution_event", short_dispatch)

    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            notices = f.notices()
            f.channel.failure = failure
            f.channel.block = failure == "timeout"
            owner = f.state.channels.store.owner

            async def delayed_owner(session_id: str) -> ChatOwner | None:
                # Deliberately exceed the connector-only deadline during lookup.
                await asyncio.sleep(0.05)
                return await owner(session_id)

            monkeypatch.setattr(f.state.channels.store, "owner", delayed_owner)
            before = asyncio.all_tasks()
            await notices.start(live=True)
            await notices.observe(call(), live=True)
            await notices.observe(call(), live=True)
            await notices.finish()
            assert f.channel.exited.is_set() and asyncio.all_tasks() == before
            assert len(f.channel.events) == 3

    asyncio.run(scenario())
    assert "opaque-private-error" not in caplog.text


def test_producer_cancellation_emits_terminal_and_repeated_cleanup_is_joined(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            f.channel.block = True

            async def producer() -> None:
                notices = f.notices()
                try:
                    await notices.start(live=True)
                finally:
                    await notices.finish("cancelled")

            task = asyncio.create_task(producer())
            f.run.task = task
            await asyncio.wait_for(f.channel.entered.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            f.channel.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert [event.phase for event in f.channel.events] == ["run_started", "cancelled"]
            assert f.channel.exited.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("background", [False, True])
def test_production_hooks_preserve_root_typing_and_do_not_broadcast_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, background: bool
) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            f.run.background = background

            async def events(prompt: str, *, task_id: str = "") -> AsyncIterator[HarnessEvent]:
                yield ToolCallEvent(id="live", name="inspect", arguments={"limit": 2})
                yield ToolResultEvent(id="live", name="inspect", result="private-result")
                yield DoneEvent(final_text="private-final")

            async def live_send(run: Run, record: dict[str, object]) -> None:
                assert f.channel.activities[-1].active

            monkeypatch.setattr(f.state.harness, "wake" if background else "run", events)
            monkeypatch.setattr(f.state, "send", live_send)
            await f.state.produce(f.run, "offline")
            assert [event.phase for event in f.channel.events] == [
                "run_started",
                "tool_requested",
                "tool_completed",
                "completed",
            ]
            assert [event.active for event in f.channel.activities] == [True, False]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["policy", "connection", "active", "credential", "root-error"])
def test_dispatch_revalidates_after_membership_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    change: str,
) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            host = f.state.channels
            notices = f.notices()
            await notices.start(live=True)
            original = host.store.chat_root

            def changed(db: sqlite3.Connection, session_id: str, channel: str, conversation: str) -> str:
                root = original(db, session_id, channel, conversation)
                if change == "policy":
                    host.catalog.connections["fixture"].auto_reply = False
                elif change == "connection":
                    host.catalog.connections["fixture"] = host.catalog.connections["fixture"].model_copy()
                elif change == "active":
                    f.state.active = Run(session_id)
                elif change == "credential":
                    host.catalog.protection.remember({"opaque": "read_file"})
                else:
                    raise RuntimeError("opaque-private-db-error")
                return root

            monkeypatch.setattr(host.store, "chat_root", changed)
            await notices.observe(call(), live=True)
            assert len(f.channel.events) == 1

    asyncio.run(scenario())
    assert "opaque-private-db-error" not in caplog.text
