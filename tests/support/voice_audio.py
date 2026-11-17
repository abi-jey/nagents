"""Device-free playback that still exercises the bounded speaker callback."""

import asyncio

from nagents.audio import AudioDuplex
from nagents.tui.voice_audio import LocalAudio
from nagents.tui.voice_audio import Speaker


class PlaybackSpeaker(Speaker):
    """Consume each decoded frame like a running output device, without timers."""

    def __init__(self) -> None:
        super().__init__()
        self.played = asyncio.Event()
        self.samples = 0

    async def write(self, chunk: bytes) -> None:
        await super().write(chunk)
        if self.closed:
            return
        output = bytearray(len(chunk))
        self.callback(memoryview(output), len(chunk) // 2, None, None)
        assert output == chunk
        self.samples += len(chunk) // 2
        self.played.set()


class PlaybackLocalAudio(LocalAudio):
    async def open(self) -> None:
        # No PortAudio callbacks run in a headless test. A passive Speaker would
        # fill after four seconds of real RTP, turning UI progress into a race
        # against the production overflow limit rather than testing call closure.
        self.output = PlaybackSpeaker()
        self.duplex = AudioDuplex(self.input, self.output)
