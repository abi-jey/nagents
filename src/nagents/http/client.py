"""
Async HTTP client for LLM API requests using aiohttp.
"""

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import aiohttp

if TYPE_CHECKING:
    from .logger import HTTPLogger


class HTTPError(Exception):
    """HTTP request error with status code, URI, and response body."""

    def __init__(self, status: int, message: str, body: str | None = None, uri: str | None = None):
        self.status = status
        self.body = body
        self.uri = uri
        if uri:
            super().__init__(f"HTTP {status}: {message} (uri={uri})")
        else:
            super().__init__(f"HTTP {status}: {message}")


class HTTPClient:
    """Async HTTP client for LLM API requests."""

    def __init__(
        self,
        timeout: float = 120.0,
        logger: "HTTPLogger | None" = None,
    ):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._logger = logger
        self._session_id: str | None = None

    def set_session_id(self, session_id: str | None) -> None:
        """Set the current session ID for logging."""
        self._session_id = session_id

    def set_logger(self, logger: "HTTPLogger | None") -> None:
        """Set the HTTP logger."""
        self._logger = logger

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def post_json(
        self,
        url: str,
        data: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """
        Non-streaming POST request.

        Args:
            url: The URL to POST to
            data: JSON body to send
            headers: HTTP headers

        Returns:
            Parsed JSON response

        Raises:
            HTTPError: If the request fails
        """
        # Log request
        if self._logger:
            self._logger.log_request("POST", url, headers, data, self._session_id)

        session = await self._get_session()
        async with session.post(url, json=data, headers=headers) as resp:
            body = await resp.text()

            # Log response
            if self._logger:
                self._logger.log_response(url, resp.status, body, self._session_id)

            if resp.status >= 400:
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url)
            return cast("dict[str, Any]", json.loads(body))

    async def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Non-streaming GET request.

        Args:
            url: The URL to GET
            headers: Optional HTTP headers

        Returns:
            Parsed JSON response

        Raises:
            HTTPError: If the request fails
        """
        # Log request
        if self._logger:
            self._logger.log_request("GET", url, headers or {}, None, self._session_id)

        session = await self._get_session()
        async with session.get(url, headers=headers or {}) as resp:
            body = await resp.text()

            # Log response
            if self._logger:
                self._logger.log_response(url, resp.status, body, self._session_id)

            if resp.status >= 400:
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url)
            return cast("dict[str, Any]", json.loads(body))

    async def post_stream(
        self,
        url: str,
        data: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[str]:
        """
        Streaming POST request, yields SSE data lines.

        Args:
            url: The URL to POST to
            data: JSON body to send
            headers: HTTP headers

        Yields:
            SSE data payloads (with "data: " prefix removed)

        Raises:
            HTTPError: If the request fails
        """
        # Log request
        if self._logger:
            self._logger.log_request("POST", url, headers, data, self._session_id)

        session = await self._get_session()
        async with session.post(url, json=data, headers=headers) as resp:
            if resp.status >= 400:
                body = await resp.text()
                # Log error response
                if self._logger:
                    self._logger.log_response(url, resp.status, body, self._session_id)
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url)

            # Read line by line for SSE
            async for line in resp.content:
                decoded = line.decode("utf-8").strip()
                if not decoded:
                    continue
                if decoded.startswith("data: "):
                    chunk_data = decoded[6:]  # Remove "data: " prefix
                    # Log SSE chunk
                    if self._logger:
                        self._logger.log_sse_chunk(url, chunk_data, self._session_id)
                    yield chunk_data
                # Some APIs use just "data:" without space
                elif decoded.startswith("data:"):
                    chunk_data = decoded[5:]
                    # Log SSE chunk
                    if self._logger:
                        self._logger.log_sse_chunk(url, chunk_data, self._session_id)
                    yield chunk_data

    async def post_stream_ndjson(
        self,
        url: str,
        data: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming POST request for NDJSON (newline-delimited JSON) responses.
        Used by Gemini native API.

        Args:
            url: The URL to POST to
            data: JSON body to send
            headers: HTTP headers

        Yields:
            Parsed JSON objects

        Raises:
            HTTPError: If the request fails
        """
        # Log request
        if self._logger:
            self._logger.log_request("POST", url, headers, data, self._session_id)

        session = await self._get_session()
        async with session.post(url, json=data, headers=headers) as resp:
            if resp.status >= 400:
                body = await resp.text()
                # Log error response
                if self._logger:
                    self._logger.log_response(url, resp.status, body, self._session_id)
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url)

            buffer = ""
            async for chunk in resp.content:
                buffer += chunk.decode("utf-8")
                # Try to parse complete JSON objects from buffer
                while buffer:
                    buffer = buffer.lstrip()
                    if not buffer:
                        break
                    try:
                        # Try to find a complete JSON object
                        obj, idx = json.JSONDecoder().raw_decode(buffer)
                        # Log SSE chunk (NDJSON object)
                        if self._logger:
                            self._logger.log_sse_chunk(url, json.dumps(obj), self._session_id)
                        yield obj
                        buffer = buffer[idx:]
                    except json.JSONDecodeError:
                        # Incomplete JSON, wait for more data
                        break

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "HTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
