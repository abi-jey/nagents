"""
Audio duplex adapters for the Realtime speech-to-speech interface.

The Realtime API is full-duplex: audio flows into the session (user speech)
and out of the session (model speech) simultaneously. These protocols define
the two ends of that channel, and each adapter codifies the audio
format/quality it works with via :class:`AudioFormat`:

- :class:`AudioInput` — an async source of raw audio chunks (microphone,
  telephony buffer, file, ...). Its :attr:`audio_format` describes what it
  produces and is used to configure the session's *input* audio.
- :class:`AudioOutput` — a sink for raw audio chunks (speaker, file, ...). Its
  :attr:`audio_format` describes what it expects and is used to configure the
  session's *output* audio.

Both are structural :class:`~typing.Protocol` s, so any object implementing the
required methods (or any async generator of ``bytes`` for input) can be passed
to the agent without subclassing. Concrete implementations for files and
in-memory buffers are included as reference implementations and for testing.
"""

import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AudioFormat:
    """
    Codifies the audio format/quality of a duplex channel.

    Attributes:
        encoding: Wire encoding. ``"audio/pcm"`` is 16-bit linear PCM and is
            the default; ``"audio/pcmu"`` selects 8-bit mu-law (telephony).
        sample_rate: Sample rate in Hz (e.g. 24000, 8000).
        channels: Number of channels (Realtime input must be mono).
        sample_width: Bytes per sample (2 = 16-bit).
    """

    encoding: str = "audio/pcm"
    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2


class AudioInput(Protocol):
    """
    Async source of raw audio chunks (mono, matching :attr:`audio_format`).

    Implementations may be async generators of ``bytes`` or any object with an
    ``__aiter__`` returning an async iterator of ``bytes``.
    """

    @property
    def audio_format(self) -> AudioFormat:
        """The format/quality of the audio this input produces."""
        ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...


class AudioOutput(Protocol):
    """
    Sink for raw audio chunks produced by the model.

    Implementations play the audio (speaker), stream it (telephony), or store
    it (file). :meth:`write` is called for each output chunk, :meth:`interrupt`
    is called when playback should drop any buffered audio (user interruption),
    and :meth:`close` is called once when the conversation ends so resources
    can be released.
    """

    @property
    def audio_format(self) -> AudioFormat:
        """The format/quality of the audio this output expects."""
        ...

    async def write(self, chunk: bytes) -> None:
        """Write a chunk of raw audio."""

    async def interrupt(self) -> None:
        """Drop any buffered/unplayed audio (user interruption)."""

    async def close(self) -> None:
        """Finalize the output (flush, close files, stop playback)."""


@dataclass(frozen=True)
class AudioDuplex:
    """
    A full-duplex audio channel pairing an input source with an output sink.

    Either end may be ``None`` to disable it:
    - ``output=None`` -> capture/recognize audio but produce no playback
      (transcripts only).
    - ``input=None`` -> no microphone; drive responses by other means (text).

    Attributes:
        input: Optional :class:`AudioInput` source (e.g. microphone).
        output: Optional :class:`AudioOutput` sink (e.g. speaker).
    """

    input: AudioInput | None = None
    output: AudioOutput | None = None


class BytesAudioInput:
    """Streams raw audio bytes from an in-memory buffer in fixed-size chunks.

    Useful for telephony buffers or tests where audio is already captured in
    memory.
    """

    def __init__(self, pcm_data: bytes, chunk_size: int = 3200, audio_format: AudioFormat | None = None):
        """
        Args:
            pcm_data: Raw audio bytes (mono, matching ``audio_format``).
            chunk_size: Number of bytes yielded per chunk.
            audio_format: Format/quality of the audio (defaults to PCM16 24 kHz).
        """
        self._data = pcm_data
        self._chunk_size = chunk_size
        self._audio_format = audio_format or AudioFormat()

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._data), self._chunk_size):
            yield self._data[offset : offset + self._chunk_size]


class WAVFileAudioInput:
    """Streams audio from a WAV file in fixed-size chunks.

    The audio format is derived from the file header. The entire file is
    decoded into memory up front, so this is best suited to short utterances,
    examples, and tests rather than long recordings.
    """

    def __init__(self, path: str | Path, chunk_size: int = 3200):
        """
        Args:
            path: Path to a WAV file.
            chunk_size: Number of bytes yielded per chunk.

        Raises:
            ValueError: If the file is not mono 16-bit PCM (the Realtime API
                expects mono PCM16 input).
        """
        self._path = Path(path)
        self._chunk_size = chunk_size
        with wave.open(str(self._path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise ValueError(
                    f"WAV file '{path}' must be mono 16-bit PCM. "
                    f"Got channels={wf.getnchannels()}, sampwidth={wf.getsampwidth()}"
                )
            self._audio_format = AudioFormat(
                encoding="audio/pcm",
                sample_rate=wf.getframerate(),
                channels=wf.getnchannels(),
                sample_width=wf.getsampwidth(),
            )
            self._data = wf.readframes(wf.getnframes())

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._data), self._chunk_size):
            yield self._data[offset : offset + self._chunk_size]


class BytesAudioOutput:
    """Collects raw audio chunks into an in-memory buffer."""

    def __init__(self, audio_format: AudioFormat | None = None) -> None:
        self._audio_format = audio_format or AudioFormat()
        self._buffer = bytearray()

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def write(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass

    @property
    def data(self) -> bytes:
        """The accumulated raw audio bytes."""
        return bytes(self._buffer)


class WAVFileAudioOutput:
    """Writes raw audio chunks to a WAV file when :meth:`close` is called."""

    def __init__(self, path: str | Path, audio_format: AudioFormat | None = None):
        self._path = Path(path)
        self._audio_format = audio_format or AudioFormat()
        self._buffer = bytearray()

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def write(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        fmt = self._audio_format
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._path), "wb") as wf:
            wf.setnchannels(fmt.channels)
            wf.setsampwidth(fmt.sample_width)
            wf.setframerate(fmt.sample_rate)
            wf.writeframes(bytes(self._buffer))


class NullAudioOutput:
    """Discards all audio chunks (useful when only transcripts are needed)."""

    def __init__(self, audio_format: AudioFormat | None = None) -> None:
        self._audio_format = audio_format or AudioFormat()

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def write(self, chunk: bytes) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass
