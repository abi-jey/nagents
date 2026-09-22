"""Pacing and application-owned playback controls for continuous Live audio."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Protocol
from typing import runtime_checkable

from ..audio import AudioFormat
from ..audio import AudioInput
from ..audio import AudioOutput

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DEFAULT_FORMAT = AudioFormat()


class PacedAudioInput:
    def __init__(self, source: AudioInput, *, silence_tail: bool = True) -> None:
        self.source = source
        self.audio_format = source.audio_format
        self.silence_tail = silence_tail

    async def __aiter__(self) -> AsyncIterator[bytes]:
        fmt = self.audio_format
        deadline = asyncio.get_running_loop().time()
        async for chunk in self.source:
            yield chunk
            deadline += len(chunk) / (fmt.sample_rate * fmt.sample_width * fmt.channels)
            await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        if self.silence_tail:
            async for chunk in SilenceInput(fmt):
                yield chunk


class SilenceInput:
    def __init__(self, audio_format: AudioFormat = DEFAULT_FORMAT) -> None:
        self.audio_format = audio_format

    async def __aiter__(self) -> AsyncIterator[bytes]:
        fmt = self.audio_format
        silence = b"\xff" if fmt.encoding == "audio/pcmu" else b"\xd5" if fmt.encoding == "audio/pcma" else b"\0"
        chunk = silence * (fmt.sample_rate * fmt.sample_width // 50)
        while True:
            yield chunk
            await asyncio.sleep(0.02)


@runtime_checkable
class DrainableOutput(AudioOutput, Protocol):
    async def drain(self) -> None:
        """Wait until the device has played queued frames, not just accepted writes."""


class PlaybackOutput:
    """Gate model audio without blocking input, events or backend execution."""

    def __init__(self, sink: AudioOutput, *, buffer_limit: int = 24000 * 2 * 10) -> None:
        self.sink = sink
        self.audio_format = sink.audio_format
        self.muted = False
        self.holding = False
        self.buffer_limit = buffer_limit
        self.buffer = bytearray()
        self.revision = 0
        self.clip_completed = False
        self.dropped_bytes = 0

    async def write(self, chunk: bytes) -> None:
        if self.muted:
            self.dropped_bytes += len(chunk)
        elif self.holding:
            if len(self.buffer) + len(chunk) > self.buffer_limit:
                await self.pause()
                raise BufferError("Playback review buffer exceeded its limit")
            self.buffer.extend(chunk)
        else:
            await self.sink.write(chunk)

    async def pause(self) -> None:
        self.muted = True
        self.holding = False
        self.revision += 1
        self.buffer.clear()
        await self.sink.interrupt()

    async def interrupt(self) -> None:
        await self.pause()

    def resume(self) -> None:
        self.muted = False
        self.holding = False

    async def hold(self) -> int:
        await self.pause()
        self.muted = False
        self.holding = True
        return self.revision

    async def approve(self, revision: int) -> bool:
        if revision != self.revision or not self.holding:
            return False
        data = bytes(self.buffer)
        self.buffer.clear()
        self.resume()
        await self.sink.write(data)
        return True

    async def play_clip(self, source: AudioInput) -> None:
        if source.audio_format != self.audio_format or not isinstance(self.sink, DrainableOutput):
            raise ValueError("Verified playback needs matching audio formats and a drain-capable output")
        await self.pause()
        self.clip_completed = False
        received = False
        try:
            async for chunk in source:
                received = received or bool(chunk)
                await self.sink.write(chunk)
            if not received:
                raise ValueError("The verified clip contains no audio")
            await self.sink.drain()
            self.clip_completed = True
        finally:
            if not self.clip_completed:
                await self.sink.interrupt()
        # The application explicitly decides when model playback may resume.

    async def close(self) -> None:
        self.buffer.clear()
        await self.sink.close()
