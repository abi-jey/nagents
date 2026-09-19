"""
Async HTTP client for LLM API requests using aiohttp.
"""

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import aiohttp

from ..observation import response_chunk
from ..observation import trace_config

if TYPE_CHECKING:
    from .logger import HTTPLogger


class HTTPError(Exception):
    """HTTP request error with status code, URI, response body, and headers."""

    def __init__(
        self,
        status: int,
        message: str,
        body: str | None = None,
        uri: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.body = body
        self.uri = uri
        self.headers: dict[str, str] = headers or {}
        if uri:
            super().__init__(f"HTTP {status}: {message} (uri={uri})")
        else:
            super().__init__(f"HTTP {status}: {message}")

    def is_rate_limited(self) -> bool:
        """Check if this error is a rate limit (429) response."""
        return self.status == 429

    def is_retryable(self) -> bool:
        """Check if this error is retryable (429 or 5xx)."""
        return self.status == 429 or self.status >= 500

    def get_retry_after(self) -> float | None:
        """Extract Retry-After header value in seconds.

        Supports integer seconds format (e.g., "36").
        Returns None if header is missing or unparseable.
        """
        retry_after = self.headers.get("Retry-After") or self.headers.get("retry-after")
        if retry_after is None:
            return None
        try:
            return float(retry_after)
        except ValueError:
            return None

    def get_rate_limit_info(self) -> dict[str, str]:
        """Extract rate limit information from response headers.

        Returns a dict with x-ratelimit-* and x-ms-error-code headers.
        """
        info: dict[str, str] = {}
        rate_limit_prefixes = ("x-ratelimit-", "x-ms-error-code")
        for key, value in self.headers.items():
            if key.lower().startswith(rate_limit_prefixes):
                info[key] = value
        return info


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
            self._session = aiohttp.ClientSession(timeout=self._timeout, trace_configs=[trace_config()])
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
            resp_headers = dict(resp.headers)

            # Log response
            if self._logger:
                self._logger.log_response(url, resp.status, body, resp_headers, self._session_id)

            if resp.status >= 400:
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url, resp_headers)
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
            resp_headers = dict(resp.headers)

            # Log response
            if self._logger:
                self._logger.log_response(url, resp.status, body, resp_headers, self._session_id)

            if resp.status >= 400:
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url, resp_headers)
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
                resp_headers = dict(resp.headers)
                # Log error response
                if self._logger:
                    self._logger.log_response(url, resp.status, body, resp_headers, self._session_id)
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url, resp_headers)

            # Read line by line for SSE
            async for line in resp.content:
                decoded = line.decode("utf-8").strip()
                if not decoded:
                    continue
                if decoded.startswith("data: "):
                    chunk_data = decoded[6:]  # Remove "data: " prefix
                    response_chunk(chunk_data)
                    # Log SSE chunk
                    if self._logger:
                        self._logger.log_sse_chunk(url, chunk_data, self._session_id)
                    yield chunk_data
                # Some APIs use just "data:" without space
                elif decoded.startswith("data:"):
                    chunk_data = decoded[5:]
                    response_chunk(chunk_data)
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
                resp_headers = dict(resp.headers)
                # Log error response
                if self._logger:
                    self._logger.log_response(url, resp.status, body, resp_headers, self._session_id)
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url, resp_headers)

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
                        response_chunk(buffer[:idx])
                        # Log SSE chunk (NDJSON object)
                        if self._logger:
                            self._logger.log_sse_chunk(url, json.dumps(obj), self._session_id)
                        yield obj
                        buffer = buffer[idx:]
                    except json.JSONDecodeError:
                        # Incomplete JSON, wait for more data
                        break

    async def post_multipart(
        self,
        url: str,
        fields: dict[str, str | tuple[str, bytes, str]],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """
        Multipart form data POST request.

        Args:
            url: The URL to POST to
            fields: Form fields. String values are sent as text fields.
                     Tuple values (filename, data, content_type) are sent as
                     file uploads.
            headers: HTTP headers (Content-Type is set automatically)

        Returns:
            Parsed JSON response

        Raises:
            HTTPError: If the request fails
        """
        data = aiohttp.FormData()
        for key, value in fields.items():
            if isinstance(value, tuple):
                filename, file_data, content_type = value
                data.add_field(key, file_data, filename=filename, content_type=content_type)
            else:
                data.add_field(key, value)

        # Log request (without binary data)
        if self._logger:
            log_fields = {
                k: v if isinstance(v, str) else f"<file: {v[0]}, {len(v[1])} bytes>" for k, v in fields.items()
            }
            self._logger.log_request("POST", url, headers, {"multipart": log_fields}, self._session_id)

        session = await self._get_session()
        async with session.post(url, data=data, headers=headers) as resp:
            body = await resp.text()
            resp_headers = dict(resp.headers)

            # Log response
            if self._logger:
                self._logger.log_response(url, resp.status, body, resp_headers, self._session_id)

            if resp.status >= 400:
                raise HTTPError(resp.status, resp.reason or "Request failed", body, url, resp_headers)
            return cast("dict[str, Any]", json.loads(body))

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "HTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
