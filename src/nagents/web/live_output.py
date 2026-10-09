"""Bounded, interruption-aware PCM pacing from provider bursts to the browser."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nagents.audio import AudioFormat

from ._async import finish_on_cancel

if TYPE_CHECKING:
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
    """Preserve provider audio with one cumulative clock, never synthetic silence.

    Already-paced RTP waits only if it arrives ahead of that clock. API bursts
    are split before buffering and backpressure their producer at two seconds.
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

    async def write(self, chunk: bytes) -> None:
        if len(chunk) % 2:
            raise ValueError("Live PCM16 output ended with an incomplete sample")
        epoch = self._epoch
        async with self._writer:
            for offset in range(0, len(chunk), FRAME_BYTES):
                part = chunk[offset : offset + FRAME_BYTES]
                async with self._condition:
                    while not self._closed and epoch == self._epoch and self.buffered_bytes + len(part) > BUFFER_BYTES:
                        self.blocked_writes += 1
                        await self._condition.wait()
                    if self._closed or epoch != self._epoch:
                        return
                    self._frames.append(part)
                    self.buffered_bytes += len(part)
                    self.peak_buffered_bytes = max(self.peak_buffered_bytes, self.buffered_bytes)
                    self._condition.notify_all()

    async def packets(self) -> AsyncIterator[OutputPacket]:
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
            if self._closed or epoch != self._epoch:
                continue
            # An event-loop stall must not grant a burst of old timing credit.
            if loop.time() - deadline > 0.1:
                self.clock_rebases += 1
                deadline = loop.time()
            self.sent_samples += len(packet.data) // 2
            yield packet
            deadline += len(packet.data) / _BYTES_PER_SECOND

    async def _clear(self, *, closing: bool = False) -> None:
        async with self._condition:
            if self._closed:
                return
            self._closed = closing
            self._epoch += 1
            self.interrupted_samples += self.buffered_bytes // 2
            self._frames.clear()
            self.buffered_bytes = 0
            self._interrupted = True
            self._changed.set()
            self._changed = asyncio.Event()
            self._condition.notify_all()
        if closing:
            logger.info(
                "Live audio output closed: sent_samples=%d interrupted_samples=%d peak_buffered_bytes=%d blocked_writes=%d clock_rebases=%d",
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
