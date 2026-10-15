"""Bounded, interruption-aware PCM pacing from provider bursts to the browser."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nagents.audio import AudioFormat

from ._async import finish_on_cancel
from .live_diagnostics import Timing
from .live_diagnostics import voice_session_identity
from .live_realtime import RealtimeBuffer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator

FRAME_BYTES = 960  # 20 ms, mono PCM16 at 24 kHz.
BUFFER_BYTES = FRAME_BYTES * 100  # Two seconds of queued output, with backpressure.
_BYTES_PER_SECOND = 48000
logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class OutputPacket:
    data: bytes = b""
    interrupted: bool = False


class BrowserOutput:
    """Route already-paced RTP or pace API bursts, never synthetic silence.

    RTP drains without backpressure into a bounded, age-aware reservoir. API
    bursts keep one cumulative clock and backpressure their producer at two seconds.
    Interrupt invalidates waiting writes and the sender's leased frame together.
    """

    audio_format = AudioFormat()

    def __init__(self) -> None:
        self._frames: deque[bytes] = deque()
        self._condition = asyncio.Condition()
        self._writer = asyncio.Lock()
        self._changed = asyncio.Event()
        self._epoch = 0
        self._closed = False
        self._interrupted = False
        self.buffered_bytes = 0
        self.peak_buffered_bytes = 0
        self.blocked_writes = 0
        self.sent_samples = 0
        self.interrupted_samples = 0
        self.clock_rebases = 0
        self.voice_session_id = ""
        self.queue_wait = Timing()
        self.pacing_late = Timing()
        self._last_report = float("-inf")
        self.realtime = False
        self._packets_started = False
        self._live = RealtimeBuffer()
        self._live_changed = asyncio.Event()

    def use_realtime_clock(self) -> None:
        """Select already-paced RTP before any output starts, never for API bursts."""
        if self._frames or self._packets_started or self.sent_samples or self._closed:
            raise RuntimeError("Real-time output must be selected before playback starts")
        self.realtime = True

    def _sync_live(self) -> None:
        self.buffered_bytes = self._live.buffered_bytes
        self.peak_buffered_bytes = self._live.peak_buffered_bytes

    def enqueue_realtime(self, chunk: bytes, *, age: float = 0.0) -> None:
        """Ingest without an await so browser delays cannot fill aiortc's queue."""
        if not self.realtime:
            raise RuntimeError("Real-time output has not been selected")
        if len(chunk) % 2:
            raise ValueError("Live PCM16 output ended with an incomplete sample")
        if self._closed:
            return
        now = asyncio.get_running_loop().time()
        dropped = self._live.dropped_samples
        for offset in range(0, len(chunk), FRAME_BYTES):
            self._live.append(chunk[offset : offset + FRAME_BYTES], now, age)
        self._sync_live()
        self._live_changed.set()
        if self._live.dropped_samples != dropped:
            self._report()

    def _report(self, *, closing: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not closing and now - self._last_report < 5:
            return
        self._last_report = now
        log = logger.info if closing else logger.warning
        log(
            "Live audio output timing: voice_session_id=%s closing=%d buffered_bytes=%d peak_buffered_bytes=%d "
            "queue_waits=%d queue_wait_ms=%.1f queue_wait_peak_ms=%.1f "
            "pacing_delays=%d pacing_late_ms=%.1f pacing_peak_ms=%.1f "
            "realtime=%d dropped_samples=%d recoveries=%d source_age_peak_ms=%.1f",
            voice_session_identity(self.voice_session_id),
            int(closing),
            self.buffered_bytes,
            self.peak_buffered_bytes,
            self.queue_wait.count,
            self.queue_wait.total_ms,
            self.queue_wait.peak_ms,
            self.pacing_late.delayed,
            self.pacing_late.total_ms,
            self.pacing_late.peak_ms,
            int(self.realtime),
            self._live.dropped_samples,
            self._live.recoveries,
            self._live.age_peak_ms,
        )

    async def write(self, chunk: bytes) -> None:
        if self.realtime:
            self.enqueue_realtime(chunk)
            return
        if len(chunk) % 2:
            raise ValueError("Live PCM16 output ended with an incomplete sample")
        epoch = self._epoch
        async with self._writer:
            for offset in range(0, len(chunk), FRAME_BYTES):
                part = chunk[offset : offset + FRAME_BYTES]
                async with self._condition:
                    while not self._closed and epoch == self._epoch and self.buffered_bytes + len(part) > BUFFER_BYTES:
                        self.blocked_writes += 1
                        started = asyncio.get_running_loop().time()
                        try:
                            await self._condition.wait()
                        finally:
                            if self.queue_wait.observe(asyncio.get_running_loop().time() - started):
                                self._report()
                    if self._closed or epoch != self._epoch:
                        return
                    self._frames.append(part)
                    self.buffered_bytes += len(part)
                    self.peak_buffered_bytes = max(self.peak_buffered_bytes, self.buffered_bytes)
                    self._condition.notify_all()

    async def packets(self) -> AsyncIterator[OutputPacket]:
        self._packets_started = True
        if self.realtime:
            async with aclosing(self._realtime_packets()) as packets:
                async for packet in packets:
                    yield packet
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            async with self._condition:
                await self._condition.wait_for(lambda: self._closed or self._interrupted or bool(self._frames))
                if self._interrupted:
                    self._interrupted = False
                    packet = OutputPacket(interrupted=True)
                    deadline = loop.time()
                elif self._closed:
                    return
                else:
                    packet = OutputPacket(self._frames.popleft())
                    self.buffered_bytes -= len(packet.data)
                    self._condition.notify_all()
                epoch, changed = self._epoch, self._changed
            if packet.interrupted:
                yield packet
                continue
            now = loop.time()
            if now - deadline > 0.1:
                self.clock_rebases += 1
                deadline = now
            if deadline > now:
                try:
                    async with asyncio.timeout(deadline - now):
                        await changed.wait()
                except TimeoutError:
                    pass
                if self.pacing_late.observe(loop.time() - deadline):
                    self._report()
            if self._closed or epoch != self._epoch:
                continue
            # An event-loop stall must not grant a burst of old timing credit.
            if loop.time() - deadline > 0.1:
                self.clock_rebases += 1
                deadline = loop.time()
            self.sent_samples += len(packet.data) // 2
            yield packet
            deadline += len(packet.data) / _BYTES_PER_SECOND

    async def _realtime_packets(self) -> AsyncGenerator[OutputPacket, None]:
        """RTP already has a clock; do not pace accumulated speech a second time."""
        loop = asyncio.get_running_loop()
        while True:
            dropped = self._live.dropped_samples
            self._live.expire(loop.time())
            self._sync_live()
            if self._live.dropped_samples != dropped:
                self._report()
            if self._interrupted or self._live.reset_pending:
                self._interrupted = False
                self._live.reset_pending = False
                yield OutputPacket(interrupted=True)
            elif self._closed:
                return
            elif self._live.frames:
                data = self._live.pop()
                self._sync_live()
                self.sent_samples += len(data) // 2
                # No await exists between selecting and yielding this frame.
                # A later interrupt therefore follows it in WebSocket order.
                yield OutputPacket(data)
            else:
                self._live_changed.clear()
                await self._live_changed.wait()

    async def _clear(self, *, closing: bool = False) -> None:
        async with self._condition:
            if self._closed:
                return
            self._closed = closing
            self._epoch += 1
            self.interrupted_samples += self.buffered_bytes // 2
            self._frames.clear()
            self._live.clear()
            self.buffered_bytes = 0
            self._interrupted = True
            self._changed.set()
            self._changed = asyncio.Event()
            self._condition.notify_all()
            self._live_changed.set()
        if closing:
            self._report(closing=True)
            logger.info(
                "Live audio output closed: voice_session_id=%s sent_samples=%d interrupted_samples=%d peak_buffered_bytes=%d blocked_writes=%d clock_rebases=%d",
                voice_session_identity(self.voice_session_id),
                self.sent_samples,
                self.interrupted_samples,
                self.peak_buffered_bytes,
                self.blocked_writes,
                self.clock_rebases,
            )

    async def interrupt(self) -> None:
        await finish_on_cancel(self._clear())

    async def close(self) -> None:
        await finish_on_cancel(self._clear(closing=True))
