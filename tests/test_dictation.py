"""Dictation tests use synthetic buffers and fake native/network backends only."""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
import threading
import traceback
import wave
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import aiohttp
import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.dictation import BYTES_PER_SECOND
from nagents.harness.dictation import MAX_RESPONSE_BYTES
from nagents.harness.dictation import REQUEST_SECONDS
from nagents.harness.dictation import DictationError
from nagents.harness.dictation import VoiceDictation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

SECRET = "synthetic-dictation-key-never-display"
BODY_SECRET = "synthetic-server-error-body-never-display"


def wav_bytes(*, frames: int = 1600, channels: int = 1, rate: int = 16000, width: int = 2) -> bytes:
    with BytesIO() as output:
        with wave.open(output, "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(width)
            audio.setframerate(rate)
            audio.writeframes(b"\x01" * frames * channels * width)
        return output.getvalue()


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class FakeMicrophone:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kwargs: dict[str, object] = {}
        self.audio = b"\x01\x00" * 1600
        self.status = False
        self.failure = ""
        self.start_release = threading.Event()
        self.start_release.set()
        self.close_release = threading.Event()
        self.close_release.set()
        self.started = threading.Event()
        self.closed = threading.Event()

    def __call__(self, **kwargs: object) -> FakeMicrophone:
        self.kwargs = kwargs
        self._call("open")
        return self

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure == name:
            raise RuntimeError(BODY_SECRET)

    def start(self) -> None:
        self._call("start")
        assert self.start_release.wait(3), "test did not release fake start"
        callback = cast("Callable[[bytes, int, object, object], None]", self.kwargs["callback"])
        callback(self.audio, len(self.audio) // 2, object(), self.status)
        self.started.set()

    def stop(self) -> None:
        self._call("stop")

    def abort(self) -> None:
        self._call("abort")

    def close(self) -> None:
        self._call("close")
        assert self.close_release.wait(3), "test did not release fake close"
        self.closed.set()


class FakeResponse:
    def __init__(self, network: FakeNetwork) -> None:
        self.network = network
        self.content = self
        self.status = network.status

    async def __aenter__(self) -> FakeResponse:
        self.network.entered.set()
        await self.network.release.wait()
        if self.network.failure:
            raise self.network.failure
        return self

    async def __aexit__(self, *args: object) -> None:
        self.network.response_closed = True

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        self.network.reads += 1
        for offset in range(0, len(self.network.body), size):
            yield self.network.body[offset : offset + size]


class FakeNetwork:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.status = 200
        self.body = json.dumps({"text": "  A synthetic draft.  "}).encode()
        self.failure: Exception | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.closed = False
        self.response_closed = False
        self.reads = 0

    def __call__(self, **kwargs: object) -> FakeNetwork:
        self.options = kwargs
        return self

    async def __aenter__(self) -> FakeNetwork:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    def post(self, endpoint: str, **kwargs: object) -> FakeResponse:
        self.requests.append((endpoint, kwargs))
        return FakeResponse(self)


@pytest.fixture
def microphone(monkeypatch: pytest.MonkeyPatch) -> FakeMicrophone:
    mic = FakeMicrophone()
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=mic))
    monkeypatch.setitem(sys.modules, "numpy", None)
    return mic


@pytest.fixture(autouse=True)
def network(monkeypatch: pytest.MonkeyPatch) -> FakeNetwork:
    fake = FakeNetwork()
    monkeypatch.setattr(aiohttp, "ClientSession", fake)
    monkeypatch.setenv("VOICE_TEST_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unrelated-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-unrelated-openrouter-key")
    return fake


@pytest.fixture
def config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace=tmp_path,
        dictation_enabled=True,
        dictation_api_key_env="VOICE_TEST_KEY",
        dictation_max_seconds=1,
    )


@pytest.mark.parametrize("mode", ["disabled", "demo", "missing-key", "bad-key"])
def test_readiness_never_touches_hardware_or_falls_back(
    config: HarnessConfig, microphone: FakeMicrophone, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    config.auth = "chatgpt"
    if mode == "disabled":
        config.dictation_enabled = False
    elif mode == "demo":
        config.demo = True
    else:
        monkeypatch.setenv("VOICE_TEST_KEY", "" if mode == "missing-key" else "bad\nkey")
    service = VoiceDictation(config)

    async def scenario() -> None:
        for operation in (service.capture(), service.transcribe(wav_bytes())):
            with pytest.raises(DictationError) as error:
                await operation
            assert SECRET not in str(error.value)
        await service.close()

    asyncio.run(scenario())
    assert not microphone.calls
    assert not network.requests


def test_construct_and_check_are_lazy(config: HarnessConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    service = VoiceDictation(config)
    service.check_ready()
    assert not service.capturing
    assert service.elapsed == 0
    assert service.endpoint == "https://api.openai.com/v1/audio/transcriptions"
    asyncio.run(service.close())


def test_capture_stop_produces_bounded_wav_without_upload(
    config: HarnessConfig, microphone: FakeMicrophone, network: FakeNetwork, tmp_path: Path
) -> None:
    async def scenario() -> None:
        service = VoiceDictation(config)
        task = asyncio.create_task(service.capture())
        await wait_until(lambda: service.capturing)
        with pytest.raises(DictationError, match="busy"):
            await service.transcribe(wav_bytes())
        service.stop()
        data = await task
        with wave.open(BytesIO(data), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
            assert audio.readframes(audio.getnframes()) == microphone.audio
        assert microphone.calls == ["open", "start", "stop", "close"]
        assert microphone.kwargs["dtype"] == "int16"
        assert microphone.kwargs["blocksize"] == 1600
        assert microphone.closed.is_set()
        assert not service.capturing
        await service.close()
        await service.close()
        with pytest.raises(DictationError, match="closed"):
            await service.capture()

    asyncio.run(scenario())
    assert not network.requests
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("by_frames", [False, True])
def test_capture_cap_stops_and_closes_without_upload(
    config: HarnessConfig, microphone: FakeMicrophone, network: FakeNetwork, by_frames: bool
) -> None:
    if by_frames:
        microphone.audio = b"\x01\x00" * 32000

    async def scenario() -> None:
        service = VoiceDictation(config)
        async with asyncio.timeout(2):
            result = await service.capture()
        assert 44 < len(result) <= BYTES_PER_SECOND + 44
        if by_frames:
            assert len(result) == BYTES_PER_SECOND + 44
        assert microphone.closed.is_set()
        await service.close()

    asyncio.run(scenario())
    assert not network.requests


@pytest.mark.parametrize("how", ["cancel-task", "cancel-service", "close", "cancel-starting", "cancel-twice"])
def test_cancellation_waits_for_stream_cleanup(
    config: HarnessConfig, microphone: FakeMicrophone, network: FakeNetwork, how: str
) -> None:
    async def scenario() -> None:
        service = VoiceDictation(config)
        microphone.close_release.clear()
        if how == "cancel-starting":
            microphone.start_release.clear()
        task = asyncio.create_task(service.capture())
        await wait_until(lambda: "start" in microphone.calls)
        cleanup: asyncio.Task[None] | asyncio.Task[bytes]
        if how in {"cancel-service", "close"}:
            cleanup = asyncio.create_task(service.cancel() if how == "cancel-service" else service.close())
        else:
            task.cancel()
            cleanup = task
        microphone.start_release.set()
        await wait_until(lambda: "close" in microphone.calls)
        if how == "cancel-twice":
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert not cleanup.done()
        microphone.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if cleanup is not task:
            await cleanup
        assert microphone.closed.is_set()
        assert not service.capturing
        await service.close()

    asyncio.run(scenario())
    assert not network.requests


@pytest.mark.parametrize("failure", ["open", "start", "stop", "close", "overflow", "empty"])
def test_capture_failures_are_safe_and_close(
    config: HarnessConfig, microphone: FakeMicrophone, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    microphone.failure = failure
    microphone.status = failure == "overflow"
    if failure == "empty":
        microphone.audio = b""

    async def scenario() -> None:
        service = VoiceDictation(config)
        task = asyncio.create_task(service.capture())
        if failure in {"stop", "close"}:
            await wait_until(lambda: service.capturing)
            service.stop()
        with pytest.raises(DictationError) as error:
            await task
        assert BODY_SECRET not in "".join(traceback.format_exception(error.value))
        if failure != "open":
            assert "stop" in microphone.calls
            assert "close" in microphone.calls
        if failure == "stop":
            assert "abort" in microphone.calls
        await service.close()

    asyncio.run(scenario())
    assert BODY_SECRET not in caplog.text


def test_unexpected_native_finish_discards_audio_and_closes(
    config: HarnessConfig, microphone: FakeMicrophone, network: FakeNetwork
) -> None:
    async def scenario() -> None:
        service = VoiceDictation(config)
        task = asyncio.create_task(service.capture())
        await wait_until(lambda: service.capturing)
        finished = cast("Callable[[], None]", microphone.kwargs["finished_callback"])
        finished()
        with pytest.raises(DictationError, match="Microphone unavailable"):
            await task
        assert microphone.closed.is_set()
        await service.close()

    asyncio.run(scenario())
    assert not network.requests


@pytest.mark.parametrize("failure", [ImportError(BODY_SECRET), OSError(BODY_SECRET)])
def test_missing_extra_or_portaudio_is_actionable(
    config: HarnessConfig, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    original = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sounddevice":
            raise failure
        return original(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    async def scenario() -> None:
        service = VoiceDictation(config)
        with pytest.raises(DictationError, match=r"voice|PortAudio") as error:
            await service.capture()
        assert BODY_SECRET not in "".join(traceback.format_exception(error.value))
        await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["", "fr"])
def test_transcription_multipart_uses_only_voice_auth_and_fixed_endpoint(
    config: HarnessConfig, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    config.dictation_base_url = "https://voice.example.invalid/compatible/v1/"
    config.dictation_language = language
    config.auth = "chatgpt"
    service = VoiceDictation(config)
    config.dictation_base_url = "https://changed.example.invalid/v1"
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert asyncio.run(service.transcribe(wav_bytes())) == "A synthetic draft."
    assert len(network.requests) == 1
    endpoint, request = network.requests[0]
    assert endpoint == "https://voice.example.invalid/compatible/v1/audio/transcriptions"
    assert request["headers"] == {"Authorization": f"Bearer {SECRET}"}
    assert request["allow_redirects"] is False
    assert network.options["trust_env"] is False
    assert isinstance(network.options["cookie_jar"], aiohttp.DummyCookieJar)
    timeout = cast("aiohttp.ClientTimeout", network.options["timeout"])
    assert timeout.total == REQUEST_SECONDS
    assert timeout.connect == 10
    form = cast("aiohttp.FormData", request["data"])
    fields = {options["name"]: value for options, _, value in form._fields}
    assert fields == {
        "file": wav_bytes(),
        "model": "gpt-4o-mini-transcribe",
        "response_format": "json",
        **({"language": language} if language else {}),
    }
    assert form._fields[0][0]["filename"] == "dictation.wav"
    assert network.closed and network.response_closed


def test_wav_metadata_is_not_uploaded(config: HarnessConfig, network: FakeNetwork) -> None:
    original = wav_bytes()
    metadata = BODY_SECRET.encode()
    chunk = b"JUNK" + len(metadata).to_bytes(4, "little") + metadata
    chunk += b"\x00" * (len(metadata) % 2)
    supplied = original[:4] + (len(original) + len(chunk) - 8).to_bytes(4, "little") + original[8:] + chunk
    asyncio.run(VoiceDictation(config).transcribe(supplied))
    form = cast("aiohttp.FormData", network.requests[0][1]["data"])
    assert form._fields[0][2] == original
    assert metadata not in form._fields[0][2]


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 429, 500])
def test_http_errors_never_read_bodies_follow_redirects_or_retry(
    config: HarnessConfig, network: FakeNetwork, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    network.status = status
    network.body = BODY_SECRET.encode()
    with pytest.raises(DictationError) as error:
        asyncio.run(VoiceDictation(config).transcribe(wav_bytes()))
    assert BODY_SECRET not in str(error.value) + caplog.text
    assert SECRET not in str(error.value) + caplog.text
    assert network.reads == 0
    assert len(network.requests) == 1
    assert network.closed and network.response_closed


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b'{"text": 42}', b'{"text":"  "}', b'{"error":"private"}', b"x" * (MAX_RESPONSE_BYTES + 1)],
)
def test_invalid_response_is_bounded_and_safe(config: HarnessConfig, network: FakeNetwork, body: bytes) -> None:
    network.body = body
    with pytest.raises(DictationError):
        asyncio.run(VoiceDictation(config).transcribe(wav_bytes()))
    assert len(network.requests) == 1
    assert network.closed and network.response_closed


@pytest.mark.parametrize("failure", [TimeoutError(BODY_SECRET), RuntimeError(BODY_SECRET)])
def test_network_failure_has_no_raw_exception_or_retry(
    config: HarnessConfig, network: FakeNetwork, failure: Exception
) -> None:
    network.failure = failure
    with pytest.raises(DictationError) as error:
        asyncio.run(VoiceDictation(config).transcribe(wav_bytes()))
    assert BODY_SECRET not in "".join(traceback.format_exception(error.value))
    assert "not retried" in str(error.value)
    assert len(network.requests) == 1
    assert network.closed


@pytest.mark.parametrize("cancel_task", [False, True])
def test_upload_cancellation_closes_session(config: HarnessConfig, network: FakeNetwork, cancel_task: bool) -> None:
    async def scenario() -> None:
        service = VoiceDictation(config)
        network.release.clear()
        task = asyncio.create_task(service.transcribe(wav_bytes()))
        await network.entered.wait()
        if cancel_task:
            task.cancel()
        else:
            await service.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert network.closed
        assert len(network.requests) == 1
        await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "audio",
    [
        b"",
        b"/etc/shadow",
        "/etc/shadow",
        b"not-a-wav" * 10,
        wav_bytes(rate=24000),
        wav_bytes(channels=2),
        wav_bytes(width=1),
        wav_bytes(frames=16001),
        wav_bytes()[:-2],
        b"x" * (BYTES_PER_SECOND + 4097),
    ],
)
def test_invalid_audio_never_reads_paths_or_uploads(
    config: HarnessConfig, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch, audio: bytes
) -> None:
    def no_open(*args: object, **kwargs: object) -> None:
        pytest.fail("dictation must never open a filesystem path")

    monkeypatch.setattr(builtins, "open", no_open)
    with pytest.raises(DictationError):
        asyncio.run(VoiceDictation(config).transcribe(audio))
    assert not network.requests


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/v1",
        "https://example.com/v1?token=secret",
        "https://example.com/v1#secret",
        "file:///etc/shadow",
        "https://example.com:bad/v1",
    ],
)
def test_invalid_endpoint_is_not_echoed(config: HarnessConfig, url: str) -> None:
    config.dictation_base_url = url
    with pytest.raises(DictationError) as error:
        VoiceDictation(config)
    assert url not in str(error.value)
