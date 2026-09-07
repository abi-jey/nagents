"""Bounded API-key HTTP transport, without raw HTTP logging or redirects.

Gateway responses can contain upstream credentials in error bodies or headers.
Never pass those to loggers or exceptions, including on a successful HTTP status.
"""

from collections.abc import AsyncIterator

import aiohttp

from ..adapters._validation import ProtocolError
from ..adapters._validation import load_object
from ..http import HTTPClient
from ..http import HTTPError

MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class GatewayHTTPClient(HTTPClient):
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, trust_env=False, cookie_jar=aiohttp.DummyCookieJar()
            )
        return self._session

    @staticmethod
    def _check_status(response: aiohttp.ClientResponse) -> None:
        if not 200 <= response.status < 300:
            # Retry only uses a numeric delay, never arbitrary response headers.
            headers: dict[str, str] = {}
            delay = response.headers.get("Retry-After", "")
            if delay.isascii() and delay.isdecimal() and len(delay) <= 8:
                headers["Retry-After"] = delay
            raise HTTPError(response.status, "Provider request failed", headers=headers)

    async def _json(self, method: str, url: str, data: object, headers: dict[str, str]) -> dict[str, object]:
        session = await self._get_session()
        async with session.request(method, url, json=data, headers=headers, allow_redirects=False) as response:
            self._check_status(response)
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProtocolError("Provider response exceeded the safe size limit.")
            try:
                return load_object(body.decode("utf-8"))
            except UnicodeError:
                raise ProtocolError("Provider returned invalid response encoding.") from None

    async def post_json(self, url: str, data: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
        return await self._json("POST", url, data, headers)

    async def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, object]:
        return await self._json("GET", url, None, headers or {})

    async def post_stream(self, url: str, data: dict[str, object], headers: dict[str, str]) -> AsyncIterator[str]:
        session = await self._get_session()
        async with session.post(url, json=data, headers=headers, allow_redirects=False) as response:
            self._check_status(response)
            if response.content_type != "text/event-stream":
                raise ProtocolError("Provider did not return an event stream.")
            buffer = b""
            fields: list[bytes] = []
            frame_size = total = 0
            async for chunk in response.content.iter_chunked(8192):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ProtocolError("Provider stream exceeded the safe size limit.")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.removesuffix(b"\r")
                    frame_size += len(line)
                    if frame_size > MAX_EVENT_BYTES:
                        raise ProtocolError("Provider event exceeded the safe size limit.")
                    if not line:
                        if fields:
                            try:
                                payload = b"\n".join(fields).decode("utf-8")
                            except UnicodeError:
                                raise ProtocolError("Provider returned invalid stream encoding.") from None
                            yield payload
                        fields = []
                        frame_size = 0
                    elif line.startswith(b"data:"):
                        fields.append(line[5:].removeprefix(b" "))
                if len(buffer) + frame_size > MAX_EVENT_BYTES:
                    raise ProtocolError("Provider event exceeded the safe size limit.")
            if fields or buffer.strip():
                raise ProtocolError("Provider stream ended inside an event; no tools were released.")
