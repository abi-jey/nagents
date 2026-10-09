"""ChatGPT-authenticated GPT-Live calls with server media and owned sideband.

This is the Codex Live (quicksilver v2) contract. Its WebRTC provisioning,
transcript items and context appends are distinct from public Live sessions.
Credentials only travel to the two fixed OpenAI service origins below.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast
from uuid import uuid4

import aiohttp

from nagents.live.runtime import _no_redirects

from .live_handoff import LoginDelegations
from .live_handoff import LoginHandoff
from .live_handoff import handoff_offset

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.live.delegation import ClientDelegationObserver
    from nagents.provider.openai import CodexCredentials

Payload = dict[str, object]
CALLS_URL = "https://chatgpt.com/backend-api/wham/realtime/calls?intent=quicksilver&architecture=avas"
SIDEBAND_URL = "wss://api.openai.com/v1/live/"
MAX_SDP_BYTES = 65536
_CALL_ID = re.compile(r"rtc_[A-Za-z0-9_-]{1,256}\Z")


@dataclass(frozen=True)
class LoginVoiceConfig:
    credentials: Callable[[], Awaitable[CodexCredentials]]
    model: str
    voice: str
    instructions: str
    handler: Callable[[LoginHandoff], Awaitable[str]]
    history: tuple[Payload, ...] = ()
    observer: ClientDelegationObserver | None = None


class LoginVoiceError(RuntimeError):
    """A safe failure message that never includes provider bodies or credentials."""


def _failure(status: int) -> LoginVoiceError:
    if status == 401:
        return LoginVoiceError("ChatGPT sign-in expired. Sign in again in Provider connections.")
    if status == 403:
        return LoginVoiceError("This ChatGPT account does not have access to GPT-Live voice.")
    if status == 429:
        return LoginVoiceError("The ChatGPT voice usage limit was reached. Try again later.")
    return LoginVoiceError("The ChatGPT voice connection could not be established.")


def normalize_event(event: Payload) -> Payload:
    """Preserve fragment timing while adapting the first-party Live envelope."""
    kind = event.get("type")
    if kind in {"input_transcript.added", "output_transcript.added"}:
        item = event.get("item")
        start, end = event.get("start_ms"), event.get("end_ms")
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or not isinstance(start, int | float)
            or isinstance(start, bool)
            or not isinstance(end, int | float)
            or isinstance(end, bool)
            or not 0 <= start <= end <= 1e12
        ):
            raise LoginVoiceError("ChatGPT voice returned an invalid transcript event.")
        normalized: Payload = {
            "type": "session.input_transcript.delta"
            if kind == "input_transcript.added"
            else "session.output_transcript.delta",
            "delta": item["text"],
            "start_ms": start,
            "end_ms": end,
        }
        source_id = item.get("id")
        if isinstance(source_id, str) and source_id and len(source_id) <= 256:
            normalized["source_event_id"] = source_id
        return normalized
    if kind == "delegation.created":
        item = event.get("item")
        if not isinstance(item, dict) or item.get("target") != "client":
            return {}
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256:
            raise LoginVoiceError("ChatGPT voice returned an invalid delegation event.")
        notice: Payload = {"type": "session.delegation.created", "delegation": {"id": identifier, "target": "client"}}
        offset = handoff_offset(event.get("offset_ms"))
        if offset is not None:
            notice["offset_ms"] = offset
        return notice
    if kind in {"session.closed", "session.usage.updated"}:
        usage = event.get("usage")
        if isinstance(usage, dict):
            duration = usage.get("audio_duration_ms")
            if isinstance(duration, int | float) and not isinstance(duration, bool) and 0 <= duration <= 1e12:
                return {**event, "usage": {**usage, "seconds": duration / 1000}}
    return event


class ChatGPTLiveConnection:
    """Own one login call and its authenticated native sideband."""

    def __init__(self, config: LoginVoiceConfig) -> None:
        self.config = config
        self.identifier = ""
        self.finalized = False
        self.closing = False
        self._thread = str(uuid4())
        self._sockets: list[aiohttp.ClientWebSocketResponse] = []
        self._workers: list[asyncio.Task[None]] = []
        self._reading = False
        self._send_lock = asyncio.Lock()

    async def _headers(self) -> dict[str, str]:
        credentials = await self.config.credentials()
        if not credentials.access_token:
            raise LoginVoiceError("Sign in with ChatGPT in Provider connections before starting voice.")
        return {
            "Authorization": f"Bearer {credentials.access_token}",
            "ChatGPT-Account-Id": credentials.account_id,
            "OpenAI-Alpha": "quicksilver=v2",
            "Thread-Id": self._thread,
            "Session-Id": self._thread,
            "User-Agent": "nagents-live",
            "originator": "nagents",
        }

    async def provision(self, sdp: str) -> str:
        if self.identifier:
            raise LoginVoiceError("This ChatGPT voice call was already provisioned.")
        if not sdp.startswith(("v=0\r\n", "v=0\n")) or len(sdp.encode()) > MAX_SDP_BYTES:
            raise LoginVoiceError("A valid server audio offer is required.")
        session = {
            "model": self.config.model,
            "instructions": self.config.instructions,
            "audio": {"output": {"voice": self.config.voice}},
            "delegation": {"type": "client"},
            "initial_items": list(self.config.history),
        }
        try:
            async with (
                aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http,
                http.post(
                    CALLS_URL,
                    headers=await self._headers(),
                    json={"sdp": sdp, "session": session},
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as response,
            ):
                if response.status != 201:
                    raise _failure(response.status)
                identifier = response.headers.get("Location", "").split("?", 1)[0].rsplit("/", 1)[-1]
                if not _CALL_ID.fullmatch(identifier):
                    raise LoginVoiceError("ChatGPT voice returned an invalid call identifier.")
                self.identifier = identifier
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_SDP_BYTES:
                        raise LoginVoiceError("ChatGPT voice returned an invalid audio answer.")
                answer = raw.decode("utf-8")
                if not answer.startswith(("v=0\r\n", "v=0\n")):
                    raise LoginVoiceError("ChatGPT voice returned an invalid audio answer.")
                return answer
        except (aiohttp.ClientError, TimeoutError, UnicodeError):
            raise LoginVoiceError("The ChatGPT voice connection could not be established.") from None

    async def _send(self, event: Payload) -> None:
        if not self._sockets:
            raise LoginVoiceError("The ChatGPT voice control connection is not ready.")
        async with self._send_lock:
            if self.closing and event.get("type") != "session.close":
                return
            await self._sockets[0].send_json(event)

    async def _result(self, event: Payload) -> None:
        # Native v3 automatic handoffs use delegation.context.append in the
        # default thinking channel. appendSpeech's session-wide speakable context
        # is a different operation and does not resolve the requesting handoff.
        identifier, content = event.get("delegation_id"), event.get("content")
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or len(identifier) > 256
            or not isinstance(content, str)
        ):
            raise LoginVoiceError("ChatGPT voice returned an invalid delegation result.")
        await self._send(
            {
                "type": "delegation.context.append",
                "delegation_item_id": identifier,
                "content": [{"type": "input_text", "text": content}],
            }
        )

    async def events(self) -> AsyncGenerator[Payload, None]:
        if not self.identifier or self._reading:
            raise LoginVoiceError("ChatGPT voice needs one provisioned call and one control reader.")
        self._reading = True

        delegations = LoginDelegations(self.config.handler, self._result)
        worker = asyncio.create_task(delegations.run(), name="web-login-live-backend")
        self._workers.append(worker)
        try:
            async with (
                aiohttp.ClientSession(trace_configs=[_no_redirects()], cookie_jar=aiohttp.DummyCookieJar()) as http,
                http.ws_connect(
                    SIDEBAND_URL + self.identifier,
                    headers=await self._headers(),
                    heartbeat=20,
                ) as socket,
            ):
                self._sockets.append(socket)
                yield {"type": "connection.attached", "session": {"id": self.identifier}}
                if self.closing:
                    await self._send({"type": "session.close"})
                while True:
                    receiving = asyncio.create_task(socket.receive())
                    try:
                        done, _ = await asyncio.wait({receiving, worker}, return_when=asyncio.FIRST_COMPLETED)
                        # An explicit close cancels delegated speech work, while
                        # the reader must continue to the server's final event.
                        if receiving not in done and not self.closing:
                            worker.result()
                        message = await receiving
                    finally:
                        receiving.cancel()
                        await asyncio.gather(receiving, return_exceptions=True)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise LoginVoiceError("ChatGPT voice ended before finalization was confirmed.")
                    raw: object = json.loads(message.data)
                    if not isinstance(raw, dict):
                        raise LoginVoiceError("ChatGPT voice returned an invalid control event.")
                    event = normalize_event(cast("Payload", raw))
                    if not event:
                        continue
                    if event.get("type") == "session.closed":
                        self.finalized = True
                    if not self.closing and not self.finalized:
                        # Process a queued final event before a concurrent write
                        # failure can discard its authoritative confirmation.
                        if worker.done():
                            worker.result()
                        delegations.observe(event, cast("Payload", raw))
                    yield event
                    if self.finalized:
                        return
        except aiohttp.WSServerHandshakeError as error:
            raise _failure(error.status) from None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise LoginVoiceError("The ChatGPT voice control connection was interrupted.") from None
        finally:
            self._sockets.clear()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._workers.remove(worker)
            self._reading = False

    async def close(self) -> None:
        self.closing = True
        for worker in self._workers:
            worker.cancel()
        if self._sockets and not self.finalized:
            await self._send({"type": "session.close"})

    async def aclose(self) -> None:
        """Finalize even when HTTP provisioning succeeded before a reader started."""
        await self.close()
        if self.identifier and not self.finalized and not self._reading:
            with suppress(LoginVoiceError, TimeoutError):
                async with asyncio.timeout(5):
                    async for _ in self.events():
                        pass
