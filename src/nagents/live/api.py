"""Live HTTP operations for browser/SIP setup, stored forks and recordings."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING
from typing import cast
from urllib.parse import quote

import aiohttp

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from ..provider import Provider


class LiveAPI:
    def __init__(self, provider: Provider) -> None:
        if provider.live_config is None:
            raise ValueError("Use a voice provider with live_config for Live API authentication")
        self.provider = provider
        self.base_url = "https://api.openai.com/v1/live/sessions"

    def url(self, session_id: str = "", operation: str = "") -> str:
        if operation and not session_id:
            raise ValueError("A session ID is required")
        return self.base_url + (f"/{quote(session_id, safe='')}/{operation}" if session_id else "")

    async def _post(self, url: str, payload: dict[str, object] | None) -> dict[str, object]:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http,
            http.post(
                url, json=payload, headers={"Authorization": f"Bearer {self.provider.api_key}"}, allow_redirects=False
            ) as response,
        ):
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Live HTTP operation failed ({response.status})")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Live HTTP response is too large")
            value: object = json.loads(raw) if raw.strip() else {}
            if not isinstance(value, dict):
                raise ValueError("Invalid Live HTTP response")
            return cast("dict[str, object]", value)

    async def create_webrtc(self, sdp: str, session: dict[str, object]) -> dict[str, object]:
        if not sdp.strip():
            raise ValueError("An SDP offer is required")
        return await self._post(self.url(), {"session": session, "transport": {"type": "webrtc", "sdp": sdp}})

    async def fork_webrtc(self, source_id: str, sdp: str, *, store: bool = False) -> dict[str, object]:
        return await self._post(
            self.url(source_id, "fork"), {"session": {"store": store}, "transport": {"type": "webrtc", "sdp": sdp}}
        )

    async def accept(self, session_id: str, session: dict[str, object]) -> None:
        await self._post(self.url(session_id, "accept"), {"session": {**session, "type": "live"}})

    async def reject(self, session_id: str, status_code: int = 486) -> None:
        if type(status_code) is not int or not 300 <= status_code <= 699:
            raise ValueError("SIP rejection status must be 300-699")
        await self._post(self.url(session_id, "reject"), {"status_code": status_code})

    async def refer(self, session_id: str, target_uri: str) -> None:
        if not target_uri.startswith(("sip:", "tel:")):
            raise ValueError("A SIP or telephone target URI is required")
        await self._post(self.url(session_id, "refer"), {"target_uri": target_uri})

    async def hangup(self, session_id: str) -> None:
        await self._post(self.url(session_id, "hangup"), None)

    async def recording(self, session_id: str) -> AsyncGenerator[bytes, None]:
        async with (
            aiohttp.ClientSession() as http,
            http.get(
                self.url(session_id, "content"),
                headers={"Authorization": f"Bearer {self.provider.api_key}"},
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                raise RuntimeError(f"Recording unavailable ({response.status})")
            async for chunk in response.content.iter_chunked(65536):
                yield chunk

    async def download_recording(self, session_id: str, path: Path) -> None:
        with path.open("wb") as stream:
            async for chunk in self.recording(session_id):
                stream.write(chunk)


def verify_webhook(body: bytes, headers: dict[str, str], secret: str, *, tolerance: int = 300) -> dict[str, object]:
    """Verify Standard Webhooks raw-body signatures before parsing a SIP event."""
    headers = {key.lower(): value for key, value in headers.items()}
    try:
        identifier = headers["webhook-id"]
        timestamp = headers["webhook-timestamp"]
        if abs(time.time() - int(timestamp)) > tolerance:
            raise ValueError
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        signature = base64.b64encode(
            hmac.new(key, identifier.encode() + b"." + timestamp.encode() + b"." + body, hashlib.sha256).digest()
        ).decode()
        if not any(hmac.compare_digest(item, "v1," + signature) for item in headers["webhook-signature"].split()):
            raise ValueError
        result: object = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError
        return cast("dict[str, object]", result)
    except (KeyError, ValueError, UnicodeError):
        raise ValueError("Invalid Live webhook signature or payload") from None
