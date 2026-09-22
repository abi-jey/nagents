"""Inspect original input samples alongside Live; keep acoustic evidence separate from transcripts."""

import asyncio
import struct
from collections.abc import AsyncIterator

from _support import drive
from _support import execute
from _support import microphone
from _support import parser
from _support import voice

from nagents.audio import AudioDuplex
from nagents.audio import AudioFormat
from nagents.audio import AudioInput


class MeteredInput:
    def __init__(self, source: AudioInput) -> None:
        self.source = source
        self.audio_format: AudioFormat = source.audio_format
        self.peak = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.source:
            self.peak = max((abs(value[0]) for value in struct.iter_unpack("<h", chunk)), default=0)
            yield chunk


async def main() -> None:
    args = parser(__doc__ or "Audio evidence").parse_args()
    audio = microphone()
    assert audio.input is not None
    meter = MeteredInput(audio.input)

    async def report() -> None:
        while True:
            await asyncio.sleep(1)
            print("Original PCM peak:", meter.peak, "(not a human/machine classification)")

    task = asyncio.create_task(report())
    try:
        await drive(voice(audio=AudioDuplex(input=meter, output=audio.output)), duration=args.duration)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    execute(main)
