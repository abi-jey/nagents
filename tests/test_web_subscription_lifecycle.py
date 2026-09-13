"""Consistent checkpoints and responsive, bounded subscription ownership."""

from __future__ import annotations

import asyncio
import json
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocket

from nagents.web import subscriptions
from nagents.web.subscriptions import EventBus
from tests.test_web_channels import site

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from starlette.types import Message

    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix


class Peer:
    """Real Starlette WebSocket with explicit ASGI input/output and no network."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.outgoing: asyncio.Queue[Message] = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.socket = WebSocket({"type": "websocket"}, self.incoming.get, self.outgoing.put)

    def subscribe(self, root: str, **position: object) -> None:
        self.input({"type": "subscribe", "session_id": root, **position})

    def input(self, body: dict[str, object]) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(body)})

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def message(self) -> Message:
        return await asyncio.wait_for(self.outgoing.get(), 5)

    async def frame(self) -> dict[str, object]:
        message = await self.message()
        assert message["type"] == "websocket.send", message
        return cast("dict[str, object]", json.loads(message["text"]))

    async def start(
        self, bus: EventBus, snapshot: Callable[[str], Awaitable[dict[str, object]]], disconnected: Callable[[], None]
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(bus.serve(self.socket, snapshot, disconnected))
        assert (await self.message())["type"] == "websocket.accept"
        return task


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


async def delayed_read(release: asyncio.Event, cancelled: asyncio.Event) -> None:
    """An adapter that finishes I/O even after cancellation, then returns late."""
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()


def stop_poller(app: Site) -> None:
    async def stop() -> None:
        task = app.state.channels.tasks[1]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert app.client.portal is not None
    app.client.portal.call(stop)


@pytest.mark.parametrize("mode", ["http", "initial", "recovery"])
def test_history_read_racing_complete_run_has_consistent_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        stop_poller(app)
        assert app.client.portal is not None
        with app.socket() as socket:
            if mode == "recovery":
                socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
                assert socket.receive_json()["type"] == "snapshot"
            reached = ThreadEvent()
            release = app.client.portal.call(asyncio.Event)
            original = app.state.history.snapshot
            reads = 0

            async def read(root: str) -> list[dict[str, object]]:
                nonlocal reads
                rows = await original(root)
                reads += 1
                if reads == 1:
                    reached.set()
                    await release.wait()
                return rows

            monkeypatch.setattr(app.state.history, "snapshot", read)
            if mode == "http":
                response = app.client.portal.start_task_soon(app.state.snapshot, app.main)
            else:
                socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            try:
                assert reached.wait(5)
                accepted = app.submit("complete during hydration")
                app.idle()  # Hydration must not block the model or the durable worker.
            finally:
                app.client.portal.call(release.set)
            if mode == "http":
                snapshot = response.result(timeout=5)
            else:
                frame = socket.receive_json()
                assert frame["type"] == "snapshot"
                snapshot = frame["snapshot"]
                cursor = frame["cursor"]
                app.client.portal.call(
                    app.state.bus.event, {"session_id": app.main, "event": "notice", "text": "after checkpoint"}
                )
                event = socket.receive_json()
                assert event["cursor"] > cursor and event["record"]["text"] == "after checkpoint"
            history = cast("list[dict[str, object]]", snapshot["history"])
            assert [row["content"] for row in history] == ["complete during hydration", "answer"]
            assert history[0]["message_id"] == accepted["message_id"]
            assert snapshot["active_run"] is None and reads >= 2


def test_replay_during_hydration_delivers_each_event_then_live_handoff() -> None:
    async def scenario() -> None:
        bus = EventBus()
        peer = Peer()
        reached = asyncio.Event()
        release = asyncio.Event()
        reads = 0

        async def snapshot(root: str) -> dict[str, object]:
            nonlocal reads
            reads += 1
            data: dict[str, object] = {"session_id": root, "history": []}
            if reads == 2:
                reached.set()
                await release.wait()
            return data

        task = await peer.start(bus, snapshot, lambda: None)
        try:
            peer.subscribe("ngn-root")
            initial = await peer.frame()
            peer.subscribe("ngn-root", after=initial["cursor"], epoch=initial["epoch"])
            await reached.wait()
            for index in range(3):
                bus.event({"session_id": "ngn-root", "event": "notice", "text": str(index)})
            release.set()
            frames = [await peer.frame() for _ in range(3)]
            assert [cast("dict[str, object]", frame["record"])["text"] for frame in frames] == ["0", "1", "2"]
            assert [frame["cursor"] for frame in frames] == [1, 2, 3]
            bus.event({"session_id": "ngn-root", "event": "notice", "text": "live"})
            assert (await peer.frame())["cursor"] == 4
            assert reads >= 3 and peer.outgoing.empty()
        finally:
            release.set()
            peer.disconnect()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("departure", ["disconnect", "unsubscribe", "switch", "invalidate"])
def test_hydration_departure_revokes_approval_before_db_cleanup_and_keeps_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, departure: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        stop_poller(app)
        target = app.client.post("/api/sessions/new", headers=app.headers, json={}).json()["session_id"]
        assert app.client.portal is not None
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main})
            socket.receive_json()
            app.submit("privilege")
            while True:
                frame = socket.receive_json()
                if frame.get("record", {}).get("event") == "approval":
                    break
            approval = frame["record"]
            run = app.state.active
            assert run is not None and run.pending is not None
            answer = run.pending.answer
            reached = ThreadEvent()
            release = app.client.portal.call(asyncio.Event)
            cancelled = app.client.portal.call(asyncio.Event)
            original = app.state.history.snapshot

            async def read(root: str) -> list[dict[str, object]]:
                rows = await original(root)
                if root == app.main:
                    reached.set()
                    await delayed_read(release, cancelled)
                return rows

            monkeypatch.setattr(app.state.history, "snapshot", read)
            socket.send_json({"type": "subscribe", "session_id": app.main})
            try:
                assert reached.wait(5)
                # Verified same-root hydration alone preserves the outstanding decision.
                assert app.state.bus.listening(app.main) and not answer.done()
                if departure == "disconnect":
                    socket.close()
                elif departure == "unsubscribe":
                    socket.send_json({"type": "unsubscribe", "session_id": app.main})
                elif departure == "switch":
                    socket.send_json({"type": "subscribe", "session_id": target})
                else:
                    app.client.portal.call(app.state.bus.invalidate_session, app.main)
                app.client.portal.call(until, lambda: not app.state.bus.listening(app.main) and answer.done())
                assert answer.result() is False and not release.is_set()
                result = app.client.post(
                    "/api/approval",
                    headers=app.headers,
                    json={
                        "run_id": approval["run_id"],
                        "approval_id": approval["approval_id"],
                        "call_id": approval["id"],
                        "decision": "allow",
                    },
                )
                assert result.status_code == 409
                app.idle()
                assert run.outcome == "completed" and not run.task.cancelled()
                assert not app.channels[0].deliveries
                app.client.portal.call(asyncio.wait_for, cancelled.wait(), 5)
                if departure == "switch":
                    assert socket.receive_json()["session_id"] == target
            finally:
                app.client.portal.call(release.set)


@pytest.mark.parametrize("late", ["snapshot", "forbidden"])
def test_superseded_noncooperative_hydration_cannot_publish_or_close_new_subscription(late: str) -> None:
    async def scenario() -> None:
        bus = EventBus()
        peer = Peer()
        reached = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        returned = asyncio.Event()

        async def snapshot(root: str) -> dict[str, object]:
            if root == "ngn-old":
                reached.set()
                await delayed_read(release, cancelled)
                returned.set()
                if late == "forbidden":
                    raise HTTPException(404, "No longer a root")
            return {"session_id": root, "history": []}

        task = await peer.start(bus, snapshot, lambda: None)
        try:
            peer.subscribe("ngn-old")
            await reached.wait()
            assert not bus.listening("ngn-old")  # Not verified until snapshot succeeds.
            peer.subscribe("ngn-new")
            assert (await peer.frame())["session_id"] == "ngn-new"
            assert cancelled.is_set() and bus.listening("ngn-new")
            release.set()
            await returned.wait()
            await asyncio.sleep(0)
            assert not task.done() and not bus.listening("ngn-old") and peer.outgoing.empty()
            bus.event({"session_id": "ngn-new", "event": "notice", "text": "still live"})
            assert cast("dict[str, object]", (await peer.frame())["record"])["text"] == "still live"
        finally:
            release.set()
            peer.disconnect()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing", "wrong-root", "exception"])
def test_failed_membership_never_publishes_snapshot_or_replay(failure: str) -> None:
    async def scenario() -> None:
        bus = EventBus()
        peer = Peer()
        bus.event({"session_id": "ngn-hidden", "event": "notice", "text": "private evidence"})

        async def snapshot(root: str) -> dict[str, object]:
            if failure == "missing":
                raise HTTPException(404, "Not a root")
            if failure == "exception":
                raise RuntimeError("private database error")
            return {"session_id": "ngn-other", "history": ["private evidence"]}

        task = await peer.start(bus, snapshot, lambda: None)
        peer.subscribe("ngn-hidden", after=0, epoch=bus.epoch)
        close = await peer.message()
        assert close["type"] == "websocket.close"
        assert close["code"] == (1013 if failure == "exception" else 1008)
        await asyncio.wait_for(task, 5)
        assert not bus.listening("ngn-hidden") and peer.outgoing.empty()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["timeout", "invalidate", "capacity", "owner-cancel"])
def test_stubborn_hydration_is_bounded_revoked_and_joined(monkeypatch: pytest.MonkeyPatch, ending: str) -> None:
    async def scenario() -> None:
        bus = EventBus()
        peer = Peer()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        started = 0
        finished = 0
        revocations = 0

        def disconnected() -> None:
            nonlocal revocations
            revocations += 1

        async def snapshot(root: str) -> dict[str, object]:
            nonlocal started, finished
            started += 1
            await delayed_read(release, cancelled)
            finished += 1
            return {"session_id": root, "history": []}

        if ending == "timeout":
            monkeypatch.setattr(subscriptions, "SNAPSHOT_TIMEOUT", 0.03)
        task = await peer.start(bus, snapshot, disconnected)
        peer.subscribe("ngn-root")
        await until(lambda: started == 1)
        before = revocations
        try:
            if ending == "invalidate":
                bus.invalidate_session("ngn-root")
                assert revocations > before
            elif ending == "capacity":
                for count in range(2, subscriptions.MAX_HYDRATIONS + 1):
                    peer.subscribe("ngn-root")

                    def all_started(count: int = count) -> bool:
                        return started == count

                    await until(all_started)
                peer.subscribe("ngn-root")
            elif ending == "owner-cancel":
                task.cancel()
                await cancelled.wait()
                # Repeated owner cancellation still joins the same bounded work.
                task.cancel()
            if ending != "owner-cancel":
                close = await peer.message()
                assert close["type"] == "websocket.close"
                assert close["code"] == (1008 if ending == "invalidate" else 1013)
            await cancelled.wait()
            assert not bus.listening("ngn-root") and revocations > before
            assert not task.done() and finished == 0 and started <= subscriptions.MAX_HYDRATIONS
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert finished == started and not bus.subscribers and peer.outgoing.empty()
        if ending == "owner-cancel":
            assert isinstance(result[0], asyncio.CancelledError)
        else:
            assert result[0] is None

    asyncio.run(scenario())


def test_checkpoint_timeout_does_not_starve_event_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        bus = EventBus()
        peer = Peer()

        async def snapshot(root: str) -> dict[str, object]:
            bus.event({"session_id": root, "event": "notice", "text": "concurrent write"})
            return {"session_id": root, "history": []}

        monkeypatch.setattr(subscriptions, "SNAPSHOT_TIMEOUT", 0.03)
        task = await peer.start(bus, snapshot, lambda: None)
        peer.subscribe("ngn-root")
        close = await peer.message()
        assert close["type"] == "websocket.close" and close["code"] == 1013
        await asyncio.wait_for(task, 5)
        assert bus.cursor > 1 and not bus.subscribers

    asyncio.run(scenario())


def test_closing_hydration_retains_connection_capacity_until_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(subscriptions, "MAX_SUBSCRIBERS", 1)
        bus = EventBus()
        peer = Peer()
        reached = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def snapshot(root: str) -> dict[str, object]:
            reached.set()
            await delayed_read(release, cancelled)
            return {"session_id": root, "history": []}

        task = await peer.start(bus, snapshot, lambda: None)
        peer.subscribe("ngn-root")
        await reached.wait()
        peer.disconnect()
        try:
            await cancelled.wait()
            assert not bus.listening("ngn-root")
            other = Peer()
            refused = asyncio.create_task(bus.serve(other.socket, snapshot, lambda: None))
            close = await other.message()
            assert close["type"] == "websocket.close" and close["code"] == 1013
            await refused
            assert not task.done()
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
        assert not bus.subscribers
        next_peer = Peer()
        next_task = await next_peer.start(bus, snapshot, lambda: None)
        next_peer.subscribe("ngn-root")
        assert (await next_peer.frame())["type"] == "snapshot"
        next_peer.disconnect()
        await asyncio.wait_for(next_task, 5)

    asyncio.run(scenario())
