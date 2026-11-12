"""No microphone access: exercise the actual callback/buffer ownership code."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from nagents.tui.voice_audio import BLOCK
from nagents.tui.voice_audio import MAX_INPUT_BLOCKS
from nagents.tui.voice_audio import MAX_OUTPUT_BYTES
from nagents.tui.voice_audio import AudioDeviceError
from nagents.tui.voice_audio import LocalAudio
from nagents.tui.voice_audio import Microphone
from nagents.tui.voice_audio import Speaker
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import Callable


def test_microphone_backlog_mute_and_closed_callbacks_are_bounded() -> None:
    async def scenario() -> None:
        microphone = Microphone()
        for index in range(MAX_INPUT_BLOCKS * 10):
            microphone.callback(bytes([index % 255, 0]) * BLOCK, BLOCK, None, None)
        assert len(microphone._frames) == MAX_INPUT_BLOCKS
        stream = microphone.__aiter__()
        assert await anext(stream) == bytes([(MAX_INPUT_BLOCKS * 9) % 255, 0]) * BLOCK
        microphone.mute(True)
        microphone.callback(b"\x01\x00" * BLOCK, BLOCK, None, None)
        assert await anext(stream) == bytes(BLOCK * 2)
        microphone.mute(False)
        microphone.callback(b"\x02\x00" * BLOCK, BLOCK, None, None)
        assert await anext(stream) == b"\x02\x00" * BLOCK

        async def next_frame() -> bytes:
            return await anext(stream)

        waiting = asyncio.create_task(next_frame())
        await asyncio.sleep(0)
        microphone.close()
        with pytest.raises(StopAsyncIteration):
            await waiting
        microphone.callback(b"\x03\x00" * BLOCK, BLOCK, None, None)
        assert not microphone._frames
        await stream.aclose()

    asyncio.run(scenario())


def test_speaker_pcm_padding_interrupt_limits_and_idempotent_close() -> None:
    async def scenario() -> None:
        speaker = Speaker()
        await speaker.write(b"\x01\x00" * 2)
        output = bytearray(8)
        speaker.callback(memoryview(output), 4, None, None)
        assert output == b"\x01\x00" * 2 + bytes(4)
        await speaker.write(b"\x02\x00" * 2)
        await speaker.interrupt()
        speaker.callback(memoryview(output), 4, None, None)
        assert output == bytes(8)
        with pytest.raises(AudioDeviceError):
            await speaker.write(b"x")
        with pytest.raises(AudioDeviceError):
            await speaker.write(bytes(MAX_OUTPUT_BYTES + 2))
        assert not speaker._buffer
        await speaker.close()
        await speaker.close()
        await speaker.write(bytes(20))
        assert not speaker._buffer

    asyncio.run(scenario())


class Stream:
    def __init__(self, start: Callable[[], None]) -> None:
        self._start = start
        self.aborted = self.closed = 0

    def start(self) -> None:
        self._start()

    def abort(self) -> None:
        self.aborted += 1

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("failure", ["none", "input", "output", "cancel"])
def test_local_devices_open_off_loop_and_join_partial_startup(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async def scenario() -> None:
        streams: list[Stream] = []
        options: list[dict[str, object]] = []
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()

        def factory(**kwargs: object) -> Stream:
            assert threading.get_ident() != loop_thread
            options.append(kwargs)
            number = len(streams)

            def start() -> None:
                if (failure == "output" and number == 0) or (failure == "input" and number == 1):
                    raise RuntimeError("private device internals")
                if failure == "cancel" and number == 1:
                    entered.set()
                    assert release.wait(HANG_GUARD)

            stream = Stream(start)
            streams.append(stream)
            return stream

        module = ModuleType("sounddevice")
        monkeypatch.setattr(module, "RawInputStream", factory, raising=False)
        monkeypatch.setattr(module, "RawOutputStream", factory, raising=False)
        monkeypatch.setitem(sys.modules, "sounddevice", module)
        audio = LocalAudio(input_device=3, output_device=4)
        opening = asyncio.create_task(audio.open())
        if failure == "cancel":
            assert await asyncio.to_thread(entered.wait, HANG_GUARD)
            opening.cancel()
            await asyncio.sleep(0)
            assert not opening.done()  # The non-cancellable device worker is still owned.
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
        elif failure == "none":
            await opening
            assert options[0]["device"] == 4 and options[1]["device"] == 3
            assert all(option["samplerate"] == 24000 and option["blocksize"] == BLOCK for option in options)
        else:
            with pytest.raises(AudioDeviceError) as caught:
                await opening
            assert "private" not in str(caught.value)
        await audio.close()
        await audio.close()
        assert streams and all(stream.closed == stream.aborted == 1 for stream in streams)
        assert audio.input.closed and audio.output.closed

    asyncio.run(scenario())
