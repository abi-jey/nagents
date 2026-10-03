"""Exact offline PCM accounting under browser jitter and delayed attachment."""

from __future__ import annotations

import asyncio
import base64
import math
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.audio import AudioDuplex
from nagents.live.runtime import send_audio
from nagents.web.live_runtime import SILENCE_FRAME
from nagents.web.live_runtime import _BrowserInput

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import AsyncGenerator
    from collections.abc import Callable
    from collections.abc import Coroutine


class ClockLoop(asyncio.SelectorEventLoop):
    """Run real asyncio tasks/timeouts with deterministic, optionally coarse time."""

    _ready: deque[asyncio.Handle]
    _scheduled: list[asyncio.TimerHandle]

    def __init__(self, quantum: float = 0) -> None:
        self.now = 0.0
        self.quantum = quantum
        super().__init__()

    def time(self) -> float:
        return self.now

    def _run_once(self) -> None:
        scheduled = [handle.when() for handle in self._scheduled if not handle.cancelled()]
        if not self._ready and scheduled:
            due = min(scheduled)
            if self.quantum:
                due = math.ceil(due / self.quantum) * self.quantum
            self.now = max(self.now, due)
        # asyncio does not expose its selector step in the public type stubs.
        step_name = "_run_once"
        step = cast("Callable[[], None]", getattr(super(), step_name))
        step()


def run(scenario: Coroutine[object, object, None], *, quantum: float = 0) -> None:
    with asyncio.Runner(loop_factory=lambda: ClockLoop(quantum)) as runner:
        runner.run(scenario)


def pcm(number: int, samples: int = 480) -> bytes:
    return number.to_bytes(2, "little", signed=True) * samples


@pytest.mark.parametrize(("interval", "batch"), [(0.0201, 1), (0.04, 2)])
@pytest.mark.parametrize("quantum", [0, 1 / 64])
def test_primary_websocket_paces_jittered_pcm_without_a_second_silence_stream(
    interval: float, batch: int, quantum: float
) -> None:
    async def scenario() -> None:
        source = _BrowserInput()
        expected = [pcm(index + 1000) for index in range(80)]
        received: list[tuple[float, bytes]] = []
        complete = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def send(event: dict[str, object]) -> None:
            data = base64.b64decode(str(event["audio"]), validate=True)
            received.append((loop.time(), data))
            if data == expected[-1]:
                complete.set()

        sender = asyncio.create_task(send_audio(AudioDuplex(input=source), send))
        try:
            # Provider allocation can precede browser audio attachment. It must
            # not accumulate timing credit that flushes the initial PCM queue.
            await asyncio.sleep(2)
            assert not received
            started = loop.time()
            source.connected.set()
            for index in range(0, len(expected), batch):
                due = started + (index // batch + 1) * interval
                await asyncio.sleep(max(0, due - loop.time()))
                for chunk in expected[index : index + batch]:
                    source.frames.put_nowait(chunk)
            async with asyncio.timeout(5):
                await complete.wait()
            payloads = [chunk for _, chunk in received if chunk != SILENCE_FRAME]
            assert payloads == expected
            duration = sum(len(chunk) for _, chunk in received) / 48000
            wall = received[-1][0] - started
            # One emitted frame can extend beyond the last append timestamp;
            # coarse scheduling can shift it by at most one timer quantum.
            assert abs(duration - wall - 0.02) <= quantum + 1e-8
            assert duration <= 1.6 + 0.08, "Network batching fabricated extra silence"
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    run(scenario(), quantum=quantum)


def test_early_queued_frames_wait_and_variable_pcm_sizes_advance_by_sample_duration() -> None:
    async def scenario() -> None:
        source = _BrowserInput()
        source.connected.set()
        chunks = [pcm(1, 240), pcm(2, 960), pcm(3, 480)]
        for chunk in chunks:
            source.frames.put_nowait(chunk)
        times = []
        async with aclosing(cast("AsyncGenerator[bytes, None]", source.__aiter__())) as frames:
            for chunk in chunks:
                assert await anext(frames) == chunk
                times.append(asyncio.get_running_loop().time())
        assert times == pytest.approx([0, 0.01, 0.05])

    run(scenario())


@pytest.mark.parametrize("stall_during_wait", [False, True])
def test_substantial_stall_rebases_without_reordering_or_bursting_queued_pcm(stall_during_wait: bool) -> None:
    async def scenario() -> None:
        source = _BrowserInput()
        source.connected.set()
        chunks = [pcm(index) for index in range(1, 5)]
        for chunk in chunks:
            source.frames.put_nowait(chunk)
        loop = asyncio.get_running_loop()
        assert isinstance(loop, ClockLoop)
        async with aclosing(cast("AsyncGenerator[bytes, None]", source.__aiter__())) as frames:
            assert await anext(frames) == chunks[0]
            if stall_during_wait:
                # Emulate suspension after sleep is armed, before its callback.
                loop.call_later(0.01, setattr, loop, "now", 0.5)
            else:
                loop.now = 0.5
            times = []
            for chunk in chunks[1:]:
                assert await anext(frames) == chunk
                times.append(loop.time())
        assert times == pytest.approx([0.5, 0.52, 0.54])

    run(scenario())


def test_mute_and_empty_capture_share_the_clock_and_resume_preserves_exact_pcm() -> None:
    async def scenario() -> None:
        source = _BrowserInput()
        source.connected.set()
        # Muting sends real zero PCM from the browser; missing packets need the
        # same output schedule, not another independent stream of silence.
        source.frames.put_nowait(SILENCE_FRAME)
        source.frames.put_nowait(SILENCE_FRAME)
        times = []
        async with aclosing(cast("AsyncGenerator[bytes, None]", source.__aiter__())) as frames:
            for _ in range(5):
                assert await anext(frames) == SILENCE_FRAME
                times.append(asyncio.get_running_loop().time())
            source.frames.put_nowait(pcm(3000))
            assert await anext(frames) == pcm(3000)
            times.append(asyncio.get_running_loop().time())
        assert times == pytest.approx([0, 0.02, 0.04, 0.06, 0.08, 0.1])

    run(scenario())


@pytest.mark.parametrize("connected", [False, True])
def test_audio_sender_cancellation_stops_without_flushing_remaining_capture(connected: bool) -> None:
    async def scenario() -> None:
        source = _BrowserInput()
        received: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            received.append(event)

        sender = asyncio.create_task(send_audio(AudioDuplex(input=source), send))
        if connected:
            source.connected.set()
        await asyncio.sleep(0.01)
        before = list(received)
        source.frames.put_nowait(pcm(9))
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        await asyncio.sleep(1)
        assert received == before
        assert len(received) == int(connected)

    run(scenario())


def test_native_rtp_source_stays_unpaced_and_does_not_invent_gap_silence() -> None:
    async def scenario() -> None:
        source = _BrowserInput(fill_gaps=False)
        source.connected.set()
        chunks = [pcm(1), pcm(2)]
        for chunk in chunks:
            source.frames.put_nowait(chunk)
        async with aclosing(cast("AsyncGenerator[bytes, None]", source.__aiter__())) as frames:
            for chunk in chunks:
                assert await anext(frames) == chunk
                assert asyncio.get_running_loop().time() == 0
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.1):
                    await anext(frames)

    run(scenario())
