"""Wake-up scheduler for the nagents server.

Allows the agent to schedule itself to wake up at a future time,
preserving the session context so it can continue where it left off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger("nagents.server.scheduler")

WAKEUPS_PATH = Path("/data/wakeups.json")
_check_task: asyncio.Task[None] | None = None
_on_wakeup: asyncio.Callable[[dict[str, str]], None] | None = None

# Strong references to in-flight wake-up tasks (prevents GC mid-run).
_wakeup_tasks: set[asyncio.Task[None]] = set()


class ScheduledWakeUp:
    """A scheduled wake-up request."""

    __slots__ = ("completed", "id", "reason", "scheduled_at", "session_id", "user_id", "wake_up_at")

    def __init__(
        self,
        id: str,
        scheduled_at: str,
        wake_up_at: str,
        session_id: str,
        user_id: str,
        reason: str = "",
        completed: bool = False,
    ) -> None:
        self.id = id
        self.scheduled_at = scheduled_at
        self.wake_up_at = wake_up_at
        self.session_id = session_id
        self.user_id = user_id
        self.reason = reason
        self.completed = completed

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "scheduled_at": self.scheduled_at,
            "wake_up_at": self.wake_up_at,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "reason": self.reason,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str | bool]) -> ScheduledWakeUp:
        return cls(
            id=str(d["id"]),
            scheduled_at=str(d["scheduled_at"]),
            wake_up_at=str(d["wake_up_at"]),
            session_id=str(d["session_id"]),
            user_id=str(d.get("user_id", "default")),
            reason=str(d.get("reason", "")),
            completed=bool(d.get("completed", False)),
        )

    def is_due(self, now: datetime | None = None) -> bool:
        if self.completed:
            return False
        if now is None:
            now = datetime.now()
        return datetime.fromisoformat(self.wake_up_at) <= now


def _load_wakeups() -> list[ScheduledWakeUp]:
    if not WAKEUPS_PATH.is_file():
        return []
    try:
        data = json.loads(WAKEUPS_PATH.read_text())
        return [ScheduledWakeUp.from_dict(d) for d in data.get("wake_ups", [])]
    except Exception:
        logger.exception("Failed to load wakeups")
        return []


def _save_wakeups(wakeups: list[ScheduledWakeUp]) -> None:
    WAKEUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WAKEUPS_PATH.write_text(json.dumps({"wake_ups": [w.to_dict() for w in wakeups]}, indent=2))
    logger.info("Saved %d wakeups", len(wakeups))


def set_wakeup_callback(callback: asyncio.Callable[[dict[str, str]], None]) -> None:
    """Set the callback invoked when a wake-up is due."""
    global _on_wakeup
    _on_wakeup = callback


def start_wakeup_loop() -> None:
    """Start the background task that checks for due wake-ups."""
    global _check_task
    if _check_task is not None:
        return
    _check_task = asyncio.create_task(_wakeup_check_loop())
    logger.info("Started wake-up check loop")


def stop_wakeup_loop() -> None:
    """Stop the background task."""
    global _check_task
    if _check_task:
        _check_task.cancel()
        _check_task = None


async def _wakeup_check_loop() -> None:
    """Background loop checking for due wake-ups every 10 seconds."""
    logger.info("Wake-up check loop running")
    while True:
        try:
            await asyncio.sleep(10)
            wakeups = _load_wakeups()
            due = [w for w in wakeups if w.is_due()]
            for w in due:
                logger.info("Processing wake-up %s (session=%s, reason=%s)", w.id, w.session_id, w.reason)
                w.completed = True
                if _on_wakeup:
                    # Fire-and-forget: the callback is a coroutine — schedule it
                    # as a task so it actually runs (a bare call only creates the
                    # coroutine, never executing it) and so a long agent run does
                    # not block the check loop.
                    task = asyncio.create_task(_on_wakeup(w.to_dict()))
                    _wakeup_tasks.add(task)
                    task.add_done_callback(_wakeup_tasks.discard)
            if due:
                _save_wakeups(wakeups)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in wake-up check loop")


# ── Context for the tool ─────────────────────────────────────────────────────
_current_session_id: str | None = None
_current_user_id: str = "default"


def set_session_context(session_id: str | None, user_id: str = "default") -> None:
    """Set the current session context for the wake_up_in tool."""
    global _current_session_id, _current_user_id
    _current_session_id = session_id
    _current_user_id = user_id


def wake_up_in(
    seconds: int | None = None,
    minutes: int | None = None,
    hours: int | None = None,
    days: int | None = None,
    reason: str = "",
) -> str:
    """Schedule yourself to wake up and resume this conversation after a delay.

    Use this tool to set reminders or schedule future actions. Common scenarios:

    - **Monitoring**: "Check if the service is back up" — wake up in 30 minutes to retry.
    - **Scheduled tasks**: "Fetch the daily report at 9 AM" — wake up tomorrow morning.
    - **Waiting**: "Wait for the deployment to finish" — wake up in 10 minutes to check status.
    - **Follow-ups**: "Remind the user about the pending PR" — wake up in 2 hours.

    When you wake up, you will have full context of this conversation and can
    continue exactly where you left off. The reason you provide will be shown
    to you when you wake up, so make it descriptive.

    At least one time parameter must be provided. Parameters are additive:
    wake_up_in(hours=1, minutes=30) means 1 hour and 30 minutes from now.

    Args:
        seconds: Number of seconds to wait before waking up.
        minutes: Number of minutes to wait before waking up.
        hours: Number of hours to wait before waking up.
        days: Number of days to wait before waking up.
        reason: Why you want to wake up. Be specific — this will be shown to you
                when you wake up. E.g., "Check if the API at X is responding",
                "Fetch daily metrics from the monitoring endpoint",
                "Retry the failed deployment after cooldown".
    """
    if all(v is None for v in [seconds, minutes, hours, days]):
        return "Error: At least one time parameter (seconds, minutes, hours, days) must be provided."

    if _current_session_id is None:
        return "Error: No active session to schedule wake-up for."

    delay = timedelta(
        seconds=int(seconds or 0),
        minutes=int(minutes or 0),
        hours=int(hours or 0),
        days=int(days or 0),
    )

    if delay.total_seconds() <= 0:
        return "Error: Wake-up time must be in the future."

    now = datetime.now()
    wake_up_at = now + delay

    wakeup = ScheduledWakeUp(
        id=str(uuid.uuid4()),
        scheduled_at=now.isoformat(),
        wake_up_at=wake_up_at.isoformat(),
        session_id=_current_session_id,
        user_id=_current_user_id,
        reason=reason,
    )

    wakeups = _load_wakeups()
    wakeups.append(wakeup)
    _save_wakeups(wakeups)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    delay_str = ", ".join(parts) if parts else "immediately"

    return (
        f"Wake-up scheduled: I will wake up in {delay_str} at "
        f"{wake_up_at.strftime('%Y-%m-%d %H:%M:%S')}.\n"
        f"Wake-up ID: {wakeup.id}\n"
        f"Session: {_current_session_id}"
    )
