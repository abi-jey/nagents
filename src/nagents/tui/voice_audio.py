"""Optional, bounded PCM devices. PortAudio callbacks never touch Textual.

Raw streams need no NumPy. Devices are opened before allocating a provider call;
all blocking device operations run off the application event loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol
from typing import SupportsBytes
from typing import cast

from nagents._async import join_owned
from nagents.audio import AudioDuplex
from nagents.audio import AudioFormat

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

RATE = 24000
BLOCK = 480  # 20 ms, matching the shared media relay.
MAX_INPUT_BLOCKS = 25
MAX_OUTPUT_BYTES = RATE * 2 * 4


class AudioDeviceError(RuntimeError):
    """Safe, actionable local device errors; never provider exception bodies."""


class _Stream(Protocol):
    @property
    def active(self) -> bool: ...

    def start(self) -> object: ...

    def abort(self) -> object: ...

    def close(self) -> object: ...


class Microphone:
    audio_format = AudioFormat()

    def __init__(self) -> None:
        self._frames: deque[bytes] = deque(maxlen=MAX_INPUT_BLOCKS)
        self._lock = threading.Lock()
        self._ready = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._notified = False
        self.closed = False
        self.muted = False

    def callback(self, data: SupportsBytes, frames: int, timing: object, status: object) -> None:
        with self._lock:
            if self.closed:
                return
            self._frames.append(bytes(frames * 2) if self.muted else bytes(data))
            if not self._notified:
                self._notified = True
                # At most one scheduled wakeup, even if the event loop stalls.
                with suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._ready.set)

    def mute(self, muted: bool) -> None:
        with self._lock:
            self.muted = muted
            self._frames.clear()

    def close(self) -> None:
        with self._lock:
            self.closed = True
            self._frames.clear()
        self._ready.set()

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        while not self.closed:
            await self._ready.wait()
            with self._lock:
                frame = self._frames.popleft() if self._frames else b""
                if not self._frames:
                    self._ready.clear()
                    self._notified = False
                muted = self.muted
            if frame:
                yield bytes(len(frame)) if muted else frame


class Speaker:
    audio_format = AudioFormat()

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self.closed = False

    def callback(self, data: memoryview, frames: int, timing: object, status: object) -> None:
        needed = frames * 2
        with self._lock:
            chunk = bytes(self._buffer[:needed])
            del self._buffer[:needed]
        data[:] = chunk + bytes(needed - len(chunk))

    async def write(self, chunk: bytes) -> None:
        with self._lock:
            if self.closed:
                return
            if len(chunk) % 2 or len(self._buffer) + len(chunk) > MAX_OUTPUT_BYTES:
                raise AudioDeviceError("Voice playback fell behind. Check the output device and reconnect voice.")
            self._buffer.extend(chunk)

    async def interrupt(self) -> None:
        with self._lock:
            self._buffer.clear()

    async def close(self) -> None:
        with self._lock:
            self.closed = True
            self._buffer.clear()


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    input: bool
    output: bool


def devices() -> tuple[AudioDevice, ...]:
    try:
        import sounddevice as sd

        entries = sd.query_devices()
        return tuple(
            AudioDevice(index, str(item["name"]), item["max_input_channels"] > 0, item["max_output_channels"] > 0)
            for index, item in enumerate(entries)
        )
    except (ImportError, OSError):
        raise AudioDeviceError(
            "Install nagents[tui,voice] and the system PortAudio library to use local voice."
        ) from None
    except Exception:
        raise AudioDeviceError(
            "Audio devices could not be listed. Check PortAudio and microphone permissions."
        ) from None


class LocalAudio:
    """One microphone/speaker pair; idempotent, cancellation-owned cleanup."""

    def __init__(self, *, input_device: int = -1, output_device: int = -1) -> None:
        self.input = Microphone()
        self.output = Speaker()
        self.duplex = AudioDuplex(self.input, self.output)
        self.input_device, self.output_device = input_device, output_device
        self._streams: list[_Stream] = []
        self._closed = False
        self._close_lock = asyncio.Lock()

    def _open(self) -> None:
        try:
            import sounddevice as sd

            for factory, device, callback in (
                (sd.RawOutputStream, self.output_device, self.output.callback),
                (sd.RawInputStream, self.input_device, self.input.callback),
            ):
                stream = cast(
                    "_Stream",
                    factory(
                        samplerate=RATE,
                        channels=1,
                        dtype="int16",
                        blocksize=BLOCK,
                        device=None if device == -1 else device,
                        callback=callback,
                    ),
                )
                self._streams.append(stream)
                stream.start()
        except (ImportError, OSError):
            raise AudioDeviceError(
                "Install nagents[tui,voice] and the system PortAudio library to use local voice."
            ) from None
        except Exception:
            raise AudioDeviceError(
                "The microphone or speaker could not open at 24 kHz. Check permissions and /voice devices. "
                "For remote terminals, devices belong to the machine running ngn."
            ) from None

    async def open(self) -> None:
        try:
            await join_owned(asyncio.create_task(asyncio.to_thread(self._open)))
        except BaseException:
            await self.close()
            raise

    async def quiet(self) -> None:
        self.input.mute(True)
        await self.output.interrupt()

    def check(self) -> None:
        if not self._closed and any(not stream.active for stream in self._streams):
            raise AudioDeviceError("A voice audio device stopped. Check the microphone/speaker and reconnect voice.")

    def _close(self) -> None:
        streams, self._streams = self._streams, []
        for stream in reversed(streams):
            # One failed stream must not strand the other device.
            for operation in (stream.abort, stream.close):
                with suppress(Exception):
                    operation()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.input.close()
            await self.output.close()
            await join_owned(asyncio.create_task(asyncio.to_thread(self._close)))
