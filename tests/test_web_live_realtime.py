"""Real-time playback bounds local queues without trusting a remote media clock."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from fractions import Fraction
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiortc import MediaStreamTrack
from av import AudioFrame

from nagents.web.live_output import BrowserOutput
from nagents.web.live_realtime import LIVE_BUFFER_BYTES
from nagents.web.live_realtime import LIVE_BUFFER_SECONDS
from nagents.web.live_realtime import LIVE_RECOVERY_BYTES
from nagents.web.live_realtime import MediaClock
from nagents.web.live_realtime import RealtimeBuffer
from nagents.web.live_relay import ChatGPTMediaRelay
from tests.test_web_live_input_clock import ClockLoop
from tests.test_web_live_input_clock import pcm
from tests.test_web_live_input_clock import run
from tests.test_web_live_relay import Source

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from nagents.web.live_output import OutputPacket


def test_reservoir_keeps_recent_whole_frames_and_accounts_for_every_dropped_sample() -> None:
    reservoir = RealtimeBuffer()
    chunks = [pcm(index) for index in range(13)]
    for chunk in chunks:
        reservoir.append(chunk, 10)
    assert reservoir.reset_pending and reservoir.recoveries == 1
    assert reservoir.buffered_bytes <= LIVE_RECOVERY_BYTES
    assert reservoir.peak_buffered_bytes <= LIVE_BUFFER_BYTES
    retained = b"".join(frame.data for frame in reservoir.frames)
    assert retained == b"".join(chunks[-4:])
    assert reservoir.dropped_samples * 2 + len(retained) == sum(map(len, chunks))
    reservoir.expire(10.25)
    assert not reservoir.frames and reservoir.dropped_samples == 13 * 480


def test_source_offset_diagnostics_handle_dtx_missing_and_restarted_timestamps_and_clock_drift() -> None:
    clock = MediaClock()
    assert clock.observe(10, 0) == 0
    assert clock.observe(10.2, 0.02) == pytest.approx(0.1798)
    assert clock.observe(20, 10) == 0, "DTX advances RTP time during a conversational pause"
    assert clock.observe(21, 0) == 0, "a timestamp restart establishes a new baseline"
    assert clock.resets == 1
    assert clock.observe(22, 0, valid=False) == 0
    assert clock.missing == 1
    assert clock.observe(23, 0) == 0
    for second in range(1, 3601):
        assert clock.observe(23 + second, second * 0.9997) == 0


def test_source_interval_diagnostics_separate_media_gaps_from_delivery_stalls() -> None:
    clock = MediaClock()
    clock.observe(10, 0, duration=0.02)
    clock.observe(10.12, 0.12, duration=0.02)
    assert clock.arrival_interval_ms == pytest.approx(120)
    assert clock.source_interval_ms == pytest.approx(120)
    assert clock.source_gap_ms == pytest.approx(100)
    assert clock.transport_late_ms == pytest.approx(0)
    clock.observe(10.24, 0.14, duration=0.02)
    assert clock.arrival_interval_ms == pytest.approx(120)
    assert clock.source_interval_ms == pytest.approx(20)
    assert clock.source_gap_ms == pytest.approx(0)
    assert clock.transport_late_ms == pytest.approx(100)
    clock.observe(10.26, 0, duration=0.02)
    assert clock.resets == 1
    assert clock.source_gap_ms == clock.transport_late_ms == 0
    clock.observe(10.28, 0.02, valid=False, duration=0.02)
    assert clock.missing == 1
    assert clock.arrival_interval_ms == clock.source_gap_ms == clock.transport_late_ms == 0


@pytest.mark.parametrize("stall", [0.15, 0.25, 0.35])
def test_three_minute_call_with_repeated_processing_stalls_bounds_local_backlog(stall: float) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, ClockLoop)
        output = BrowserOutput()
        output.use_realtime_clock()
        index = 0
        delivered: list[int] = []
        resets = 0
        async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
            for second in range(180):
                # The source keeps producing while the event loop is occupied.
                # Each batch is what the public remote-track recv now releases.
                loop.now = second + stall
                while index * 0.02 <= loop.time():
                    output.enqueue_realtime(pcm(index))
                    index += 1
                while output.buffered_bytes or output._live.reset_pending:
                    packet = await anext(packets)
                    if packet.interrupted:
                        resets += 1
                    else:
                        number = int.from_bytes(packet.data[:2], "little", signed=True)
                        delivered.append(number)
                        assert loop.time() - number * 0.02 <= LIVE_BUFFER_SECONDS + 1e-8
                # Clean arrival resumes after each stall, without any second
                # pacing clock carrying latency into the next exchange.
                while index * 0.02 < second + 1:
                    loop.now = index * 0.02
                    output.enqueue_realtime(pcm(index))
                    assert (await anext(packets)).data == pcm(index)
                    delivered.append(index)
                    index += 1
        assert delivered == sorted(set(delivered)), "retained speech cannot repeat or change order"
        if stall > LIVE_BUFFER_SECONDS:
            assert resets > 100 and output._live.dropped_samples > 0
        else:
            assert resets == 0 and output._live.dropped_samples == 0
        assert output.peak_buffered_bytes <= LIVE_BUFFER_BYTES
        assert output.blocked_writes == 0 and output.clock_rebases == 0
        assert output.sent_samples + output._live.dropped_samples == index * 480
        await output.close()

    run(scenario())


def test_slow_consumer_expires_old_speech_then_resets_before_recent_audio() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, ClockLoop)
        output = BrowserOutput()
        output.use_realtime_clock()
        output.enqueue_realtime(pcm(1))
        loop.now = 0.35
        output.enqueue_realtime(pcm(2))
        async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
            assert (await anext(packets)).interrupted
            assert (await anext(packets)).data == pcm(2)
            output.enqueue_realtime(pcm(3))
            await output.interrupt()
            output.enqueue_realtime(pcm(4))
            assert (await anext(packets)).interrupted
            assert (await anext(packets)).data == pcm(4)
            assert loop.time() == 0.35, "already-paced media must never acquire another pacing deadline"
            pending = asyncio.create_task(anext(packets))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert output._live.dropped_samples == 480 and output.interrupted_samples == 480
        await output.close()
        output.enqueue_realtime(pcm(5))
        assert output.buffered_bytes == 0

    run(scenario())


def test_realtime_close_wakes_waiting_sender_without_leaking_queued_audio() -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        output.use_realtime_clock()
        async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
            pending = asyncio.create_task(anext(packets))
            await asyncio.sleep(0)
            await output.close()
            assert (await pending).interrupted
            with pytest.raises(StopAsyncIteration):
                await anext(packets)

    run(scenario())


def test_public_receiver_drains_without_browser_backpressure_and_yields_fairly(monkeypatch: pytest.MonkeyPatch) -> None:
    class Track(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.frames: asyncio.Queue[AudioFrame] = asyncio.Queue()

        async def recv(self) -> AudioFrame:
            return await self.frames.get()

        def add(self, index: int) -> None:
            frame = AudioFrame(format="s16", layout="mono", samples=480)
            frame.planes[0].update(pcm(index))
            frame.sample_rate = 24000
            frame.time_base = Fraction(1, 24000)
            frame.pts = index * 480
            self.frames.put_nowait(frame)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, ClockLoop)
        monkeypatch.setattr("nagents.web.live_relay.monotonic", loop.time)
        output, track = BrowserOutput(), Track()
        relay = ChatGPTMediaRelay(Source(), output)
        assert output.realtime
        worker = asyncio.create_task(relay._playback(track))
        try:
            track.add(0)
            await asyncio.sleep(0)
            loop.now = 0.4
            for index in range(1, 21):
                track.add(index)
            await asyncio.sleep(0)
            assert 0 < track.frames.qsize() < 20, "backlog ingestion yields to control and microphone workers"
            for _ in range(4):
                await asyncio.sleep(0)
            assert track.frames.empty(), "a browser that is not reading must not block remote-track draining"
            assert output.buffered_bytes <= LIVE_BUFFER_BYTES and output._live.dropped_samples > 0
            assert output.blocked_writes == 0
            assert relay._media_clock.offset_lag_peak_ms > 300
            await relay.quiet()
            track.add(21)
            await asyncio.sleep(0)
            assert output.buffered_bytes == 0
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await relay.aclose()

    run(scenario())


def test_paced_audio_survives_persistent_remote_timestamp_rate_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    class Track(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.frames: asyncio.Queue[AudioFrame] = asyncio.Queue()

        async def recv(self) -> AudioFrame:
            return await self.frames.get()

        def add(self, index: int) -> None:
            frame = AudioFrame(format="s16", layout="mono", samples=480)
            frame.planes[0].update(pcm(index))
            frame.sample_rate = 24000
            frame.time_base = Fraction(1, 24000)
            # Delivery and PCM duration are 20ms. Source timestamps progress
            # 1% more slowly, recreating the measured false-age growth without
            # creating any actual queue or event-loop backlog.
            frame.pts = index * 480 * 99 // 100
            self.frames.put_nowait(frame)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, ClockLoop)
        monkeypatch.setattr("nagents.web.live_relay.monotonic", loop.time)
        output, track = BrowserOutput(), Track()
        relay = ChatGPTMediaRelay(Source(), output)
        worker = asyncio.create_task(relay._playback(track))
        try:
            async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
                for index in range(9000):
                    loop.now = index * 0.02
                    track.add(index)
                    packet = await anext(packets)
                    assert not packet.interrupted, "source timestamp drift cannot trigger a playback reset"
                    assert packet.data == pcm(index), "every on-time PCM frame must survive in order"
            assert relay._media_clock.offset_lag_peak_ms > 1500
            assert output.sent_samples == 9000 * 480
            assert output._live.dropped_samples == 0 and output._live.recoveries == 0
            assert output.blocked_writes == 0 and output.clock_rebases == 0
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await relay.aclose()

    run(scenario())
