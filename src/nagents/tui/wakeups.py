"""Bounded process-local timers; the TUI owns polling, dispatch and shutdown."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from time import monotonic
from uuid import uuid4


@dataclass
class Budget:
    activations: int = 0
    cancelled: bool = False


@dataclass(frozen=True)
class Wakeup:
    id: str
    root_session_id: str
    session_id: str
    deadline: float
    due_at: str
    reason: str
    budget: Budget


class Wakeups:
    def __init__(self) -> None:
        self.pending: dict[str, Wakeup] = {}
        self.budget = Budget()
        self.closed = False
        self.error = ""

    def human_turn(self) -> None:
        self.budget = Budget()

    def schedule(self, root: str, target: str, delay: float, reason: str) -> dict[str, str]:
        if self.closed or self.budget.cancelled:
            raise RuntimeError("The wake-up owner is no longer active")
        if isinstance(delay, bool) or not math.isfinite(delay) or not 0 < delay <= 604800:
            raise ValueError("Wake-up delay must be finite, positive and at most seven days")
        if not reason.strip() or len(reason) > 2000:
            raise ValueError("Wake-up reason must contain 1 to 2000 characters")
        if len(self.pending) >= 32 or self.budget.activations >= 8:
            raise ValueError("Wake-up scheduling budget exhausted")
        wakeup = Wakeup(
            uuid4().hex,
            root,
            target,
            monotonic() + delay,
            (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
            reason.strip(),
            self.budget,
        )
        self.pending[wakeup.id] = wakeup
        return {
            "wakeup_id": wakeup.id,
            "root_session_id": root,
            "session_id": target,
            "due_at": wakeup.due_at,
            "reason": wakeup.reason,
            "status": "scheduled",
        }

    def upcoming(self, root: str) -> list[Wakeup]:
        return sorted(
            (item for item in self.pending.values() if item.root_session_id == root), key=lambda item: item.deadline
        )

    def claim(self, root: str) -> tuple[Wakeup, ...]:
        if self.closed:
            return ()
        for item in self.upcoming(root):
            if item.budget.cancelled:
                self.pending.pop(item.id)
            elif item.deadline <= monotonic():
                self.pending.pop(item.id)
                if item.budget.activations >= 8:
                    self.error = "A wake-up was not run: its automatic activation budget is exhausted."
                    continue
                item.budget.activations += 1
                self.budget = item.budget
                return (item,)
        return ()

    def cancel_turn(self) -> None:
        self.budget.cancelled = True
        self.pending = {key: item for key, item in self.pending.items() if item.budget is not self.budget}

    def close(self) -> None:
        self.closed = True
        self.pending.clear()
