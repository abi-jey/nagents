"""Server-owned Codex media peer; browsers only exchange PCM with ngn.

This module is loaded only when a login voice call starts. aiortc and PyAV are
optional web dependencies, not requirements for the provider-agnostic library.
The native login provisioning and delegation protocol live in live_login.py.
"""

from __future__ import annotations

import asyncio
from fractions import Fraction
from typing import TYPE_CHECKING

from aiortc import AudioStreamTrack
from aiortc import MediaStreamTrack
from aiortc import RTCBundlePolicy
from aiortc import RTCConfiguration
from aiortc import RTCPeerConnection
from aiortc import RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from av import AudioResampler

from .live_login import LoginVoiceError

if TYPE_CHECKING:
    from nagents.audio import AudioInput
    from nagents.audio import AudioOutput

SAMPLE_RATE = 24000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_BUFFER_BYTES = SAMPLE_RATE * 2  # At most one second of unconsumed microphone audio.
CONNECT_SECONDS = 15.0


class _MicrophoneTrack(AudioStreamTrack):
    """A monotonic 20 ms PCM clock, including silence before browser attachment."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.silent = False
        self._started = 0.0
        self._samples = 0

    def append(self, chunk: bytes) -> None:
        if self.silent:
            return
        if len(chunk) % 2 or len(self.buffer) + len(chunk) > MAX_BUFFER_BYTES:
            raise LoginVoiceError("Microphone audio could not be relayed in time. Reconnect voice.")
        self.buffer.extend(chunk)

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        now = asyncio.get_running_loop().time()
        if not self._started:
            self._started = now
        due = self._started + self._samples / SAMPLE_RATE
        # Never burst old frames after the event loop stalls; retain timestamps
        # while resuming a paced clock instead of sending a backlog at once.
        if now - due > 0.1:
            self._started = now - self._samples / SAMPLE_RATE
            due = now
        await asyncio.sleep(max(0, due - now))
        if self.readyState != "live":
            raise MediaStreamError
        count = min(FRAME_BYTES, len(self.buffer)) if not self.silent else 0
        pcm = bytes(self.buffer[:count]) + bytes(FRAME_BYTES - count)
        del self.buffer[:count]
        frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.pts = self._samples
        self._samples += FRAME_SAMPLES
        return frame


class ChatGPTMediaRelay:
    """Own the provider media connection and all its audio workers.

    ``quiet`` stops microphone delivery and browser playback immediately but
    retains the peer's silent RTP clock while the sideband confirms closure.
    Only ``aclose`` tears down the peer. No provider credentials or SDP leave
    this server, and no public API-key transport is used as a fallback.
    """

    def __init__(self, source: AudioInput, output: AudioOutput) -> None:
        self._source, self._output = source, output
        self._track = _MicrophoneTrack()
        # Provider candidates are supplied by the native SDP answer. Avoid
        # aiortc's default third-party STUN service; browser egress is never used.
        self._peer = RTCPeerConnection(RTCConfiguration(iceServers=[], bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
        # aiortc serializes audio before application in SDP. Create the audio
        # transport first as well so MAX_BUNDLE retains the negotiated master.
        self._peer.addTrack(self._track)
        self._channel = self._peer.createDataChannel("oai-events")
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()
        self._failure = ""
        self._quiet = False
        self._closed = False
        self._receiving = False
        self._workers: list[asyncio.Task[None]] = []
        self._peer.on("connectionstatechange", self._state_changed)
        self._peer.on("track", self._remote_track)
        self._channel.on("open", self._mark_ready)
        self._channel.on("close", self._channel_closed)

    def _fail(self, message: str) -> None:
        if not self._quiet and not self._closed and not self._failure:
            self._failure = message
            self._failed.set()

    def _mark_ready(self) -> None:
        if self._peer.connectionState == "connected" and self._channel.readyState == "open":
            self._ready.set()

    def _state_changed(self) -> None:
        self._mark_ready()
        if self._peer.connectionState in {"closed", "failed"}:
            self._fail("The ChatGPT voice media connection was interrupted. Reconnect voice.")

    def _channel_closed(self) -> None:
        self._fail("The ChatGPT voice media connection was interrupted. Reconnect voice.")

    def _worker(self, task: asyncio.Task[None]) -> None:
        self._workers.append(task)

        def finished(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self._fail(str(error) if isinstance(error, LoginVoiceError) else "ChatGPT audio relay was interrupted.")

        task.add_done_callback(finished)

    async def _microphone(self) -> None:
        async for chunk in self._source:
            if self._quiet:
                return
            self._track.append(chunk)
        self._fail("The microphone audio stream ended. Reconnect voice.")

    def _remote_track(self, track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            return
        if self._receiving:
            self._fail("ChatGPT voice returned an unexpected audio stream.")
            return
        self._receiving = True
        self._worker(asyncio.create_task(self._playback(track), name="web-chatgpt-media-output"))

    async def _playback(self, track: MediaStreamTrack) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        while not self._closed:
            try:
                frame = await track.recv()
            except MediaStreamError:
                self._fail("The ChatGPT voice audio stream ended. Reconnect voice.")
                return
            if not isinstance(frame, AudioFrame):
                raise LoginVoiceError("ChatGPT voice returned an invalid audio frame.")
            for converted in resampler.resample(frame):
                if not self._quiet:
                    # PyAV planes may include alignment padding, which is not
                    # audio and must not be sent to the browser's sample clock.
                    await self._output.write(bytes(converted.planes[0])[: converted.samples * 2])

    async def offer(self) -> str:
        self._worker(asyncio.create_task(self._microphone(), name="web-chatgpt-media-input"))
        await self._peer.setLocalDescription(await self._peer.createOffer())
        description = self._peer.localDescription
        if description is None:
            raise LoginVoiceError("The server could not prepare ChatGPT voice audio.")
        return description.sdp

    async def connect(self, answer: str) -> None:
        await self._peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        ready = asyncio.create_task(self._ready.wait())
        failed = asyncio.create_task(self._failed.wait())
        try:
            await asyncio.wait({ready, failed}, timeout=CONNECT_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            if self._failed.is_set():
                raise LoginVoiceError(self._failure)
            if not self._ready.is_set():
                raise LoginVoiceError("The server could not connect ChatGPT voice audio. Reconnect voice.")
        finally:
            ready.cancel()
            failed.cancel()
            await asyncio.gather(ready, failed, return_exceptions=True)

    async def wait(self) -> None:
        await self._failed.wait()
        raise LoginVoiceError(self._failure)

    async def quiet(self) -> None:
        self._quiet = True
        self._track.silent = True
        self._track.buffer.clear()
        await self._output.interrupt()

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.quiet()
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._track.stop()
        await self._peer.close()
