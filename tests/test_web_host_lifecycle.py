"""Host shutdown/claim races, per-root polling and deferred command recovery."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from typing import TypeVar

import aiosqlite
import pytest
from fastapi import HTTPException

from nagents.channels.runtime import _envelope
from nagents.channels.types import ChannelCommand
from nagents.channels.types import ChannelMessage
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness import runtime
from nagents.harness.config import HarnessConfig
from nagents.types import Message
from nagents.web.app import create_app
from nagents.web.catalog import ChannelCatalog
from nagents.web.subscriptions import Subscriber
from tests.test_subagents import FakeProvider
from tests.test_web_channels import FakeChannel
from tests.test_web_channels import site
from tests.test_web_subscription_lifecycle import until

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from nagents.channels.types import ChannelDelivery
    from nagents.channels.types import ChannelSend
    from nagents.events import Event
    from nagents.web.service import WebState

pytestmark = pytest.mark.requires_posix
T = TypeVar("T")


async def answer(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="finished")


@asynccontextmanager
async def application(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: Callable[[FakeProvider, list[Message]], AsyncIterator[Event]] = answer,
) -> AsyncIterator[tuple[WebState, list[FakeProvider]]]:
    providers: list[FakeProvider] = []

    def provider(config: HarnessConfig) -> FakeProvider:
        instance = FakeProvider(config, len(providers), script)
        providers.append(instance)
        return instance

    def harness(config: HarnessConfig) -> Harness:
        instance = Harness(config)
        instance.agent.compactor = None
        return instance

    monkeypatch.setattr(runtime, "HarnessProvider", provider)
    assets = path / "static"
    (assets / "assets").mkdir(parents=True, exist_ok=True)
    config = HarnessConfig(workspace=path, data_dir=path / "data", auth="api-key")
    app = create_app(config, assets=assets, harness_factory=harness)
    async with app.router.lifespan_context(app):
        state: WebState = app.state.web
        yield state, providers
    assert state.active is None and all(task.done() for task in state.channels.tasks)
    assert all(provider.closed for provider in providers)


async def statuses(state: WebState) -> dict[str, str]:
    async with aiosqlite.connect(state.channels.store.db_path) as db:
        cursor = await db.execute("SELECT message_id, status FROM ngn_web_inbox ORDER BY id")
        return {str(id): str(status) for id, status in await cursor.fetchall()}


async def idle(state: WebState) -> None:
    async with asyncio.timeout(10):
        while True:
            pending = await state.channels.store.has_pending(available_channels=tuple(state.channels.channels))
            if not pending and state.active is None and not state.mutating:
                return
            await asyncio.sleep(0.001)


async def joined(task: asyncio.Task[None], state: WebState) -> None:
    done, _ = await asyncio.wait({task}, timeout=5)
    if not done:
        # A regression must fail rather than leave an uncancelled fake provider
        # hanging inside the production owner's deliberately shielded join.
        if state.active is not None:
            await state.stop(state.active)
        await asyncio.wait_for(task, 5)
        pytest.fail("Host cleanup failed to stop and join its worker/producer")
    await task


@pytest.mark.parametrize("ending", ["shutdown", "worker-cancel", "shutdown-and-cancel"])
def test_unstarted_claim_racing_sql_commit_is_requeued_before_shutdown_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    async def scenario() -> None:
        async with application(tmp_path, monkeypatch) as (state, providers):
            channel = FakeChannel("fixture")
            await state.channels.open("fixture", channel)
            reached = ThreadEvent()
            release = ThreadEvent()
            transaction = state.channels.store._transaction

            async def delayed(operation: Callable[[sqlite3.Connection], T]) -> T:
                def blocked(db: sqlite3.Connection) -> T:
                    result = operation(db)
                    if operation.__name__ == "claim":
                        reached.set()
                        assert release.wait(10)
                    return result

                return await transaction(blocked)

            monkeypatch.setattr(state.channels.store, "_transaction", delayed)
            root = state.selected_session_id
            await state.channels.store.web(root, "claim-race", "run once after restart")
            state.channels.changed.set()
            worker = state.channels.tasks[0]
            try:
                await until(reached.is_set)
                assert state.active is None and not providers[0].requests
                if ending != "worker-cancel":
                    closing = asyncio.create_task(state.channels.close())
                    await until(lambda: state.channels.closed)
                if ending != "shutdown":
                    worker.cancel()
                    await asyncio.sleep(0)
                    worker.cancel()  # Repeated cancellation cannot orphan the committed claim.
                assert not channel.closed and state.active is None
            finally:
                release.set()
            if ending == "worker-cancel":
                result = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 5)
                assert isinstance(result[0], asyncio.CancelledError)
                closing = asyncio.create_task(state.channels.close())
            await joined(closing, state)
            assert channel.closed and not providers[0].requests
            assert await statuses(state) == {"claim-race": "queued"}
            assert not any(frame.get("record", {}).get("event") == "run_started" for frame, _ in state.bus.ring)
        async with application(tmp_path, monkeypatch) as (state, providers):
            await idle(state)
            assert await statuses(state) == {"claim-race": "completed"}
            assert len(providers[0].requests) == 1
            await state.channels.store.web(root, "claim-race", "run once after restart")
            state.channels.changed.set()
            await idle(state)
            assert len(providers[0].requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_owner", [False, True])
def test_active_producer_and_worker_stop_before_transport_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_owner: bool
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()
        close_observations: list[tuple[bool, bool, bool]] = []

        async def holding(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            try:
                started.set()
                yield TextChunkEvent(chunk="in progress")
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.set()

        async with application(tmp_path, monkeypatch, holding) as (state, providers):

            class ObservedChannel(FakeChannel):
                async def close(self) -> None:
                    close_observations.append(
                        (stopped.is_set(), state.active is None, all(task.done() for task in state.channels.tasks))
                    )
                    await super().close()

            channel = ObservedChannel("fixture")
            await state.channels.open("fixture", channel)
            await state.channels.store.receive(
                "fixture",
                _envelope("fixture", ChannelMessage("attach", "chat", "sender", "/session main")),
                state.selected_session_id,
                ChannelCommand("session", "main"),
            )
            state.channels.changed.set()
            await idle(state)
            await state.channels.store.web(state.selected_session_id, "active", "hold")
            state.channels.changed.set()
            await started.wait()
            await state.channels.store.web(state.selected_session_id, "later", "pending")
            if cancel_owner:
                # A cancelled worker is still joining a server-owned producer.
                state.channels.tasks[0].cancel()
                await asyncio.sleep(0)
            closing = asyncio.create_task(state.channels.close())
            await joined(closing, state)
            assert close_observations == [(True, True, True)]
            assert len(providers[0].requests) == 1
            assert [event.active for event in channel.activities] == [True, False]
            assert await statuses(state) == {"attach": "completed", "active": "interrupted", "later": "queued"}

    asyncio.run(scenario())


def test_graceful_shutdown_joins_inflight_ack_before_transport_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        reached = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async with application(tmp_path, monkeypatch) as (state, providers):

            class SlowChannel(FakeChannel):
                async def send(self, message: ChannelSend) -> ChannelDelivery:
                    reached.set()
                    await release.wait()
                    result = await super().send(message)
                    finished.set()
                    return result

            channel = SlowChannel("fixture")
            await state.channels.open("fixture", channel)
            await state.channels.store.receive(
                "fixture",
                _envelope("fixture", ChannelMessage("ack", "chat", "sender", "/session main")),
                state.selected_session_id,
                ChannelCommand("session", "main"),
            )
            state.channels.changed.set()
            await reached.wait()
            closing = asyncio.create_task(state.channels.close())
            try:
                await until(lambda: state.channels.closed)
                closing.cancel()  # Owner cancellation still joins network/SQL cleanup.
                await asyncio.sleep(0)
                assert not channel.closed and not closing.done() and not finished.is_set()
            finally:
                release.set()
            result = await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 5)
            assert isinstance(result[0], asyncio.CancelledError)
            assert channel.closed and finished.is_set() and len(channel.deliveries) == 1
            assert await statuses(state) == {"ack": "completed"}
            assert not providers[0].requests

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["deleted", "transient", "malformed"])
@pytest.mark.parametrize("hydrating", [False, True])
def test_failed_root_does_not_starve_healthy_polling_and_deleted_subscription_is_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, hydrating: bool
) -> None:
    async def scenario() -> None:
        async with application(tmp_path, monkeypatch) as (state, providers):
            target = await state.harness.new_session()
            subs = [Subscriber(session_id=root, ready=True) for root in (state.selected_session_id, target)]
            state.bus.subscribers.update(subs)
            try:
                await until(
                    lambda: all(
                        any(
                            frame.get("type") == "snapshot" and frame.get("session_id") == sub.session_id
                            for frame, _ in state.bus.ring
                        )
                        for sub in subs
                    )
                )
                bad, healthy = tuple({sub.session_id for sub in state.bus.subscribers if sub.ready})
                revoked = asyncio.Event()
                bad_sub = next(sub for sub in subs if sub.session_id == bad)
                bad_sub.disconnected = revoked.set
                # Also cover the live-eligibility state while a WS hydration is pending.
                bad_sub.ready = not hydrating
                bad_sub.hydrating = hydrating
                if failure == "deleted":
                    await state.harness.agent.session.delete_session(bad)
                else:
                    original = state.snapshot

                    async def snapshot(root: str = "") -> dict[str, object]:
                        if root == bad:
                            if failure == "malformed":
                                raise ValueError("Synthetic malformed history row")
                            raise HTTPException(503, "Temporary read contention")
                        return await original(root)

                    monkeypatch.setattr(state, "snapshot", snapshot)
                cursor = state.bus.cursor
                await state.harness.agent.session.add_message(healthy, Message(role="user", content="still updated"))
                await until(
                    lambda: any(
                        frame.get("type") == "snapshot"
                        and frame.get("session_id") == healthy
                        and int(str(frame["cursor"])) > cursor
                        for frame, _ in state.bus.ring
                    )
                )
                if failure == "deleted":
                    assert revoked.is_set() and not state.bus.listening(bad) and bad_sub.close_code == 1008
                else:
                    assert not revoked.is_set() and state.bus.listening(bad) and not bad_sub.close_code
                assert not providers[0].requests
            finally:
                state.bus.subscribers.difference_update(subs)

    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["disabled", "deleted", "uninstalled"])
def test_unavailable_ack_survives_restart_while_other_work_runs_then_reenable_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        assert app.client.portal is not None

        async def pause() -> None:
            worker = app.state.channels.tasks[0]
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        app.client.portal.call(pause)
        app.emit("/new Kept once", id="pending-ack", thread="command-thread")
        root = app.bindings()["chat-a"]
        app.emit("accepted channel input", id="pending-model", thread="input-thread")
        main = app.main
        if unavailable == "disabled":
            app.configure(enabled=False)
        elif unavailable == "deleted":
            response = app.client.request(
                "DELETE",
                "/api/channels/fixture",
                headers=app.headers,
                json={"revision": app.state.channels.catalog.revision},
            )
            assert response.status_code == 200
        assert not app.channels[0].deliveries
    discover = ChannelCatalog.discover
    if unavailable == "uninstalled":
        # The saved connector's entry point disappears between server instances.
        def missing_plugin(self: ChannelCatalog, *, descriptors: bool = False) -> None:
            discover(self, descriptors=descriptors)
            self.plugins.pop("fixture", None)

        monkeypatch.setattr(ChannelCatalog, "discover", missing_plugin)
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        app.submit("independent web input", session=main)
        app.client.portal.call(idle, app.state)
        before = app.client.portal.call(statuses, app.state)
        assert before["pending-ack"] == "queued" and before["pending-model"] == "completed"
        assert len(app.providers[0].requests) == 2  # Both web and channel model work bypassed the ack.
        assert not app.state.channels.channels
        assert app.client.portal.call(app.state.channels.store.has_pending)  # Still durably queued.

        async def no_eligible_work() -> bool:
            return await app.state.channels.store.has_pending(available_channels=())

        assert not app.client.portal.call(no_eligible_work)
        monkeypatch.setattr(ChannelCatalog, "discover", discover)
        refreshed = app.client.post("/api/channels/refresh", headers=app.headers, json={})
        assert refreshed.status_code == 200
        app.configure()
        app.idle()
        channel = app.channels[-1]
        assert len(channel.deliveries) == 1
        sent = channel.deliveries[0]
        assert sent.text == f"Session: {root}" and sent.thread_id == "command-thread" and sent.reply_to == "remote-id"
        app.emit("/new Kept once", id="pending-ack", thread="command-thread")
        app.emit("accepted channel input", id="pending-model", thread="input-thread")
        app.idle()
        assert len(channel.deliveries) == 1 and len(app.providers[0].requests) == 2
        assert list(app.roots().values()).count("Kept once") == 1
        assert app.client.portal.call(statuses, app.state)["pending-ack"] == "completed"
