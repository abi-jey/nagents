"""
Reference sounddevice adapters for the Realtime duplex protocols.

The nagents library ships the :class:`~nagents.realtime.AudioInput` and
:class:`~nagents.realtime.AudioOutput` protocols plus file/in-memory reference
implementations. It deliberately does not depend on a capture/playback backend,
so a real microphone/speaker adapter is provided here on top of ``sounddevice``
(a dev dependency).

Usage:
    from audio_adapters import SoundDeviceAudioInput, SoundDeviceAudioOutput

    mic = SoundDeviceAudioInput()
    speaker = SoundDeviceAudioOutput()
    async for event in agent.run_voice(mic, speaker, auto_commit=False):
        ...
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any

import sounddevice as sd

from nagents.realtime import AudioFormat

logger = logging.getLogger(__name__)


class SoundDeviceAudioInput:
    """Streams live microphone audio as raw PCM16 chunks.

    Uses a callback-based ``sounddevice.RawInputStream``. Audio frames are
    marshalled onto the event loop with ``call_soon_threadsafe`` and consumed
    by the async iterator, so no extra threads are leaked on shutdown.
    """

    def __init__(
        self,
        sample_rate: int = 24000,
        blocksize: int = 3200,
        device: int | str | None = None,
    ):
        """
        Args:
            sample_rate: Microphone sample rate in Hz.
            blocksize: Frames captured per callback (each frame is one int16 sample).
            device: Optional sounddevice input device index or name.
        """
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._device = device
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(encoding="audio/pcm", sample_rate=self._sample_rate, channels=1, sample_width=2)

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            logger.warning("sounddevice input status: %s", status)
        if self._loop is not None:
            # RawInputStream passes a cffi buffer (not a numpy array), so
            # convert it to bytes before queuing.
            self._loop.call_soon_threadsafe(self._queue.put_nowait, bytes(indata))

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self._loop = asyncio.get_running_loop()
        with sd.RawInputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=self._callback,
        ):
            while True:
                yield await self._queue.get()


class SoundDeviceAudioOutput:
    """Plays model audio through the speakers as raw PCM16 chunks.

    Uses a callback-based ``sounddevice.RawOutputStream`` with an internal
    byte buffer, so :meth:`write` never blocks the event loop and
    :meth:`interrupt` can drop unplayed audio instantly. The callback drains
    the buffer on PortAudio's thread and emits silence when it is empty.
    """

    def __init__(
        self,
        sample_rate: int = 24000,
        blocksize: int = 3200,
        device: int | str | None = None,
    ):
        """
        Args:
            sample_rate: Playback sample rate in Hz.
            blocksize: Frames rendered per callback (each frame is one int16 sample).
            device: Optional sounddevice output device index or name.
        """
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._device = device
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(encoding="audio/pcm", sample_rate=self._sample_rate, channels=1, sample_width=2)

    def _callback(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            logger.warning("sounddevice output status: %s", status)
        needed = frames * 2  # mono int16
        with self._lock:
            n = min(needed, len(self._buffer))
            chunk = bytes(self._buffer[:n])
            del self._buffer[:n]
        if n < needed:
            chunk += b"\x00" * (needed - n)
        outdata[:] = chunk

    async def write(self, chunk: bytes) -> None:
        if self._stream is None:
            self._loop = asyncio.get_running_loop()
            self._stream = sd.RawOutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device,
                callback=self._callback,
            )
            self._stream.start()

        with self._lock:
            self._buffer.extend(chunk)

    async def interrupt(self) -> None:
        """Drop all buffered/unplayed audio immediately."""
        with self._lock:
            self._buffer.clear()

    async def close(self) -> None:
        if self._stream is not None and self._loop is not None:
            await self._loop.run_in_executor(None, self._stream.stop)
            await self._loop.run_in_executor(None, self._stream.close)
            self._stream = None
            with self._lock:
                self._buffer.clear()
