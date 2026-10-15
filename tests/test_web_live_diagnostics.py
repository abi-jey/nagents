"""Voice timing distinguishes socket, producer and event-loop waits without content."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.web.live_diagnostics import BrowserAudioHealth
from nagents.web.live_diagnostics import Timing
from nagents.web.live_diagnostics import voice_session_identity
from nagents.web.live_output import BUFFER_BYTES
from nagents.web.live_output import BrowserOutput
from tests.test_web_live_input_clock import ClockLoop
from tests.test_web_live_input_clock import pcm
from tests.test_web_live_input_clock import run

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from nagents.web.live_output import OutputPacket


def test_timing_counts_total_and_peak_without_retaining_samples() -> None:
    timing = Timing()
    assert not timing.observe(-0.01)
    assert not timing.observe(0.02)
    assert timing.observe(0.15)
    assert timing.count == 3 and timing.delayed == 1
    assert timing.total_ms == pytest.approx(170)
    assert timing.peak_ms == pytest.approx(150)
    assert set(vars(timing)) == {"count", "delayed", "total_ms", "peak_ms"}


def test_audio_diagnostic_id_is_only_an_opaque_application_identifier() -> None:
    assert voice_session_identity("a" * 32) == "a" * 32
    for value in ("private\ncontent", "Bearer sensitive", "a" * 33, 123, None):
        assert voice_session_identity(value) == "unavailable"


def test_audio_health_rate_limits_anomalies_and_always_reports_final_totals(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 10.0
    monkeypatch.setattr("nagents.web.live_diagnostics.monotonic", lambda: now)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    health = BrowserAudioHealth()
    health.sent(0.08, 480)
    health.lag(0.12)
    health.sent(0.2, 0, completed=False)
    assert len(caplog.records) == 1
    now += 5
    health.lag(0.15)
    assert len(caplog.records) == 2
    health.report(closing=True)
    assert len(caplog.records) == 3
    final = caplog.records[-1]
    assert "closing=1 sent_samples=480" in final.message
    assert "loop_delays=2" in final.message and "slow_sends=2 incomplete_sends=1" in final.message
    assert "loop_peak_ms=150.0" in final.message and "send_peak_ms=200.0" in final.message
    assert isinstance(final.args, tuple) and final.args[0] == "unavailable"
    assert all(type(value) in {int, float} for value in final.args[1:])


def test_owned_audio_heartbeat_observes_blocking_and_cancels_cleanly() -> None:
    async def scenario() -> None:
        health = BrowserAudioHealth()
        watch = asyncio.create_task(health.watch())
        await asyncio.sleep(0)
        # Emulate synchronous processing while the independent 50ms heartbeat
        # is waiting. No wall-clock sleeping or provider/media traffic is needed.
        loop = cast("ClockLoop", asyncio.get_running_loop())
        loop.now += 0.2
        await asyncio.sleep(0.001)
        watch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watch
        assert health.loop.delayed == 1
        assert health.loop.peak_ms == pytest.approx(150)
        assert health.send.count == 0

    run(scenario())


def test_output_queue_wait_is_accounted_without_changing_backpressure() -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        await output.write(pcm(7, BUFFER_BYTES // 2))
        blocked = asyncio.create_task(output.write(pcm(8)))
        await asyncio.sleep(0)
        assert not blocked.done()
        await asyncio.sleep(0.12)
        async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
            assert (await anext(packets)).data == pcm(7)
            await blocked
        assert output.queue_wait.count == output.blocked_writes == 1
        assert output.queue_wait.peak_ms == pytest.approx(120)
        assert output.buffered_bytes == BUFFER_BYTES
        await output.close()

    run(scenario())
