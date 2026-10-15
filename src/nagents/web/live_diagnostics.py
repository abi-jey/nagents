"""Fixed numeric voice timing counters; never retain audio or request content."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from dataclasses import field
from time import monotonic
from typing import Literal

from nagents.provider._diagnostics import exception_diagnostic

logger = logging.getLogger("uvicorn.error")
FailureStage = Literal[
    "lifecycle",
    "configuration",
    "login_provision",
    "login_control",
    "login_setup",
    "cleanup",
    "provider_stream",
    "media_worker",
    "media_input",
    "media_output",
    "media_stats",
]
_FAILURE_STAGES = frozenset(
    {
        "lifecycle",
        "configuration",
        "login_provision",
        "login_control",
        "login_setup",
        "cleanup",
        "provider_stream",
        "media_worker",
        "media_input",
        "media_output",
        "media_stats",
    }
)


def voice_session_identity(value: object) -> str:
    return value if type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) else "unavailable"


def log_live_failure(session_id: str, stage: FailureStage, error: BaseException | str = "", *, status: int = 0) -> None:
    """Preserve failure causes without exception text, provider payloads or credentials."""
    if issubclass(type(error), asyncio.CancelledError):
        return
    safe_stage = stage if type(stage) is str and stage in _FAILURE_STAGES else "lifecycle"
    diagnostic = exception_diagnostic(error) if isinstance(error, BaseException) else "none"
    logger.error(
        "Live failure: voice_session_id=%s stage=%s status=%d diagnostic=%s",
        voice_session_identity(session_id),
        safe_stage,
        status if type(status) is int and 100 <= status <= 599 else 0,
        diagnostic,
    )


@dataclass
class Timing:
    count: int = 0
    delayed: int = 0
    total_ms: float = 0.0
    peak_ms: float = 0.0

    def observe(self, seconds: float, *, threshold: float = 0.05) -> bool:
        milliseconds = max(0.0, seconds) * 1000
        self.count += 1
        self.total_ms += milliseconds
        self.peak_ms = max(self.peak_ms, milliseconds)
        late = seconds >= threshold
        self.delayed += int(late)
        return late


@dataclass
class BrowserAudioHealth:
    """Separate event-loop lateness from time spent awaiting socket writes."""

    session_id: str = ""
    loop: Timing = field(default_factory=Timing)
    send: Timing = field(default_factory=Timing)
    sent_samples: int = 0
    incomplete_sends: int = 0
    _last_report: float = float("-inf")

    def sent(self, seconds: float, samples: int, *, completed: bool = True) -> None:
        self.sent_samples += samples
        self.incomplete_sends += int(not completed)
        if self.send.observe(seconds) or not completed:
            self.report()

    def lag(self, seconds: float) -> None:
        if self.loop.observe(seconds, threshold=0.025):
            self.report()

    async def watch(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            deadline = loop.time() + 0.05
            await asyncio.sleep(0.05)
            self.lag(loop.time() - deadline)

    def report(self, *, closing: bool = False) -> None:
        now = monotonic()
        if not closing and now - self._last_report < 5:
            return
        self._last_report = now
        log = logger.info if closing else logger.warning
        log(
            "Live browser audio timing: voice_session_id=%s closing=%d sent_samples=%d loop_checks=%d "
            "loop_delays=%d loop_late_ms=%.1f loop_peak_ms=%.1f sends=%d "
            "slow_sends=%d incomplete_sends=%d send_ms=%.1f send_peak_ms=%.1f",
            voice_session_identity(self.session_id),
            int(closing),
            self.sent_samples,
            self.loop.count,
            self.loop.delayed,
            self.loop.total_ms,
            self.loop.peak_ms,
            self.send.count,
            self.send.delayed,
            self.incomplete_sends,
            self.send.total_ms,
            self.send.peak_ms,
        )
