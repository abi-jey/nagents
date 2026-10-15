"""Distinguish missing inbound packets from a decoder/output-frame stall."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from aiortc.stats import RTCInboundRtpStreamStats
from aiortc.stats import RTCStatsReport

from nagents.web.live_relay import ChatGPTMediaRelay
from nagents.web.live_rtp_diagnostics import ReceiverAudioHealth
from nagents.web.live_rtp_diagnostics import inbound_audio_counters
from tests.support.hang_guard import HANG_GUARD
from tests.test_web_live_relay import Output
from tests.test_web_live_relay import Source

if TYPE_CHECKING:
    import pytest
    from aiortc import RTCRtpReceiver


def report(received: int, lost: int = 0, jitter: int = 24, *, kind: str = "audio") -> RTCStatsReport:
    values = RTCStatsReport()
    values.add(
        RTCInboundRtpStreamStats(
            timestamp=datetime.now(UTC),
            type="inbound-rtp",
            id="private-stats-id",
            ssrc=123,
            kind=kind,
            transportId="private-transport-identity",
            packetsReceived=received,
            packetsLost=lost,
            jitter=jitter,
        )
    )
    return values


def test_inbound_snapshot_is_bounded_and_ignores_non_audio_and_invalid_counters() -> None:
    counters = inbound_audio_counters(report(123, -2, 37))
    assert (counters.streams, counters.received, counters.lost, counters.jitter) == (1, 123, -2, 37)
    assert inbound_audio_counters(report(123, kind="video")).streams == 0
    assert inbound_audio_counters(report(-1)).streams == 0
    assert inbound_audio_counters(report(1, jitter=-1)).streams == 0
    assert inbound_audio_counters({"unrelated": object()}).streams == 0
    values = {str(index): next(iter(report(1).values())) for index in range(100)}
    assert inbound_audio_counters(values).streams == 8


def test_receiver_logs_separate_packet_loss_from_decode_stall_and_recovery(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("nagents.web.live_rtp_diagnostics.monotonic", lambda: now)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    health = ReceiverAudioHealth("b" * 32)
    health.frames, health.output_samples, health.sample_rate = 50, 24000, 48000
    health.receiving = True
    health.observe(report(50))
    now += 1
    health.observe(report(100, 2, 48))
    assert health.received_delta == 50 and health.frame_delta == 0
    assert health.lost_delta == 2 and health.jitter_delta == 24
    assert "packets_delta=50" in caplog.records[-1].message
    assert "frame_delta=0" in caplog.records[-1].message
    assert "awaiting_receive=1 awaiting_write=0" in caplog.records[-1].message
    now += 1
    health.observe(report(100, 2, 48))
    assert health.received_delta == 0 and health.frame_delta == 0
    assert "packets_delta=0" in caplog.records[-1].message
    now += 1
    health.frames += 100
    health.output_samples += 48000
    health.observe(report(150, 2, 48))
    assert health.frame_delta == 100 and health.output_delta == 48000
    now += 0.2
    health.report(closing=True)
    assert "closing=1" in caplog.records[-1].message
    assert "sample_age_ms=200.0" in caplog.records[-1].message
    assert "private-stats-id" not in caplog.text
    assert "private-transport-identity" not in caplog.text
    assert "ssrc" not in caplog.text
    for entry in caplog.records:
        assert isinstance(entry.args, tuple) and entry.args[0] == "b" * 32
        assert all(type(value) in {int, float} for value in entry.args[1:])


def test_healthy_receiver_logs_are_sparse_and_counter_reset_is_explicit(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("nagents.web.live_rtp_diagnostics.monotonic", lambda: now)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    health = ReceiverAudioHealth("")
    for index in range(6):
        health.frames += 50
        health.output_samples += 24000
        health.observe(report((index + 1) * 50))
        now += 1
    assert len(caplog.records) == 2
    health.observe(report(2))
    assert health.resets == 1 and health.received_delta == 0


def test_receiver_stats_watcher_is_cancelled_and_joined_with_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        relay = ChatGPTMediaRelay(Source(), Output())
        receiver = relay._peer.getReceivers()[0]
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def blocked_stats(self: RTCRtpReceiver) -> RTCStatsReport:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return RTCStatsReport()

        monkeypatch.setattr(type(receiver), "getStats", blocked_stats)
        worker = asyncio.create_task(relay._watch_receiver(receiver))
        relay._worker(worker)
        try:
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            await relay.aclose()
            assert worker.done() and worker.cancelled() and stopped.is_set()
            assert relay._receiver_health.failures == 0
            assert not relay._failure
        finally:
            await relay.aclose()

    asyncio.run(scenario())
