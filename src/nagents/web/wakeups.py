"""Bounded, process-local one-shot wakeups and read-only background activity."""

import asyncio
import json
import math
import secrets
import time
from collections import deque
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from nagents.cli import _json_default

from .settings import _join

MAX_PENDING = 32
MAX_ACTIVATIONS = 8
MAX_EVENTS = 1024
MAX_ACTIVITY_BYTES = 2 * 1024 * 1024


@dataclass
class Chain:
    activations: int = 0
    cancelled: bool = False


@dataclass(frozen=True)
class Wakeup:
    id: str
    session_id: str
    task_id: str
    run_id: str
    chain: Chain
    deadline: float
    due_at: str
    reason: str

    def summary(self) -> dict[str, str]:
        return {"wakeup_id": self.id, "task_id": self.task_id, "due_at": self.due_at, "reason": self.reason}


class Wakeups:
    def __init__(
        self,
        idle: Callable[[], bool],
        execute: Callable[[Wakeup], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        observer: Callable[[dict[str, object]], None] = lambda record: None,
    ) -> None:
        self.idle = idle
        self.execute = execute
        self.clock = clock
        self.observer = observer
        self.pending: dict[str, Wakeup] = {}
        self.changed = asyncio.Event()
        self.closed = False
        self.cursor = 0
        self._events: deque[tuple[int, dict[str, object], int]] = deque()
        self._bytes = 0
        self._discarded = 0
        self._tasks: list[asyncio.Task[None]] = []

    def publish(self, record: dict[str, object]) -> None:
        self.observer(record)
        record = {**record, "cursor": self.cursor + 1}
        size = len(json.dumps(record, default=_json_default, ensure_ascii=True))
        self.cursor += 1
        self._events.append((self.cursor, record, size))
        self._bytes += size
        while len(self._events) > MAX_EVENTS or self._bytes > MAX_ACTIVITY_BYTES:
            self._discarded, _, removed = self._events.popleft()
            self._bytes -= removed

    def lifecycle(self, wakeup: Wakeup, status: str, *, run_id: str = "") -> None:
        record: dict[str, object] = {
            "event": "wakeup",
            "schema_version": 1,
            "session_id": wakeup.session_id,
            "run_id": run_id or wakeup.run_id,
            **wakeup.summary(),
            "status": status,
        }
        self.publish(record)

    def schedule(
        self, session_id: str, run_id: str, chain: Chain, task_id: str, delay: float, reason: str
    ) -> dict[str, str]:
        if self.closed or chain.cancelled:
            raise RuntimeError("Wakeup owner is no longer active")
        if isinstance(delay, bool) or not math.isfinite(delay) or not 0 < delay <= 7 * 86400:
            raise ValueError("Wakeup delay must be finite, positive, and at most seven days")
        if not reason.strip() or len(reason) > 2000:
            raise ValueError("Wakeup reason must contain 1 to 2000 characters")
        if len(self.pending) >= MAX_PENDING:
            raise ValueError("At most 32 wakeups may be pending in this process")
        if chain.activations >= MAX_ACTIVATIONS:
            raise ValueError("Automatic activation budget exhausted for this human run")
        wakeup = Wakeup(
            secrets.token_urlsafe(24),
            session_id,
            task_id,
            run_id,
            chain,
            self.clock() + delay,
            (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
            reason,
        )
        self.pending[wakeup.id] = wakeup
        self.lifecycle(wakeup, "scheduled")
        self.changed.set()
        return {
            **wakeup.summary(),
            "status": "scheduled",
            "session_id": session_id,
            "run_id": run_id,
        }

    def cancel(self, chain: Chain) -> None:
        chain.cancelled = True
        for wakeup in tuple(self.pending.values()):
            if wakeup.chain is chain:
                del self.pending[wakeup.id]
                self.lifecycle(wakeup, "cancelled")
        self.changed.set()

    def activity(self, session_id: str, after: int, active_run_id: str) -> dict[str, object]:
        return {
            "session_id": session_id,
            "cursor": self.cursor,
            "events": [
                event for cursor, event, _ in self._events if event["session_id"] == session_id and cursor > after
            ],
            "active_run_id": active_run_id,
            "pending_wakeups": [item.summary() for item in self.pending.values() if item.session_id == session_id],
            "truncated": after < self._discarded or after > self.cursor,
        }

    async def tick(self) -> None:
        """Claim at most one due timer, synchronously, before executing any work."""
        if self.closed or not self.idle() or not self.pending:
            return
        wakeup = min(self.pending.values(), key=lambda item: item.deadline)
        if wakeup.deadline > self.clock():
            return
        del self.pending[wakeup.id]
        if wakeup.chain.cancelled:
            self.lifecycle(wakeup, "cancelled")
            return
        if wakeup.chain.activations >= MAX_ACTIVATIONS:
            self.lifecycle(wakeup, "failed")
            return
        wakeup.chain.activations += 1
        try:
            await self.execute(wakeup)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never retry model/tool work or expose raw provider exceptions.
            self.lifecycle(wakeup, "failed")

    def start(self) -> None:
        if not self._tasks and not self.closed:
            self._tasks.append(asyncio.create_task(self._serve(), name="ngn-web-wakeups"))

    async def _serve(self) -> None:
        while not self.closed:
            self.changed.clear()
            await self.tick()
            if self.pending and self.idle():
                delay = max(0, min(item.deadline for item in self.pending.values()) - self.clock())
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), timeout=delay)
            else:
                await self.changed.wait()

    def shutdown(self) -> None:
        self.closed = True
        for wakeup in tuple(self.pending.values()):
            self.cancel(wakeup.chain)
        self.changed.set()

    async def close(self) -> None:
        self.shutdown()
        for task in self._tasks:
            await _join(task)
