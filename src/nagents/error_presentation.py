"""Fixed, human-facing explanations without changing provider event records."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .events import ErrorEvent

_TIMEOUT_MESSAGES = {
    "request": "The provider request timed out before a response arrived.",
    "response": "The provider request timed out while receiving the response.",
    "unknown": "The provider request timed out.",
}
_TRANSPORT_MESSAGES = {
    "connect_timeout": "The connection to the provider timed out.",
    "read_timeout": "Timed out waiting for data from the provider.",
    "dns": "The provider address could not be resolved.",
    "tls": "A secure connection to the provider could not be established.",
    "connect": "Could not connect to the provider.",
    "connection_lost": "The connection to the provider was interrupted.",
    "connection": "The provider connection failed.",
    "response_payload": "The provider response could not be read completely.",
}


def transport_error_message(event: ErrorEvent) -> str:
    """Return an allowlisted explanation, or leave the caller's fallback intact.

    Only known generation transport codes and fixed category/phase strings are
    considered. Never render other metadata, raw exceptions, or provider data.
    """
    if not isinstance(event.code, str) or event.code not in {"CODEX_CONNECTION", "PROVIDER_REQUEST_FAILED"}:
        return ""
    if not isinstance(event.extra, dict):
        return ""
    details = event.extra.get("transport")
    if not isinstance(details, dict):
        return ""
    category, phase = details.get("category"), details.get("phase")
    if not isinstance(category, str) or not isinstance(phase, str) or phase not in _TIMEOUT_MESSAGES:
        return ""
    if category == "timeout":
        return _TIMEOUT_MESSAGES[phase]
    return _TRANSPORT_MESSAGES.get(category, "")
