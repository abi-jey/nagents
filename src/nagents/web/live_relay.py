"""Server-owned Codex media peer; browsers only exchange PCM with ngn.

This module is loaded only when a login voice call starts. aiortc and PyAV are
optional web dependencies, not requirements for the provider-agnostic library.
The native login provisioning and delegation protocol live in live_login.py.
"""

from __future__ import annotations

import asyncio
import logging
from array import array
from asyncio import sleep
from fractions import Fraction
from time import monotonic
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

from .live_diagnostics import Timing
from .live_diagnostics import log_live_failure
from .live_diagnostics import voice_session_identity
from .live_login import LoginVoiceError
from .live_output import BrowserOutput
from .live_realtime import MediaClock
from .live_rtp_diagnostics import ReceiverAudioHealth

if TYPE_CHECKING:
    from aiortc import RTCRtpReceiver

    from nagents.audio import AudioInput
    from nagents.audio import AudioOutput

    from .live_diagnostics import FailureStage

SAMPLE_RATE = 24000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_BUFFER_BYTES = SAMPLE_RATE * 2  # At most one second of unconsumed microphone audio.
MAX_LATENCY_BYTES = FRAME_BYTES * 10  # Recover to at most 200 ms of recent speech.
DRIFT_RESERVE_BYTES = FRAME_BYTES * 4  # Ordinary delivery jitter may retain 80 ms.
CONNECT_SECONDS = 15.0
logger = logging.getLogger("uvicorn.error")


def _correct_drift(pcm: bytes) -> bytes:
    """Fit 481 samples into 480 without a discontinuous single-sample deletion."""
    samples = array("h", pcm)
    corrected = array("h")
    for index in range(FRAME_SAMPLES):
        offset = index / (FRAME_SAMPLES - 1)
        corrected.append(round(samples[index] * (1 - offset) + samples[index + 1] * offset))
    return corrected.tobytes()


class _MicrophoneTrack(AudioStreamTrack):
    """A monotonic 20 ms PCM clock, including silence before browser attachment."""

    def __init__(self, voice_session_id: str = "") -> None:
        super().__init__()
        self.voice_session_id = voice_session_identity(voice_session_id)
        self.buffer = bytearray()
        self.silent = False
        self._started = 0.0
        self._samples = 0
        self.input_samples = 0
        self.dropped_samples = 0
        self.adjusted_samples = 0
        self.silence_samples = 0
        self.clock_rebases = 0
        self.late_ms = 0.0
        self.peak_buffered_bytes = 0
        self.recoveries = 0
        self._last_report = float("-inf")

    def _drop(self, count: int) -> None:
        self.dropped_samples += count // 2
        self.recoveries += 1
        now = monotonic()
        if now - self._last_report >= 5:
            self._last_report = now
            logger.warning(
                "Live microphone backlog recovered: voice_session_id=%s dropped_samples=%d buffered_samples=%d clock_rebases=%d late_ms=%.0f recoveries=%d",
                self.voice_session_id,
                self.dropped_samples,
                len(self.buffer) // 2,
                self.clock_rebases,
                self.late_ms,
                self.recoveries,
            )

    def append(self, chunk: bytes) -> None:
        if self.silent:
            return
        if len(chunk) % 2:
            raise LoginVoiceError("Microphone PCM audio ended with an incomplete sample. Reconnect voice.")
        self.input_samples += len(chunk) // 2
        size = len(self.buffer) + len(chunk)
        self.peak_buffered_bytes = max(self.peak_buffered_bytes, min(size, MAX_BUFFER_BYTES))
        if size > MAX_BUFFER_BYTES:
            # A browser/network backlog may arrive without yielding. Retain its
            # newest speech without allocating an unbounded temporary buffer.
            keep = max(0, MAX_LATENCY_BYTES - len(chunk))
            self.buffer = self.buffer[-keep:] if keep else bytearray()
            self.buffer.extend(chunk[-MAX_LATENCY_BYTES:])
            self._drop(size - len(self.buffer))
        else:
            self.buffer.extend(chunk)

    def _deadline(self, now: float) -> float:
        due = self._started + self._samples / SAMPLE_RATE
        if now - due > 0.1:
            self.clock_rebases += 1
            self.late_ms += (now - due) * 1000
            self._started = now - self._samples / SAMPLE_RATE
            return now
        return due

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        now = monotonic()
        if not self._started:
            self._started = now
        # Never burst old frames after the event loop stalls; retain timestamps
        # while resuming a paced clock. Check after sleep too: delegation/tool
        # processing may have delayed the scheduled wake-up itself.
        due = self._deadline(now)
        await sleep(max(0, due - now))
        self._deadline(monotonic())
        if self.readyState != "live":
            raise MediaStreamError
        if len(self.buffer) > MAX_LATENCY_BYTES:
            count = len(self.buffer) - MAX_LATENCY_BYTES
            del self.buffer[:count]
            self._drop(count)
        # The capture hardware and server RTP clock are independent. A gentle
        # maximum 1/480 (0.208%) correction drains sustained drift above the
        # jitter reserve; brief jitter below it retains every original sample.
        correct = not self.silent and len(self.buffer) > DRIFT_RESERVE_BYTES
        count = min(FRAME_BYTES + (2 if correct else 0), len(self.buffer)) if not self.silent else 0
        pcm = bytes(self.buffer[:count])
        del self.buffer[:count]
        if correct:
            pcm = _correct_drift(pcm)
            self.adjusted_samples += 1
        else:
            self.silence_samples += (FRAME_BYTES - count) // 2
            pcm += bytes(FRAME_BYTES - count)
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
        if isinstance(output, BrowserOutput):
            output.use_realtime_clock()
        self._voice_session_id = voice_session_identity(getattr(output, "voice_session_id", ""))
        self._track = _MicrophoneTrack(self._voice_session_id)
        # Provider candidates are supplied by the native SDP answer. Avoid
        # aiortc's default third-party STUN service; browser egress is never used.
        # Gather separate transports, then let the answer select the BUNDLE
        # master. Native Codex answers advertise candidates only on that master;
        # aiortc MAX_BUNDLE otherwise overwrites them with the empty application
        # section before connectivity checks can start (especially with ICE-lite).
        self._peer = RTCPeerConnection(RTCConfiguration(iceServers=[], bundlePolicy=RTCBundlePolicy.BALANCED))
        self._peer.addTrack(self._track)
        self._channel = self._peer.createDataChannel("oai-events")
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()
        self._failure = ""
        self._quiet = False
        self._closed = False
        self._receiving = False
        self._workers: list[asyncio.Task[None]] = []
        self._upstream_gap = Timing()
        self._output_wait = Timing()
        self._output_samples = 0
        self._last_output_report = float("-inf")
        self._receiver_health = ReceiverAudioHealth(self._voice_session_id)
        self._media_clock = MediaClock()
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

    def _worker(self, task: asyncio.Task[None], stage: FailureStage = "media_worker") -> None:
        self._workers.append(task)

        def finished(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                if not self._quiet and not self._closed and not self._failure:
                    log_live_failure(self._voice_session_id, stage, error)
                self._fail(str(error) if isinstance(error, LoginVoiceError) else "ChatGPT audio relay was interrupted.")

        task.add_done_callback(finished)

    async def _microphone(self) -> None:
        async for chunk in self._source:
            if self._quiet:
                return
            self._track.append(chunk)
            # Queue.get() can complete synchronously for an entire browser
            # backlog. Give RTP/control/result delivery a turn between chunks.
            await asyncio.sleep(0)
        self._fail("The microphone audio stream ended. Reconnect voice.")

    def _remote_track(self, track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            return
        if self._receiving:
            self._fail("ChatGPT voice returned an unexpected audio stream.")
            return
        self._receiving = True
        self._worker(asyncio.create_task(self._playback(track), name="web-chatgpt-media-output"), "media_output")
        for receiver in self._peer.getReceivers():
            if receiver.track is track:
                self._worker(
                    asyncio.create_task(self._watch_receiver(receiver), name="web-chatgpt-audio-stats"), "media_stats"
                )
                break

    async def _watch_receiver(self, receiver: RTCRtpReceiver) -> None:
        while not self._closed:
            try:
                async with asyncio.timeout(0.5):
                    report = await receiver.getStats()
                self._receiver_health.observe(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Diagnostics cannot end voice or expose an exception payload.
                self._receiver_health.failures += 1
                if self._receiver_health.failures == 1:
                    self._receiver_health.report()
            await asyncio.sleep(1)

    async def _playback(self, track: MediaStreamTrack) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        previous = 0.0
        duration = 0.0
        drained = 0
        while not self._closed:
            try:
                self._receiver_health.receiving = True
                frame = await track.recv()
            except MediaStreamError:
                self._fail("The ChatGPT voice audio stream ended. Reconnect voice.")
                return
            finally:
                self._receiver_health.receiving = False
            if not isinstance(frame, AudioFrame):
                raise LoginVoiceError("ChatGPT voice returned an invalid audio frame.")
            self._receiver_health.frames += 1
            self._receiver_health.sample_rate = frame.sample_rate
            now = monotonic()
            valid_clock = frame.pts is not None and frame.time_base is not None
            media_time = (
                float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base is not None else 0.0
            )
            # Source timestamps are not synchronized with the local clock.
            # Keep offset drift visible, but never use it to discard fresh PCM.
            frame_duration = frame.samples / frame.sample_rate if frame.sample_rate else 0.0
            self._media_clock.observe(now, media_time, valid=valid_clock, duration=frame_duration)
            if self._media_clock.arrival_interval_ms > 40 or self._media_clock.source_gap_ms > 40:
                logger.info(
                    "Live decoded audio interval: voice_session_id=%s arrival_interval_ms=%.1f "
                    "source_interval_ms=%.1f source_gap_ms=%.1f transport_late_ms=%.1f output_samples=%d",
                    self._voice_session_id,
                    self._media_clock.arrival_interval_ms,
                    self._media_clock.source_interval_ms,
                    self._media_clock.source_gap_ms,
                    self._media_clock.transport_late_ms,
                    self._output_samples,
                )
            # This is observed decoded-frame spacing, not network-only jitter:
            # downstream write waits also delay the next recv(). The concurrent
            # RTP snapshot reports packet progress and receive/write wait flags.
            if previous and self._upstream_gap.observe(now - previous - duration):
                self._report_output()
            previous = now
            duration = frame_duration
            for converted in resampler.resample(frame):
                if not self._quiet:
                    # PyAV planes may include alignment padding, which is not
                    # audio and must not be sent to the browser's sample clock.
                    started = monotonic()
                    self._receiver_health.writing = True
                    try:
                        pcm = bytes(converted.planes[0])[: converted.samples * 2]
                        if isinstance(self._output, BrowserOutput):
                            self._output.enqueue_realtime(pcm)
                        else:
                            await self._output.write(pcm)
                    finally:
                        self._receiver_health.writing = False
                    self._output_samples += converted.samples
                    self._receiver_health.output_samples += converted.samples
                    if self._output_wait.observe(monotonic() - started):
                        self._report_output()
            # Public recv() can synchronously drain aiortc's accumulated frames.
            # Yield fairly without downstream backpressure or one task/frame.
            drained += 1
            if drained % 8 == 0:
                await asyncio.sleep(0)

    def _report_output(self, *, closing: bool = False) -> None:
        now = monotonic()
        if not closing and now - self._last_output_report < 5:
            return
        self._last_output_report = now
        log = logger.info if closing else logger.warning
        log(
            "Live provider audio timing: voice_session_id=%s closing=%d output_samples=%d frame_intervals=%d "
            "frame_gaps=%d frame_gap_ms=%.1f frame_gap_peak_ms=%.1f writes=%d "
            "slow_writes=%d write_ms=%.1f write_peak_ms=%.1f "
            "source_clock_offset_lag_ms=%.1f source_clock_offset_lag_peak_ms=%.1f source_clock_resets=%d source_clock_missing=%d",
            self._voice_session_id,
            int(closing),
            self._output_samples,
            self._upstream_gap.count,
            self._upstream_gap.delayed,
            self._upstream_gap.total_ms,
            self._upstream_gap.peak_ms,
            self._output_wait.count,
            self._output_wait.delayed,
            self._output_wait.total_ms,
            self._output_wait.peak_ms,
            self._media_clock.offset_lag_ms,
            self._media_clock.offset_lag_peak_ms,
            self._media_clock.resets,
            self._media_clock.missing,
        )

    async def offer(self) -> str:
        self._worker(asyncio.create_task(self._microphone(), name="web-chatgpt-media-input"), "media_input")
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
        self._report_output(closing=True)
        self._receiver_health.report(closing=True)
        logger.info(
            "Live microphone relay closed: voice_session_id=%s input_samples=%d dropped_samples=%d adjusted_samples=%d silence_samples=%d peak_buffered_bytes=%d clock_rebases=%d late_ms=%.0f recoveries=%d",
            self._voice_session_id,
            self._track.input_samples,
            self._track.dropped_samples,
            self._track.adjusted_samples,
            self._track.silence_samples,
            self._track.peak_buffered_bytes,
            self._track.clock_rebases,
            self._track.late_ms,
            self._track.recoveries,
        )
