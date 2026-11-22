"""Fixed, actionable provider error text for the Rust host.

Only HTTP status numbers and allowlisted codes/categories are interpreted.
Upstream messages, payloads and arbitrary plugin codes are never echoed.
"""

from __future__ import annotations

import math

from nagents.events import ErrorEvent
from nagents.events import RateLimitEvent

GENERIC = "The provider request failed. Check the provider, model and credentials, then send again."

_TIMEOUT = {
    "request": "The provider request timed out before a response arrived. Send again, or check the network.",
    "response": "The provider timed out while sending its response. Send again, or check the network.",
}
_TRANSPORT = {
    "connect_timeout": "The connection to the provider timed out. Check the network and base URL.",
    "read_timeout": "Timed out waiting for data from the provider. Send again, or check the network.",
    "dns": "The provider address could not be resolved. Check the base URL and network.",
    "tls": "A secure connection to the provider could not be established. Check the base URL and certificates.",
    "connect": "Could not connect to the provider. Check the base URL and network.",
    "connection_lost": "The connection to the provider was interrupted. Send again.",
    "connection": "The provider connection failed. Check the base URL and network.",
    "response_payload": "The provider response could not be read completely. Send again.",
}
_CODES = {
    "MODEL_NOT_FOUND": "The provider could not find this model. Check the model ID in the provider settings.",
    "CODEX_AUTH": "The ChatGPT sign-in is missing or expired (check credentials). Run `ngn auth login` again.",
    "PROVIDER_PROTOCOL_ERROR": "The provider returned a response ngn could not validate. Check the API route setting.",
}


def _status(code: str) -> int:
    digits = code.removeprefix("CODEX_HTTP_")
    if len(digits) == 3 and digits.isdigit():
        return int(digits)
    return 0


def _http_text(status: int) -> str:
    if status == 401:
        return f"The provider rejected the credentials (HTTP {status}). Check the API key or sign in again."
    if status == 403:
        return f"The provider denied access (HTTP {status}). Check that this account can use the selected model."
    if status == 404:
        return f"The provider could not find the model or endpoint (HTTP {status}). Check the model ID and base URL."
    if status == 429:
        return (
            f"The provider rate limit or usage quota was reached (HTTP {status}) and retries did not succeed. "
            "Wait a moment, then send again."
        )
    if 400 <= status < 500:
        return f"The provider rejected the request (HTTP {status}). Check the model and request settings."
    if 500 <= status < 600:
        return f"The provider had a server error (HTTP {status}). Try again shortly."
    return ""


def _transport_text(event: ErrorEvent) -> str:
    extra = event.extra if isinstance(event.extra, dict) else {}
    details = extra.get("transport")
    if not isinstance(details, dict):
        return ""
    category, phase = details.get("category"), details.get("phase")
    if category == "timeout" and isinstance(phase, str):
        return _TIMEOUT.get(phase, "")
    if isinstance(category, str):
        return _TRANSPORT.get(category, "")
    return ""


def provider_error_text(event: ErrorEvent) -> str:
    """Explain a failed model request without exposing provider data."""
    code = event.code if isinstance(event.code, str) else ""
    if status := _status(code):
        return _http_text(status) or GENERIC
    if code in _CODES:
        return _CODES[code]
    if code in {"PROVIDER_REQUEST_FAILED", "CODEX_CONNECTION"}:
        return _transport_text(event) or GENERIC
    return GENERIC


def retry_notice_text(event: RateLimitEvent) -> str:
    """Describe an automatic provider retry that is about to happen."""
    status = event.status_code if isinstance(event.status_code, int) else 0
    attempt = event.attempt if isinstance(event.attempt, int) else 0
    limit = event.max_retries if isinstance(event.max_retries, int) else 0
    delay = event.retry_after if isinstance(event.retry_after, (int, float)) else math.nan
    if not (
        (status == 429 or 500 <= status < 600)
        and 1 <= attempt <= limit <= 100
        and math.isfinite(delay)
        and 0 <= delay <= 3600
    ):
        return "Provider request failed. Retrying shortly…"
    reason = "rate limit" if status == 429 else "server error"
    return f"Provider {reason} (HTTP {status}). Retrying in {round(delay)}s (attempt {attempt} of {limit})…"
