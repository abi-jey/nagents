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
    an ordered playback reset before it can be consumed. ``received_at`` is
    strictly the local enqueue time. Remote timestamp offsets are diagnostic
    observations and must never decide whether valid received audio is admitted.
    """

    def __init__(self) -> None:
        self.frames: deque[RealtimeFrame] = deque()
        self.buffered_bytes = 0
        self.peak_buffered_bytes = 0
        self.dropped_samples = 0
        self.recoveries = 0
        self.reset_pending = False

    def _drop(self) -> None:
        frame = self.frames.popleft()
        self.buffered_bytes -= len(frame.data)
        self.dropped_samples += len(frame.data) // 2

    def recover(self) -> None:
        if not self.reset_pending:
            self.recoveries += 1
        self.reset_pending = True

    def expire(self, now: float) -> None:
        dropped = False
        while self.frames and now - self.frames[0].received_at > LIVE_BUFFER_SECONDS:
            self._drop()
            dropped = True
        if dropped:
            self.recover()

    def append(self, data: bytes, now: float) -> None:
        self.expire(now)
        self.frames.append(RealtimeFrame(data, now))
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
    """Observe source clock offset changes for diagnostics, never admission.

    RTP's timestamp clock advances during DTX. Missing or restarted timestamps
    establish a fresh baseline. A small drift allowance reduces ordinary noise,
    but larger source rate/offset differences can still appear as growing lag
    while every decoded frame arrives on time. This is not an audio age clock.
    """

    def __init__(self) -> None:
        self.initialized = False
        self.offset = 0.0
        self.last_media = 0.0
        self.last_arrival = 0.0
        self.offset_lag_ms = 0.0
        self.offset_lag_peak_ms = 0.0
        self.resets = 0
        self.missing = 0

    def observe(self, now: float, media: float, *, valid: bool = True) -> float:
        if not valid:
            self.missing += 1
            self.initialized = False
            self.offset_lag_ms = 0.0
            return 0.0
        offset = now - media
        if not self.initialized or media < self.last_media:
            self.resets += int(self.initialized)
            self.offset = offset
            self.initialized = True
        else:
            # A diagnostic baseline, not a bound on all valid provider clocks.
            # Forward timestamp gaps naturally account for DTX.
            elapsed = max(0.0, now - self.last_arrival)
            self.offset = min(offset, self.offset + elapsed * 0.001)
        self.last_media, self.last_arrival = media, now
        lag = max(0.0, offset - self.offset)
        self.offset_lag_ms = lag * 1000
        self.offset_lag_peak_ms = max(self.offset_lag_peak_ms, self.offset_lag_ms)
        return lag
