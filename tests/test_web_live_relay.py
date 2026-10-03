"""Offline PCM/provider-peer tests for server-owned ChatGPT voice media."""

from __future__ import annotations

import asyncio
from array import array
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiohttp import web
from aiortc import MediaStreamTrack
from aiortc import RTCConfiguration
from aiortc import RTCPeerConnection
from aiortc import RTCSessionDescription
from av import AudioFrame

from nagents.audio import AudioFormat
from nagents.provider.openai import CodexCredentials
from nagents.web import live_runtime as runtime
from nagents.web.live_login import LoginVoiceConfig
from nagents.web.live_login import LoginVoiceError
from nagents.web.live_relay import FRAME_BYTES
from nagents.web.live_relay import MAX_BUFFER_BYTES
from nagents.web.live_relay import ChatGPTMediaRelay
from nagents.web.live_relay import _MicrophoneTrack
from nagents.web.live_runtime import LiveService
from nagents.web.live_runtime import _BrowserInput
from tests.test_web_live_login import ANSWER
from tests.test_web_live_login import OFFER
from tests.test_web_live_login import config
from tests.test_web_live_login import upstream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable

    from nagents.agent import Agent
    from nagents.audio import AudioInput
    from nagents.audio import AudioOutput


class Source:
    audio_format = AudioFormat()

    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield await self.frames.get()


class Output:
    audio_format = AudioFormat()

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.received = asyncio.Event()

    async def write(self, chunk: bytes) -> None:
        self.frames.append(chunk)
        if max((abs(sample) for sample in array("h", chunk)), default=0) > 500:
            self.received.set()

    async def interrupt(self) -> None:
        self.frames.clear()

    async def close(self) -> None:
        await self.interrupt()


def pcm(value: int, samples: int) -> bytes:
    return array("h", [value] * samples).tobytes()


def test_microphone_track_paces_partial_frames_and_quiet_clears_speech() -> None:
    async def scenario() -> None:
        track = _MicrophoneTrack()
        track.append(pcm(1234, 240))
        first = await track.recv()
        second = await track.recv()
        assert first.pts == 0 and second.pts == 480
        assert first.sample_rate == 24000 and first.samples == second.samples == 480
        assert bytes(first.planes[0]) == pcm(1234, 240) + bytes(480)
        assert bytes(second.planes[0]) == bytes(FRAME_BYTES)
        with pytest.raises(LoginVoiceError, match="relayed in time"):
            track.append(bytes(MAX_BUFFER_BYTES + 2))
        with pytest.raises(LoginVoiceError):
            track.append(b"x")
        track.stop()

    asyncio.run(scenario())


def test_cancelled_login_creation_collects_delayed_call_id_and_finalizes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        allocated, release = asyncio.Event(), asyncio.Event()
        requests: list[str] = []
        commands: list[str] = []
        media_closed = asyncio.Event()

        class Media:
            def __init__(self, source: AudioInput, output: AudioOutput) -> None:
                pass

            async def offer(self) -> str:
                return OFFER

            async def connect(self, answer: str) -> None:
                pytest.fail("A stopped call must not start the media handshake")

            async def wait(self) -> None:
                await asyncio.Event().wait()

            async def quiet(self) -> None:
                pass

            async def aclose(self) -> None:
                assert commands == ["session.close"]
                media_closed.set()

        async def handle(request: web.Request) -> web.StreamResponse:
            requests.append(request.path)
            if request.method == "POST":
                await request.json()
                allocated.set()
                await release.wait()
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_pending"})
            assert request.path == "/v1/live/rtc_pending"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                event = message.json()
                commands.append(event["type"])
                assert not media_closed.is_set()
                await socket.send_json({"type": "session.closed"})
                break
            return socket

        def forbidden_factory(voice: str) -> Agent:
            pytest.fail("Login cancellation must not retry through an API-key provider")

        monkeypatch.setattr("nagents.web.live_relay.ChatGPTMediaRelay", Media)
        async with upstream(monkeypatch, handle):
            service = LiveService(forbidden_factory, login_factory=lambda voice: config())
            creating = asyncio.create_task(service.create_stream())
            try:
                async with asyncio.timeout(3):
                    await allocated.wait()
                    identifier = service.active_session_id
                    creating.cancel()
                    done, _ = await asyncio.wait({creating}, timeout=0.02)
                    assert not done, "Cancellation must collect the already-allocated call's response"
                    assert service.active_session_id == identifier
                    assert (await service.snapshot(identifier))["status"] == "closing"
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await creating
                    snapshot = await service.snapshot(identifier)
                    assert snapshot["status"] == "closed"
                    assert "Finalization confirmed." in str(snapshot["message"])
                    assert requests == ["/calls", "/v1/live/rtc_pending"]
                    assert commands == ["session.close"] and media_closed.is_set()
                    assert not service.active_session_id
            finally:
                release.set()
                await asyncio.gather(creating, return_exceptions=True)
                await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("master_candidates_only", [False, True])
def test_local_provider_peer_exchanges_real_encoded_audio_and_quiet_keeps_peer_alive(
    master_candidates_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        source, output = Source(), Output()
        relay = ChatGPTMediaRelay(source, output)
        provider = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        provider_voice = _MicrophoneTrack()
        provider_voice.append(pcm(2000, 24000))
        provider.addTrack(provider_voice)
        heard = asyncio.Event()
        readers: list[asyncio.Task[None]] = []

        async def listen(track: MediaStreamTrack) -> None:
            while True:
                frame = await track.recv()
                assert isinstance(frame, AudioFrame)
                assert frame.format.name == "s16"
                samples = bytes(frame.planes[0])[: frame.samples * len(frame.layout.channels) * 2]
                if max(abs(sample) for sample in array("h", samples)) > 500:
                    heard.set()

        def incoming(track: MediaStreamTrack) -> None:
            readers.append(asyncio.create_task(listen(track)))

        provider.on("track", incoming)
        try:
            async with asyncio.timeout(8):
                offer = await relay.offer()
                assert "m=audio" in offer and "m=application" in offer
                await provider.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
                if master_candidates_only:
                    # An ICE-lite provider answers checks instead of rescuing a
                    # missing candidate list by initiating its own connectivity
                    # checks. Retain real UDP/STUN/DTLS/audio in this fixture.
                    transport = provider.getTransceivers()[0].receiver.transport.transport
                    ice = transport._connection
                    monkeypatch.setattr(ice, "check_periodic", lambda: not ice._check_list_done)
                await provider.setLocalDescription(await provider.createAnswer())
                assert provider.localDescription is not None
                answer = provider.localDescription.sdp
                if master_candidates_only:
                    # The actual native Codex answer advertises candidates only
                    # on the BUNDLE master; application shares that transport.
                    application = False
                    lines: list[str] = []
                    for line in answer.splitlines():
                        if line.startswith("m="):
                            application = line.startswith("m=application ")
                        if not (application and line.startswith(("a=candidate:", "a=end-of-candidates"))):
                            lines.append(line)
                    answer = ("\r\n".join(lines) + "\r\n").replace("t=0 0\r\n", "t=0 0\r\na=ice-lite\r\n")
                await relay.connect(answer)
                source.frames.put_nowait(pcm(3000, 12000))
                await heard.wait()
                await output.received.wait()
                assert all(len(frame) % 2 == 0 and len(frame) <= 960 for frame in output.frames)
                await relay.quiet()
                assert relay._peer.connectionState == "connected"
                assert relay._track.readyState == "live" and relay._track.silent
                assert not relay._track.buffer and not output.frames
                source.frames.put_nowait(pcm(4000, 12000))
                await asyncio.sleep(0.08)
                assert not output.frames and not relay._track.buffer
        finally:
            await relay.aclose()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await provider.close()
        assert relay._peer.connectionState == "closed"
        assert all(worker.done() for worker in relay._workers)

    asyncio.run(scenario())


def test_relay_reports_remote_media_failure_and_closes_blocked_input() -> None:
    async def scenario() -> None:
        relay = ChatGPTMediaRelay(Source(), Output())
        await relay.offer()
        try:
            relay._channel_closed()
            with pytest.raises(LoginVoiceError, match="interrupted"):
                await relay.wait()
        finally:
            await relay.aclose()
        assert all(worker.done() for worker in relay._workers)

    asyncio.run(scenario())


def test_rtp_clock_handles_browser_jitter_without_adding_a_second_silence_stream() -> None:
    async def scenario() -> None:
        source = _BrowserInput(fill_gaps=False)
        source.connected.set()
        relay = ChatGPTMediaRelay(source, Output())
        produced = 0
        peak = 0

        async def browser() -> None:
            nonlocal produced
            # The correct 24 kHz input rate, arriving in 40 ms batches. A second
            # 20 ms padding clock overflows the unchanged 1-second cap in ~2s.
            for _ in range(65):
                await asyncio.sleep(0.04)
                for _ in range(2):
                    source.frames.put_nowait(bytes(FRAME_BYTES))
                    produced += FRAME_BYTES

        async def sender() -> None:
            nonlocal peak
            while True:
                peak = max(peak, len(relay._track.buffer))
                await relay._track.recv()

        feeding = asyncio.create_task(browser())
        pumping = asyncio.create_task(relay._microphone())
        sending = asyncio.create_task(sender())
        try:
            async with asyncio.timeout(5):
                done, _ = await asyncio.wait({feeding, pumping}, return_when=asyncio.FIRST_COMPLETED)
                if pumping in done:
                    pumping.result()
                    pytest.fail("Microphone input ended before the browser producer")
                await feeding
                await asyncio.sleep(0.05)
                assert not pumping.done()
                assert produced > MAX_BUFFER_BYTES * 2
                assert peak <= FRAME_BYTES * 4
                assert len(relay._track.buffer) <= FRAME_BYTES * 2
        finally:
            for task in (feeding, pumping, sending):
                task.cancel()
            await asyncio.gather(feeding, pumping, sending, return_exceptions=True)
            await relay.aclose()

    asyncio.run(scenario())


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("cancel_during_connect", [False, True])
def test_login_stream_owns_media_until_finalization_and_cleans_cancelled_setup(
    monkeypatch: pytest.MonkeyPatch, cancel_during_connect: bool
) -> None:
    async def scenario() -> None:
        timeline: list[str] = []
        closing = asyncio.Event()
        connecting = asyncio.Event()
        ready = asyncio.Event()
        if not cancel_during_connect:
            ready.set()

        class Media:
            def __init__(self, source: AudioInput, output: AudioOutput) -> None:
                assert source.audio_format == output.audio_format == AudioFormat()
                assert isinstance(source, _BrowserInput) and not source.fill_gaps

            async def offer(self) -> str:
                timeline.append("offer")
                return "v=0\r\nserver-only-offer"

            async def connect(self, answer: str) -> None:
                assert answer == "v=0\r\nprovider-only-answer"
                connecting.set()
                await ready.wait()

            async def wait(self) -> None:
                await asyncio.Event().wait()

            async def quiet(self) -> None:
                timeline.append("quiet")

            async def aclose(self) -> None:
                assert "finalized" in timeline
                timeline.append("peer-closed")

        class Connection:
            def __init__(self, config: LoginVoiceConfig) -> None:
                assert config.model == "gpt-live-1-codex"
                self.identifier = ""
                self.finalized = False

            async def provision(self, sdp: str) -> str:
                assert sdp == "v=0\r\nserver-only-offer"
                self.identifier = "rtc_server_owned"
                return "v=0\r\nprovider-only-answer"

            async def events(self) -> AsyncIterator[dict[str, object]]:
                yield {"type": "connection.attached", "session": {"id": self.identifier}}
                await closing.wait()
                self.finalized = True
                timeline.append("finalized")
                yield {"type": "session.closed"}

            async def close(self) -> None:
                assert "quiet" in timeline and "peer-closed" not in timeline
                timeline.append("close-request")
                closing.set()

            async def aclose(self) -> None:
                assert self.finalized

        async def credentials() -> CodexCredentials:
            return CodexCredentials("fixture-token", "fixture-account")

        async def backend(text: str) -> str:
            return text

        def forbidden_factory(voice: str) -> Agent:
            pytest.fail("A ChatGPT voice relay must never instantiate the API-key provider")

        monkeypatch.setattr("nagents.web.live_relay.ChatGPTMediaRelay", Media)
        monkeypatch.setattr(runtime, "ChatGPTLiveConnection", Connection)
        config = LoginVoiceConfig(credentials, "gpt-live-1-codex", "cove", "Instructions", backend)
        service = LiveService(forbidden_factory, login_factory=lambda voice: config)
        creating = asyncio.create_task(service.create_stream())
        await connecting.wait()
        if cancel_during_connect:
            creating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await creating
        else:
            result = await creating
            assert result == {"session_id": service.active_session_id, "model": config.model, "voice": config.voice}
            snapshot = await service.close(cast("str", result["session_id"]))
            assert snapshot["status"] == "closed" and "confirmed" in str(snapshot["message"])
        await service.shutdown()
        assert timeline == ["offer", "quiet", "close-request", "finalized", "peer-closed"]
        assert not service.active_session_id

    asyncio.run(scenario())
