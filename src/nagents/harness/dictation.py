"""Opt-in, in-memory file transcription, separate from Realtime and chat auth.

``capture()`` starts the microphone only when explicitly called. ``stop()`` ends
capture without uploading; ``transcribe(wav_bytes)`` performs exactly one upload.
Always await ``close()`` on shutdown. No filenames, URLs, or file-like inputs are
accepted as audio, and neither recordings nor transcripts are logged or saved.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import wave
from contextlib import suppress
from io import BytesIO
from time import monotonic
from typing import TYPE_CHECKING
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
REQUEST_SECONDS = 60
MAX_RESPONSE_BYTES = 256 * 1024
_MIC_ERROR = (
    "Microphone unavailable. Check the default input device, OS microphone permission, "
    "and PortAudio installation; a local microphone supporting 16 kHz mono is required."
)


class DictationError(RuntimeError):
    """Safe user-facing failure; never includes backend exceptions or response bodies."""


class _InputStream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


def _wav(pcm: bytes | bytearray) -> bytes:
    with BytesIO() as output:
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(SAMPLE_RATE)
            audio.writeframes(pcm)
        return output.getvalue()


class VoiceDictation:
    """One capture or upload at a time, with a fixed destination per instance.

    API-key lookup uses only ``dictation_api_key_env``. Construction and readiness
    checks do not import sounddevice, open devices, or access the network. Native
    device work runs in a thread; cancellation waits for that thread to close its
    stream (a stuck OS driver cannot safely be forcibly interrupted by Python).
    """

    def __init__(self, config: HarnessConfig) -> None:
        try:
            url = urlsplit(config.dictation_base_url)
            valid_url = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not any(char.isspace() or ord(char) < 32 for char in config.dictation_base_url)
            )
            _ = url.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise DictationError(
                "Set dictation_base_url to an HTTP(S) API base URL without credentials or query/fragment."
            )
        if type(config.dictation_max_seconds) is not int or not 1 <= config.dictation_max_seconds <= 300:
            raise DictationError("Set dictation_max_seconds to an integer between 1 and 300.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.dictation_api_key_env):
            raise DictationError("Set dictation_api_key_env to an environment variable name, not a secret.")
        if not config.dictation_model.strip() or (
            config.dictation_language and not re.fullmatch(r"[a-z]{2}", config.dictation_language)
        ):
            raise DictationError("Set a dictation model and a blank or two-letter lowercase language code.")
        self.endpoint = url.geturl().rstrip("/") + "/audio/transcriptions"
        self.max_seconds = config.dictation_max_seconds
        self.model = config.dictation_model
        self.language = config.dictation_language
        self.api_key_env = config.dictation_api_key_env
        self._enabled = config.dictation_enabled is True
        self._demo = config.demo
        self._closed = False
        self._stop = threading.Event()
        self._discard = threading.Event()
        self._capturing = threading.Event()
        self._started_at = 0.0
        self._record_task: asyncio.Task[bytes] | None = None
        self._upload_task: asyncio.Task[str] | None = None

    @property
    def capturing(self) -> bool:
        return self._capturing.is_set()

    @property
    def elapsed(self) -> float:
        return min(self.max_seconds, max(0.0, monotonic() - self._started_at)) if self._started_at else 0.0

    def check_ready(self) -> None:
        """Check opt-in and API-key setup without probing the microphone."""
        if self._closed:
            raise DictationError("Dictation is closed. Open a new dictation dialog.")
        if not self._enabled:
            raise DictationError("Dictation is disabled. Restart with --dictation or set dictation_enabled=true.")
        if self._demo:
            raise DictationError("Dictation is unavailable in demo mode. Restart without --demo.")
        self._key()

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not re.fullmatch(r"[\x21-\x7e]{1,65536}", key):
            raise DictationError(
                f"Set {self.api_key_env} to a transcription API key in your environment. "
                "ChatGPT login and chat-provider keys are not used as fallbacks; API billing is separate."
            )
        return key

    def _check_idle(self) -> None:
        self.check_ready()
        if self._record_task is not None or self._upload_task is not None:
            raise DictationError("Dictation is busy. Stop or cancel the current operation first.")

    async def capture(self) -> bytes:
        """Explicitly start capture; return WAV on stop or cap, without uploading.

        Cancellation discards the recording and waits for native stream cleanup.
        Overflow or device failure discards the entire recording, not partial audio.
        """
        self._check_idle()
        self._stop.clear()
        self._discard.clear()
        self._started_at = 0.0
        task = self._record_task = asyncio.create_task(asyncio.to_thread(self._record))
        try:
            audio = await asyncio.shield(task)
            if self._discard.is_set():
                raise asyncio.CancelledError
            return audio
        except asyncio.CancelledError:
            self._discard.set()
            self._stop.set()
            # Cancelling to_thread alone leaves the microphone running. Shield and
            # drain even repeated cancellations before relinquishing ownership.
            while not task.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(task)
            with suppress(Exception):
                task.result()
            raise
        finally:
            self._record_task = None

    def stop(self) -> None:
        """Request capture completion. This never starts an upload."""
        self._stop.set()

    def _record(self) -> bytes:
        if self._stop.is_set():
            return b""
        try:
            import sounddevice
        except ImportError:
            raise DictationError(
                "Install the voice extra in this environment: pip install 'nagents[tui,voice]'."
            ) from None
        except Exception:
            raise DictationError(_MIC_ERROR) from None

        pcm = bytearray()
        limit = self.max_seconds * BYTES_PER_SECOND
        failure = ""

        def callback(indata: bytes, frames: int, timing: object, status: object) -> None:
            nonlocal failure
            if self._stop.is_set():
                return
            try:
                if status:
                    failure = "Audio capture overflowed or was interrupted. Close other audio apps and record again."
                elif len(indata) != frames * 2 or frames <= 0:
                    failure = "Invalid microphone audio. Check the input device and record again."
                else:
                    # RawInputStream supplies a buffer, not a NumPy array. Copy at
                    # most the remaining allowance; never queue callback payloads.
                    pcm.extend(memoryview(indata)[: limit - len(pcm)])
                if failure or len(pcm) >= limit:
                    self._stop.set()
            except Exception:
                failure = _MIC_ERROR
                self._stop.set()

        def finished() -> None:
            nonlocal failure
            if not self._stop.is_set():
                failure = _MIC_ERROR
                self._stop.set()

        stream: _InputStream | None = None
        try:
            stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=1600,
                callback=callback,
                finished_callback=finished,
            )
            if not self._stop.is_set():
                self._started_at = monotonic()
                stream.start()
                self._capturing.set()
                self._stop.wait(max(0.0, self.max_seconds - self.elapsed))
        except Exception:
            failure = _MIC_ERROR
        finally:
            self._stop.set()
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    failure = _MIC_ERROR
                    with suppress(Exception):
                        stream.abort()
                try:
                    stream.close()
                except Exception:
                    failure = _MIC_ERROR
            self._capturing.clear()
        try:
            if self._discard.is_set():
                return b""
            if failure:
                raise DictationError(failure)
            if not pcm:
                raise DictationError("No audio captured. Check your microphone and record again.")
            return _wav(pcm)
        finally:
            pcm.clear()

    async def transcribe(self, audio: bytes) -> str:
        """Upload explicitly supplied 16 kHz mono PCM16 WAV bytes once.

        No path/file/URL coercion or file access. Input is duration/size checked and
        re-encoded without ancillary WAV metadata before sending. Compatible APIs
        must accept multipart file/model/response_format=json and return {text}.
        """
        self._check_idle()
        if not isinstance(audio, bytes) or not 44 < len(audio) <= self.max_seconds * BYTES_PER_SECOND + 4096:
            raise DictationError("Supply bounded WAV bytes, not a path: 16 kHz mono PCM16 within the recording limit.")
        try:
            with wave.open(BytesIO(audio), "rb") as wav:
                frames = wav.getnframes()
                if (
                    wav.getnchannels() != 1
                    or wav.getsampwidth() != 2
                    or wav.getframerate() != SAMPLE_RATE
                    or wav.getcomptype() != "NONE"
                    or not 0 < frames <= self.max_seconds * SAMPLE_RATE
                ):
                    raise ValueError
                pcm = wav.readframes(frames)
                if len(pcm) != frames * 2:
                    raise ValueError
                audio = _wav(pcm)
        except Exception:
            raise DictationError(
                "Invalid or truncated WAV. Supply 16 kHz mono PCM16 audio within the recording limit."
            ) from None
        task = self._upload_task = asyncio.create_task(self._upload(audio))
        try:
            return await task
        finally:
            self._upload_task = None

    async def _upload(self, audio: bytes) -> str:
        form = aiohttp.FormData()
        form.add_field("file", audio, filename="dictation.wav", content_type="audio/wav")
        form.add_field("model", self.model)
        form.add_field("response_format", "json")
        if self.language:
            form.add_field("language", self.language)
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=REQUEST_SECONDS, connect=10),
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                ) as session,
                session.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self._key()}"},
                    data=form,
                    allow_redirects=False,
                ) as response,
            ):
                if not 200 <= response.status < 300:
                    if response.status in {401, 403}:
                        raise DictationError(
                            "Transcription authorization failed. Check the configured API key and model access."
                        )
                    if response.status == 429:
                        raise DictationError(
                            "Transcription rate limit or quota reached. Check API billing before trying again."
                        )
                    raise DictationError(
                        f"Transcription failed (HTTP {response.status}). Check the endpoint and model; upload was not retried."
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DictationError(
                            "Transcription response was too large. Check the endpoint; upload was not retried."
                        )
                    body.extend(chunk)
                data: object = json.loads(body)
                if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                    raise DictationError("Invalid transcription response. Check the endpoint; upload was not retried.")
                text = "".join(char for char in data["text"] if char.isprintable() or char in "\n\t").strip()
                if not text:
                    raise DictationError("No speech recognized. Check the microphone and record again.")
                return text
        except DictationError:
            raise
        except TimeoutError:
            raise DictationError(
                "Transcription timed out. Upload was not retried; the server may already have received audio."
            ) from None
        except Exception:
            raise DictationError(
                "Transcription failed. Check the connection and endpoint; upload was not retried."
            ) from None

    async def cancel(self) -> None:
        """Discard capture or cancel the HTTP request, awaiting resource cleanup.

        Cancellation cannot recall audio already received by the remote service.
        """
        self._discard.set()
        self._stop.set()
        if self._upload_task is not None:
            self._upload_task.cancel()
            with suppress(asyncio.CancelledError, DictationError):
                await self._upload_task
        if self._record_task is not None:
            with suppress(DictationError):
                await asyncio.shield(self._record_task)

    async def close(self) -> None:
        """Idempotently disable this instance and await microphone/network cleanup."""
        self._closed = True
        await self.cancel()
