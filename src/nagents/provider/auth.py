"""Async bearer authentication without owning or persisting caller credentials."""

from collections.abc import Awaitable
from collections.abc import Callable
from urllib.parse import urlsplit

BearerTokenProvider = Callable[[], Awaitable[str]]


def validate_endpoint(url: str, *, websocket: bool = False) -> None:
    """Validate before acquiring credentials; permit loopback HTTP for local tests."""
    parsed = urlsplit(url)
    schemes = {"ws", "wss"} if websocket else {"http", "https"}
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or any(ord(char) <= 32 or ord(char) >= 127 for char in url)
    ):
        raise ValueError("Invalid authentication endpoint")
    # Accessing port also rejects malformed port specifications.
    _ = parsed.port
    if parsed.scheme in {"http", "ws"} and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Authentication requires HTTPS/WSS (except loopback)")


def validate_prefix(url: str) -> None:
    validate_endpoint(url)
    if (
        urlsplit(url)
        .path.rstrip("/")
        .endswith(("/responses", "/chat/completions", "/messages", "/completions", "/live/sessions", "/realtime"))
    ):
        raise ValueError("base_url must be an API prefix, not a generation endpoint")


async def bearer_headers(callback: BearerTokenProvider) -> dict[str, str]:
    try:
        token = await callback()
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError
    except Exception:
        raise ValueError("Bearer token provider failed or returned an invalid token") from None
    return {"Authorization": f"Bearer {token}"}
