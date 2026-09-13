"""Bounded nonblocking fan-out, replay, and authenticated WebSocket subscriptions."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from nagents.cli import _json_default

from .settings import _join

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from starlette.websockets import WebSocket

MAX_FRAME = 4096
MAX_REPLAY = 1024
MAX_BYTES = 2 * 1024 * 1024
MAX_QUEUE = 64
MAX_SUBSCRIBERS = 32
MAX_HYDRATIONS = 2
SNAPSHOT_TIMEOUT = 10


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["subscribe", "unsubscribe"]
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^ngn-[A-Za-z0-9-]+$")
    after: int = Field(default=0, ge=0, le=2**53 - 1)
    epoch: str = Field(default="", max_length=128)


@dataclass(eq=False)
class Subscriber:
    session_id: str = ""
    ready: bool = False
    hydrating: bool = False
    lagged: bool = False
    generation: int = 0
    close_code: int = 0
    bytes: int = 0
    queue: asyncio.Queue[tuple[dict[str, object], int]] = field(default_factory=lambda: asyncio.Queue(MAX_QUEUE))
    disconnected: Callable[[], None] = field(default=lambda: None, repr=False)

    def revoke(self) -> None:
        self.generation += 1
        self.ready = self.hydrating = False
        self.session_id = ""
        self.clear()
        self.disconnected()

    def close(self, code: int) -> None:
        if self.close_code:
            return
        self.close_code = code
        self.revoke()
        # Wake the sender without waiting for socket or hydration I/O. Eligibility
        # is already revoked, even if a previous send is backpressured.
        self.queue.put_nowait(({"type": "close"}, 0))

    def offer(self, frame: dict[str, object], size: int) -> None:
        if self.lagged or self.close_code:
            return
        # A full persisted transcript may be larger than the streaming budget.
        # It occupies the entire queue budget by itself; never make an old root
        # impossible to subscribe to solely because its history grew.
        if frame.get("type") == "snapshot":
            size = min(size, MAX_BYTES)
        if self.queue.full() or self.bytes + size > MAX_BYTES:
            self.lagged = True
            self.close(1013)
            return
        self.bytes += size
        self.queue.put_nowait((frame, size))

    def clear(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        self.bytes = 0


class EventBus:
    def __init__(self) -> None:
        self.epoch = secrets.token_hex(16)
        self.cursor = 0
        self.discarded = 0
        self.bytes = 0
        self.ring: deque[tuple[dict[str, object], int]] = deque()
        self.subscribers: set[Subscriber] = set()

    def listening(self, session_id: str) -> bool:
        return any(
            (sub.ready or sub.hydrating) and not sub.lagged and sub.session_id == session_id for sub in self.subscribers
        )

    def invalidate_session(self, session_id: str) -> None:
        """Revoke matching subscriptions now; their owners close with 1008 and join hydration.

        Call on the service event loop. This also invalidates pending, not-yet-
        verified subscriptions and prevents their late snapshots from publishing.
        """
        for subscriber in self.subscribers:
            if subscriber.session_id == session_id:
                subscriber.close(1008)

    async def checkpoint(
        self, session_id: str, snapshot: Callable[[str], Awaitable[dict[str, object]]]
    ) -> tuple[int, dict[str, object]]:
        """Read a stable event/history cut without locking or slowing producers.

        An asynchronous history read can predate run completion and the loss of
        its active archive. A later cursor cannot label that older view. Retry
        changed reads instead; busy readers time out rather than blocking runs.
        The caller must publish the result without another await.
        """
        async with asyncio.timeout(SNAPSHOT_TIMEOUT):
            while True:
                cursor = self.cursor
                data = await snapshot(session_id)
                if cursor == self.cursor:
                    return cursor, data
                await asyncio.sleep(0)

    def publish(self, frame: dict[str, object]) -> None:
        self.cursor += 1
        # Detach mutable event payloads and normalize datetime/Path wire values.
        encoded = json.dumps(
            {**frame, "cursor": self.cursor, "epoch": self.epoch}, default=_json_default, ensure_ascii=True
        )
        record: dict[str, object] = json.loads(encoded)
        size = len(encoded)
        self.ring.append((record, size))
        self.bytes += size
        while len(self.ring) > MAX_REPLAY or self.bytes > MAX_BYTES:
            removed, count = self.ring.popleft()
            self.discarded = int(str(removed["cursor"]))
            self.bytes -= count
        for subscriber in self.subscribers:
            if subscriber.ready and frame.get("session_id", subscriber.session_id) == subscriber.session_id:
                subscriber.offer(record, size)

    def event(self, record: dict[str, object]) -> None:
        self.publish({"type": "event", "session_id": record["session_id"], "record": record})

    async def serve(
        self,
        socket: WebSocket,
        snapshot: Callable[[str], Awaitable[dict[str, object]]],
        disconnected: Callable[[], None],
    ) -> None:
        if len(self.subscribers) >= MAX_SUBSCRIBERS:
            await socket.close(code=1013)
            return
        # Reserve before accept yields so concurrent handshakes respect the cap.
        subscriber = Subscriber(disconnected=disconnected)
        self.subscribers.add(subscriber)
        hydrations: set[asyncio.Task[None]] = set()

        def cancel_hydrations() -> None:
            for task in hydrations:
                if not task.done() and not task.cancelling():
                    task.cancel()

        async def hydrate(body: SubscriptionInput, generation: int) -> None:
            def current() -> bool:
                return subscriber.generation == generation and not subscriber.close_code

            task = asyncio.current_task()
            assert task is not None

            def expired() -> None:
                # Independent of cooperative cancellation: an adapter can join a
                # slow DB read, but cannot retain approval eligibility meanwhile.
                if current():
                    subscriber.close(1013)
                if not task.done() and not task.cancelling():
                    task.cancel()

            deadline = asyncio.get_running_loop().call_later(SNAPSHOT_TIMEOUT, expired)
            try:
                # Validate root membership even when an in-range replay exists.
                # checkpoint also guards callbacks that yield after reading state.
                cursor, data = await self.checkpoint(body.session_id, snapshot)
                if not current():
                    return
                if data.get("session_id") != body.session_id:
                    raise ValueError("Snapshot session mismatch")
                replay = [
                    (frame, size)
                    for frame, size in self.ring
                    if body.after < int(str(frame["cursor"]))
                    and frame.get("session_id", body.session_id) == body.session_id
                ]
                subscriber.clear()
                if (
                    body.epoch == self.epoch
                    and self.discarded <= body.after <= cursor
                    and len(replay) <= MAX_QUEUE
                    and sum(size for _, size in replay) <= MAX_BYTES
                ):
                    for frame, size in replay:
                        subscriber.offer(frame, size)
                else:
                    frame = {
                        "type": "snapshot",
                        "session_id": body.session_id,
                        "cursor": cursor,
                        "epoch": self.epoch,
                        "snapshot": data,
                    }
                    subscriber.offer(frame, len(json.dumps(frame, default=_json_default)))
                # No await between the checkpoint, replay and live activation.
                subscriber.ready = not subscriber.close_code
                subscriber.hydrating = False
            except HTTPException as error:
                if current():
                    subscriber.close(1008 if error.status_code in {403, 404} else 1013)
            except (ValueError, RecursionError):
                if current():
                    subscriber.close(1008)
            except asyncio.CancelledError:
                if current():
                    subscriber.close(1013)
                raise
            except Exception:
                if current():
                    subscriber.close(1013)
            finally:
                deadline.cancel()

        async def receive() -> None:
            while not subscriber.close_code:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    subscriber.revoke()
                    return
                text = message.get("text")
                if text is None or len(text.encode("utf-8")) > MAX_FRAME:
                    subscriber.close(1008)
                    return
                try:
                    # JSON duplicate fields are not allowed to select conflicting sessions/cursors.
                    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
                        result: dict[str, object] = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError("Duplicate field")
                            result[key] = value
                        return result

                    body = SubscriptionInput.model_validate(json.loads(text, object_pairs_hook=unique))
                    if body.type == "unsubscribe" and body.session_id != subscriber.session_id:
                        raise ValueError("Unknown subscription")
                    same_session = (
                        body.type == "subscribe"
                        and subscriber.session_id == body.session_id
                        and (subscriber.ready or subscriber.hydrating)
                    )
                    subscriber.generation += 1
                    cancel_hydrations()
                    # A still-live, verified same-root subscription keeps its
                    # approval eligibility, but controls never wait for history.
                    subscriber.hydrating = same_session
                    subscriber.ready = False
                    if not same_session:
                        subscriber.session_id = ""
                    subscriber.clear()
                    if not same_session:
                        disconnected()
                    if body.type == "unsubscribe":
                        continue
                    subscriber.session_id = body.session_id
                    hydrations.difference_update(task for task in tuple(hydrations) if task.done())
                    if len(hydrations) >= MAX_HYDRATIONS:
                        subscriber.close(1013)
                        return
                    task = asyncio.create_task(hydrate(body, subscriber.generation), name="ngn-web-hydration")
                    hydrations.add(task)
                    task.add_done_callback(hydrations.discard)
                except (ValidationError, ValueError, RecursionError):
                    subscriber.close(1008)
                    return

        async def send() -> None:
            while True:
                frame, size = await subscriber.queue.get()
                subscriber.bytes -= size
                if subscriber.close_code:
                    return
                # An ASGI peer that stops reading cannot retain approvals indefinitely.
                async with asyncio.timeout(5):
                    await socket.send_text(json.dumps(frame, default=_json_default, ensure_ascii=True))

        tasks: list[asyncio.Task[None]] = []
        try:
            await socket.accept(subprotocol="ngn.events.v1")
            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except TimeoutError:
            subscriber.close(1013)
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            subscriber.revoke()
            cancel_hydrations()
            for task in tasks:
                task.cancel()

            async def cleanup() -> None:
                await asyncio.gather(*tasks, return_exceptions=True)
                if subscriber.close_code:
                    with suppress(WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
                        async with asyncio.timeout(5):
                            await socket.close(code=subscriber.close_code)
                # Receive never joins cancelled hydration. Only the connection
                # owner does so after revocation/close, including ASGI cancellation.
                await asyncio.gather(*tuple(hydrations), return_exceptions=True)

            try:
                await _join(asyncio.create_task(cleanup()))
            finally:
                # Closing sockets retain their capacity slot until DB cleanup
                # completes; reconnects cannot accumulate unlimited orphan reads.
                self.subscribers.discard(subscriber)
