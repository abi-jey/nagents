"""SDK input pacing starts with audio availability, not source setup time."""

from __future__ import annotations

import asyncio
import math
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.audio import AudioFormat
from nagents.live import audio
from nagents.live.audio import PacedAudioInput

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator


class Clock:
    """Replace only the pacer's time/sleep calls; source and consumer share time."""

    def __init__(self, quantum: float = 0) -> None:
        self.now = 0.0
        self.quantum = quantum

    def get_running_loop(self) -> Clock:
        return self

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += max(0, delay)
        if delay > 0 and self.quantum:
            self.now = math.ceil(self.now / self.quantum) * self.quantum
        await asyncio.sleep(0)


class Input:
    def __init__(self, clock: Clock, chunks: list[tuple[float, bytes]], audio_format: AudioFormat) -> None:
        self.clock = clock
        self.chunks = chunks
        self.audio_format = audio_format

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, chunk in self.chunks:
            await self.clock.sleep(delay)
            yield chunk


FORMATS = [
    AudioFormat(),
    AudioFormat(encoding="audio/pcmu", sample_rate=8000, sample_width=1),
    AudioFormat(encoding="audio/pcma", sample_rate=8000, sample_width=1),
]


@pytest.mark.parametrize("fmt", FORMATS, ids=["pcm16", "pcmu", "pcma"])
@pytest.mark.parametrize("initial_delay", [0, 0.25])
def test_buffered_audio_pacing_begins_at_first_available_chunk(
    monkeypatch: pytest.MonkeyPatch, fmt: AudioFormat, initial_delay: float
) -> None:
    clock = Clock()
    monkeypatch.setattr(audio, "asyncio", clock)

    async def scenario() -> None:
        rate = fmt.sample_rate * fmt.sample_width * fmt.channels
        chunks = [bytes([index + 1]) * round(seconds * rate) for index, seconds in enumerate([0.01, 0.02, 0.04])]
        source = Input(clock, [(initial_delay, chunks[0]), (0, chunks[1]), (0, chunks[2])], fmt)
        paced = PacedAudioInput(source, silence_tail=False)
        received = []
        async for chunk in paced:
            received.append((clock.time(), chunk))
        assert [chunk for _, chunk in received] == chunks
        assert [when for when, _ in received] == pytest.approx(
            [initial_delay, initial_delay + 0.01, initial_delay + 0.03]
        )
        assert clock.time() == pytest.approx(initial_delay + 0.07)

    asyncio.run(scenario())


def test_empty_chunks_do_not_arm_the_clock_but_actual_silent_pcm_does(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(audio, "asyncio", clock)

    async def scenario() -> None:
        chunks = [b"", b"", bytes(960), b"\x01\x02" * 480]
        source = Input(clock, [(0.25, chunks[0]), (0.25, chunks[1]), (0.25, chunks[2]), (0, chunks[3])], AudioFormat())
        received = []
        async for chunk in PacedAudioInput(source, silence_tail=False):
            received.append((clock.time(), chunk))
        assert [chunk for _, chunk in received] == chunks
        assert [when for when, _ in received] == pytest.approx([0.25, 0.5, 0.75, 0.77])
        assert clock.time() == pytest.approx(0.79)

    asyncio.run(scenario())


def test_consumer_time_counts_toward_existing_cumulative_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(audio, "asyncio", clock)

    async def scenario() -> None:
        source = Input(clock, [(0.25, bytes(960)), (0, bytes(960)), (0, bytes(960))], AudioFormat())
        times = []
        async for _ in PacedAudioInput(source, silence_tail=False):
            times.append(clock.time())
            await clock.sleep(0.012)  # Playback/send work consumes part of each frame.
        assert times == pytest.approx([0.25, 0.27, 0.29])
        assert clock.time() == pytest.approx(0.31)

    asyncio.run(scenario())


def test_coarse_timer_does_not_reset_clock_or_accumulate_delay_for_each_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(1 / 64)
    monkeypatch.setattr(audio, "asyncio", clock)

    async def scenario() -> None:
        source = Input(clock, [(0.25 if index == 0 else 0, bytes(960)) for index in range(40)], AudioFormat())
        times = []
        async for _ in PacedAudioInput(source, silence_tail=False):
            times.append(clock.time())
        for index, when in enumerate(times):
            due = 0.25 + index * 0.02
            assert due - 1e-8 <= when < due + clock.quantum + 1e-8
        assert 1.05 <= clock.time() < 1.05 + clock.quantum

    asyncio.run(scenario())


@pytest.mark.parametrize("fmt", FORMATS, ids=["pcm16", "pcmu", "pcma"])
def test_silence_tail_follows_full_duration_of_the_final_delayed_chunk(
    monkeypatch: pytest.MonkeyPatch, fmt: AudioFormat
) -> None:
    clock = Clock()
    monkeypatch.setattr(audio, "asyncio", clock)

    async def scenario() -> None:
        size = fmt.sample_rate * fmt.sample_width // 50
        source = Input(clock, [(0.25, b"\x01" * size), (0, b"\x02" * size * 2)], fmt)
        paced = PacedAudioInput(source)
        received = []
        async with aclosing(cast("AsyncGenerator[bytes, None]", paced.__aiter__())) as frames:
            for _ in range(4):
                chunk = await anext(frames)
                received.append((clock.time(), chunk))
        silence = b"\xff" if fmt.encoding == "audio/pcmu" else b"\xd5" if fmt.encoding == "audio/pcma" else b"\0"
        assert [chunk for _, chunk in received] == [b"\x01" * size, b"\x02" * size * 2, silence * size, silence * size]
        assert [when for when, _ in received] == pytest.approx([0.25, 0.27, 0.31, 0.33])

    asyncio.run(scenario())


@pytest.mark.parametrize("silence_tail", [False, True])
def test_source_with_no_audio_preserves_eof_and_optional_silence_tail(
    monkeypatch: pytest.MonkeyPatch, silence_tail: bool
) -> None:
    clock = Clock()
    monkeypatch.setattr(audio, "asyncio", clock)

    async def scenario() -> None:
        source = Input(clock, [(0.25, b"")], AudioFormat())
        paced = PacedAudioInput(source, silence_tail=silence_tail)
        async with aclosing(cast("AsyncGenerator[bytes, None]", paced.__aiter__())) as frames:
            assert await anext(frames) == b""
            if silence_tail:
                assert await anext(frames) == bytes(960)
                assert clock.time() == 0.25
                assert await anext(frames) == bytes(960)
                assert clock.time() == 0.27
            else:
                with pytest.raises(StopAsyncIteration):
                    await anext(frames)

    asyncio.run(scenario())


@pytest.mark.parametrize("before_audio", [False, True])
def test_cancellation_stops_waiting_without_draining_buffered_audio(before_audio: bool) -> None:
    async def scenario() -> None:
        waiting = asyncio.Event()
        released = asyncio.Event()
        first = asyncio.Event()
        received: list[bytes] = []
        advanced = False

        class Source:
            audio_format = AudioFormat()

            async def __aiter__(self) -> AsyncIterator[bytes]:
                nonlocal advanced
                if before_audio:
                    try:
                        waiting.set()
                        await asyncio.Event().wait()
                    finally:
                        released.set()
                yield bytes(48000)  # One second of PCM; cancellation must interrupt its pacing wait.
                advanced = True
                yield bytes(960)

        async def consume() -> None:
            async for chunk in PacedAudioInput(Source(), silence_tail=False):
                received.append(chunk)
                first.set()

        task = asyncio.create_task(consume())
        try:
            async with asyncio.timeout(5):
                await (waiting if before_audio else first).wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not advanced
            assert len(received) == (0 if before_audio else 1)
            if before_audio:
                assert released.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_empty_chunks_keep_the_event_loop_cooperative() -> None:
    async def scenario() -> None:
        progressed = asyncio.Event()

        class Source:
            audio_format = AudioFormat()

            async def __aiter__(self) -> AsyncIterator[bytes]:
                for _ in range(10):
                    yield b""

        async def other_work() -> None:
            await asyncio.sleep(0)
            progressed.set()

        worker = asyncio.create_task(other_work())
        try:
            async for chunk in PacedAudioInput(Source(), silence_tail=False):
                assert chunk == b""
            assert progressed.is_set()
        finally:
            await worker

    asyncio.run(scenario())
