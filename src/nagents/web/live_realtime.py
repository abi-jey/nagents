"""A small real-time PCM reservoir; stale speech must not become a delayed call."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

PCM_BYTES_PER_SECOND = 48000
LIVE_BUFFER_SECONDS = 0.24
LIVE_RECOVERY_SECONDS = 0.08
LIVE_BUFFER_BYTES = int(PCM_BYTES_PER_SECOND * LIVE_BUFFER_SECONDS)
LIVE_RECOVERY_BYTES = int(PCM_BYTES_PER_SECOND * LIVE_RECOVERY_SECONDS)


@dataclass(frozen=True)
class RealtimeFrame:
    data: bytes
    received_at: float


class RealtimeBuffer:
    """Synchronous ingestion never propagates browser backpressure into aiortc.

    Frames retain their order. An overload keeps only recent speech and requests
    an ordered playback reset before it can be consumed. ``received_at`` is a
    local estimate, corrected for observed source timestamp lag, not a claim of
    synchronized clocks between the provider and this machine.
    """

    def __init__(self) -> None:
        self.frames: deque[RealtimeFrame] = deque()
        self.buffered_bytes = 0
        self.peak_buffered_bytes = 0
        self.dropped_samples = 0
        self.recoveries = 0
        self.reset_pending = False
        self.age_peak_ms = 0.0

    def _drop(self) -> None:
        frame = self.frames.popleft()
        self.buffered_bytes -= len(frame.data)
        self.dropped_samples += len(frame.data) // 2

    def recover(self) -> None:
        if not self.reset_pending:
            self.recoveries += 1
        self.reset_pending = True

    def discard(self, samples: int) -> None:
        self.dropped_samples += samples
        self.recover()

    def expire(self, now: float) -> None:
        dropped = False
        while self.frames and now - self.frames[0].received_at > LIVE_BUFFER_SECONDS:
            self._drop()
            dropped = True
        if dropped:
            self.recover()

    def append(self, data: bytes, now: float, age: float = 0.0) -> None:
        self.age_peak_ms = max(self.age_peak_ms, age * 1000)
        self.expire(now)
        if age > LIVE_BUFFER_SECONDS:
            self.discard(len(data) // 2)
            return
        self.frames.append(RealtimeFrame(data, now - age))
        self.buffered_bytes += len(data)
        if self.buffered_bytes > LIVE_BUFFER_BYTES:
            while self.frames and self.buffered_bytes > LIVE_RECOVERY_BYTES:
                self._drop()
            self.recover()
        self.peak_buffered_bytes = max(self.peak_buffered_bytes, self.buffered_bytes)

    def pop(self) -> bytes:
        frame = self.frames.popleft()
        self.buffered_bytes -= len(frame.data)
        return frame.data

    def clear(self) -> int:
        samples = self.buffered_bytes // 2
        self.frames.clear()
        self.buffered_bytes = 0
        self.reset_pending = False
        return samples


class MediaClock:
    """Estimate excess media age relative to the earliest local arrival offset.

    RTP's timestamp clock advances during DTX. Missing or restarted timestamps
    establish a fresh baseline. A small clock-drift allowance avoids interpreting
    different hardware rates as a growing queue during long calls.
    """

    def __init__(self) -> None:
        self.initialized = False
        self.offset = 0.0
        self.last_media = 0.0
        self.last_arrival = 0.0
        self.age_ms = 0.0
        self.age_peak_ms = 0.0
        self.resets = 0
        self.missing = 0

    def observe(self, now: float, media: float, *, valid: bool = True) -> float:
        if not valid:
            self.missing += 1
            self.initialized = False
            self.age_ms = 0.0
            return 0.0
        offset = now - media
        if not self.initialized or media < self.last_media:
            self.resets += int(self.initialized)
            self.offset = offset
            self.initialized = True
        else:
            # Up to 1000ppm is ordinary clock correction, never a multi-second
            # backlog reset. Forward timestamp gaps naturally account for DTX.
            elapsed = max(0.0, now - self.last_arrival)
            self.offset = min(offset, self.offset + elapsed * 0.001)
        self.last_media, self.last_arrival = media, now
        age = max(0.0, offset - self.offset)
        self.age_ms = age * 1000
        self.age_peak_ms = max(self.age_peak_ms, self.age_ms)
        return age
