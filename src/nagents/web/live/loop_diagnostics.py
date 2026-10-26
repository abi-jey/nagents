"""Identify stalled voice-loop code without collecting request or frame contents."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from .._async import finish_on_cancel

if TYPE_CHECKING:
    from types import FrameType

logger = logging.getLogger("uvicorn.error")
_PACKAGE = Path(__file__).parents[2]
_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FUNCTION = re.compile(r"[A-Za-z_<>][A-Za-z0-9_.<>]{0,127}\Z")


def source_sites(frame: FrameType | None) -> tuple[str, ...]:
    """Keep at most four static package code locations, never frame objects.

    Frames from dependencies, user tools and the workspace are excluded. Do
    not inspect globals, locals, arguments, source text or exception values.
    """
    sites: list[str] = []
    try:
        for _ in range(64):
            if frame is None:
                break
            code, line = frame.f_code, frame.f_lineno
            try:
                relative = Path(code.co_filename).relative_to(_PACKAGE)
            except ValueError:
                relative = Path()
            parts = (*relative.parts[:-1], relative.stem)
            if (
                relative.suffix == ".py"
                and 0 < len(parts) <= 12
                and all(_PART.fullmatch(part) for part in parts)
                and _FUNCTION.fullmatch(code.co_qualname)
                and 0 < line < 1_000_000
            ):
                sites.append(f"nagents.{'.'.join(parts)}:{code.co_qualname}:{line}")
                if len(sites) == 4:
                    break
            frame = frame.f_back
        return tuple(sites)
    finally:
        # A frame retains its entire caller chain and live request objects.
        # Only immutable validated source-location strings leave this function.
        del frame


class LoopStallSampler:
    """An owned watchdog thread samples only an overdue event-loop heartbeat."""

    def __init__(self, session_id: str) -> None:
        self.session_id = (
            session_id if type(session_id) is str and re.fullmatch(r"[0-9a-f]{32}", session_id) else "unavailable"
        )
        self._thread_id = threading.get_ident()
        self._deadline = monotonic() + 0.05
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="ngn-live-loop-sampler", daemon=True)
        self._last_report = float("-inf")
        self._samples = 0
        self._peak_ms = 0.0

    def start(self) -> None:
        self._thread.start()

    def beat(self) -> None:
        self._deadline = monotonic() + 0.05

    def _watch(self) -> None:
        while not self._stop.wait(0.025):
            self._sample()

    def _sample(self) -> None:
        now = monotonic()
        late_ms = (now - self._deadline) * 1000
        if late_ms < 75:
            return
        self._samples += 1
        self._peak_ms = max(self._peak_ms, late_ms)
        if now - self._last_report < 5:
            return
        self._last_report = now
        # The temporary frame mapping is discarded before logging. We retain
        # neither traceback/frame objects nor arbitrary code representations.
        sites = source_sites(sys._current_frames().get(self._thread_id))
        logger.warning(
            "Live event loop stalled: voice_session_id=%s overdue_ms=%.1f samples=%d peak_ms=%.1f sources=%s",
            self.session_id,
            late_ms,
            self._samples,
            self._peak_ms,
            ",".join(sites) if sites else "unavailable",
        )

    async def close(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            await finish_on_cancel(asyncio.to_thread(self._thread.join))
