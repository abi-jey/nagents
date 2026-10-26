"""Bounded audio RTP counters, separated from decoded PCM progression."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import islice
from time import monotonic

from aiortc.stats import RTCInboundRtpStreamStats

from .diagnostics import voice_session_identity

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class InboundAudioCounters:
    streams: int = 0
    received: int = 0
    lost: int = 0
    jitter: int = 0


def inbound_audio_counters(report: object) -> InboundAudioCounters:
    """Read only known audio inbound fields; never project the full stats object."""
    streams = received = lost = jitter = 0
    if not isinstance(report, dict):
        return InboundAudioCounters()
    for entry in islice(report.values(), 8):
        if type(entry) is not RTCInboundRtpStreamStats or entry.kind != "audio":
            continue
        if (
            type(entry.packetsReceived) is not int
            or not 0 <= entry.packetsReceived <= 2**63 - 1
            or type(entry.packetsLost) is not int
            or not -(2**31) <= entry.packetsLost <= 2**31 - 1
            or type(entry.jitter) is not int
            or not 0 <= entry.jitter <= 2**32 - 1
        ):
            continue
        streams += 1
        received += entry.packetsReceived
        lost += entry.packetsLost
        jitter = max(jitter, entry.jitter)
    return InboundAudioCounters(streams, received, lost, jitter)


class ReceiverAudioHealth:
    def __init__(self, session_id: str) -> None:
        self.session_id = voice_session_identity(session_id)
        self.frames = 0
        self.output_samples = 0
        self.sample_rate = 0
        self.receiving = False
        self.writing = False
        self.failures = 0
        self.resets = 0
        self.samples = 0
        self.current = InboundAudioCounters()
        self.received_delta = self.lost_delta = self.jitter_delta = 0
        self.frame_delta = self.output_delta = 0
        self.interval_ms = 0.0
        self._frames = self._output_samples = 0
        self._sampled_at = 0.0
        self._reported_at = float("-inf")
        self._stalled = False

    def observe(self, report: object) -> None:
        now = monotonic()
        next_counters = inbound_audio_counters(report)
        reset = self.samples > 0 and next_counters.received < self.current.received
        self.resets += int(reset)
        self.received_delta = next_counters.received - self.current.received if not reset else 0
        self.lost_delta = next_counters.lost - self.current.lost if not reset else 0
        self.jitter_delta = next_counters.jitter - self.current.jitter
        self.current = next_counters
        self.frame_delta = self.frames - self._frames
        self.output_delta = self.output_samples - self._output_samples
        self.interval_ms = (now - self._sampled_at) * 1000 if self.samples else 0.0
        self._frames, self._output_samples = self.frames, self.output_samples
        stalled = self.samples > 0 and (self.received_delta == 0 or self.frame_delta == 0)
        changed = stalled != self._stalled
        self._stalled = stalled
        self._sampled_at = now
        self.samples += 1
        # One sample/second during gaps makes packet-vs-frame progress visible.
        # Healthy calls emit only every five seconds, plus transitions and close.
        if stalled or changed or now - self._reported_at >= 5:
            self.report()

    def report(self, *, closing: bool = False) -> None:
        now = monotonic()
        self._reported_at = now
        logger.info(
            "Live audio receiver timing: voice_session_id=%s closing=%d stats_samples=%d "
            "sample_interval_ms=%.1f sample_age_ms=%.1f streams=%d packets_received=%d "
            "packets_delta=%d packets_lost=%d lost_delta=%d jitter_ticks=%d jitter_delta=%d "
            "frames=%d frame_delta=%d output_samples=%d output_delta=%d sample_rate=%d "
            "awaiting_receive=%d awaiting_write=%d stats_failures=%d counter_resets=%d",
            self.session_id,
            int(closing),
            self.samples,
            self.interval_ms,
            max(0.0, now - self._sampled_at) * 1000 if self.samples else 0.0,
            self.current.streams,
            self.current.received,
            self.received_delta,
            self.current.lost,
            self.lost_delta,
            self.current.jitter,
            self.jitter_delta,
            self.frames,
            self.frame_delta,
            self.output_samples,
            self.output_delta,
            self.sample_rate,
            int(self.receiving),
            int(self.writing),
            self.failures,
            self.resets,
        )
