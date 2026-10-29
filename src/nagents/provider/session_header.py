"""Opt-in conversation correlation headers, separate from authentication."""

import re

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_RESERVED = frozenset(
    {
        "accept",
        "accept-charset",
        "accept-encoding",
        "accept-language",
        "api-key",
        "authentication",
        "authorization",
        "cache-control",
        "connection",
        "content-disposition",
        "content-encoding",
        "content-length",
        "content-type",
        "cookie",
        "date",
        "expect",
        "host",
        "http-referer",
        "keep-alive",
        "max-forwards",
        "range",
        "pragma",
        "origin",
        "originator",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "referer",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "user-agent",
        "via",
        "x-api-key",
        "x-api-token",
        "x-auth-token",
        "x-access-token",
        "x-authorization",
        "x-litellm-api-key",
        "x-goog-api-key",
        "x-ngn-token",
        "x-http-method-override",
        "chatgpt-account-id",
        "anthropic-version",
        "anthropic-beta",
        "openai-organization",
        "openai-project",
        "openai-beta",
    }
)


def validate_session_id_header(value: object) -> str:
    """Allow an empty opt-out or a bounded ASCII HTTP token without reserved semantics."""
    if not isinstance(value, str):
        raise ValueError("session_id_header must be a string")
    if not value:
        return value
    lowered = value.lower()
    if (
        not _TOKEN.fullmatch(value)
        or lowered in _RESERVED
        or lowered.startswith(("sec-", "proxy-", "if-", "x-forwarded-", "x-openai-internal-", "x-amz-"))
        or lowered == "forwarded"
    ):
        raise ValueError(
            "session_id_header must be an HTTP header name of at most 128 ASCII characters, excluding authentication and protocol headers"
        )
    return value


def validate_session_header_value(value: str) -> str:
    """Transmit opaque visible ASCII IDs verbatim; never interpolate invalid values in errors."""
    if not 1 <= len(value) <= 1024 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("Session header values must contain 1 to 1024 visible ASCII characters without spaces")
    return value
