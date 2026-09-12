"""Request-owned, memory-only WAV uploads. Never captures devices or runs an agent."""

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from nagents.harness.dictation import BYTES_PER_SECOND
from nagents.harness.dictation import DictationError
from nagents.harness.dictation import DictationInputError
from nagents.harness.dictation import VoiceDictation

from .settings import _join

if TYPE_CHECKING:
    from starlette.requests import Request

    from nagents.harness.config import HarnessConfig

BODY_SECONDS = 60.0
MAX_AUDIO_BYTES = 300 * BYTES_PER_SECOND + 4096
MAX_TEXT_CHARACTERS = 32000


@dataclass
class _Upload:
    voice: VoiceDictation
    task: asyncio.Task[str]
    finished: asyncio.Event = field(default_factory=asyncio.Event)


async def _read_audio(request: "Request", limit: int) -> bytes:
    lengths = request.headers.getlist("content-length")
    if lengths and (
        len(lengths) != 1
        or re.fullmatch(r"[0-9]{1,20}", lengths[0]) is None
        or request.headers.getlist("transfer-encoding")
    ):
        raise HTTPException(422, "Invalid recording Content-Length.")
    if lengths and int(lengths[0]) > limit:
        raise HTTPException(413, "Recording exceeds the configured byte limit.")
    body = bytearray()
    try:
        async with asyncio.timeout(BODY_SECONDS):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, "Recording exceeds the configured byte limit.")
                body.extend(chunk)
        if lengths and len(body) != int(lengths[0]):
            raise HTTPException(422, "Recording length does not match Content-Length.")
        return bytes(body)
    except TimeoutError:
        raise HTTPException(408, "Recording upload timed out. Record again when the connection is ready.") from None
    except ClientDisconnect:
        raise HTTPException(400, "Recording upload disconnected.") from None
    finally:
        body.clear()


async def _disconnected(request: "Request") -> None:
    # Only called AFTER stream() consumed the terminal body message. This is
    # the sole receive consumer while the upstream request is in progress.
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _stop_tasks(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task


class WebDictation:
    """One upload owned by the caller's WebState.idle() lease until cleanup ends.

    Execution is never shielded. Waiting with wait() lets request cancellation
    cancel each owned worker once, without repeatedly interrupting its cleanup.
    Only joins are shielded, including repeated asyncio/AnyIO cancellation.
    """

    def __init__(self) -> None:
        self._active: _Upload | None = None
        self._closed = False

    async def transcribe(self, request: "Request", config: "HarnessConfig") -> dict[str, str]:
        if self._closed:
            raise HTTPException(503, "Dictation is shutting down. Reconnect before recording again.")
        if self._active is not None:
            raise HTTPException(409, "A recording upload is already active.")
        try:
            voice = VoiceDictation(config)
            voice.check_ready()
        except DictationError as exc:
            raise HTTPException(503, str(exc)) from None
        except Exception:
            raise HTTPException(
                503, "Dictation configuration is unavailable. Check backend transcription setup."
            ) from None
        limit = min(config.dictation_max_seconds * BYTES_PER_SECOND + 4096, MAX_AUDIO_BYTES)
        upload = _Upload(voice, asyncio.create_task(self._perform(request, voice, limit)))
        self._active = upload
        try:
            await asyncio.wait({upload.task})
            return {"text": upload.task.result()}
        finally:
            if not upload.task.done() and not upload.task.cancelling():
                upload.task.cancel()
            try:
                await _join(asyncio.create_task(self._cleanup(upload)))
            finally:
                self._active = None
                upload.finished.set()

    async def _perform(self, request: "Request", voice: VoiceDictation, limit: int) -> str:
        try:
            audio = await _read_audio(request, limit)
            disconnected = asyncio.create_task(_disconnected(request))
            transcription = asyncio.create_task(voice.transcribe(audio))
            try:
                await asyncio.wait({disconnected, transcription}, return_when=asyncio.FIRST_COMPLETED)
                if disconnected.done():
                    disconnected.result()
                    raise HTTPException(400, "Recording upload disconnected; transcription was cancelled.")
                text = transcription.result()
                if len(text) > MAX_TEXT_CHARACTERS:
                    raise HTTPException(502, "Transcription text exceeds the draft limit. Record a shorter message.")
                return text
            finally:
                await _join(asyncio.create_task(_stop_tasks(disconnected, transcription)))
        except DictationInputError as exc:
            raise HTTPException(422, str(exc)) from None
        except DictationError as exc:
            raise HTTPException(502, str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                502, "Transcription failed. Check backend transcription setup; upload was not retried."
            ) from None

    @staticmethod
    async def _cleanup(upload: _Upload) -> None:
        try:
            await _stop_tasks(upload.task)
        finally:
            await upload.voice.close()

    async def close(self) -> None:
        """Disable admission and join the request's cleanup before closing Harness."""
        self._closed = True
        upload = self._active
        if upload is not None:
            if not upload.task.done() and not upload.task.cancelling():
                upload.task.cancel()

            async def finished() -> None:
                await upload.finished.wait()

            await _join(asyncio.create_task(finished()))
