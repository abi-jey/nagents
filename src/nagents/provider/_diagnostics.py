"""Small, payload-free diagnostics for provider generation failures."""

from __future__ import annotations

import json
import logging
import math
import re
import socket
import ssl
from time import monotonic
from typing import Literal
from typing import cast

import aiohttp

from ..adapters._validation import ProtocolError
from ..http import HTTPError
from ..observation import http_attempt
from ..observation import scope

FailurePhase = Literal["request", "response", "unknown"]
logger = logging.getLogger(__name__)
_CATEGORIES = frozenset(
    {
        "protocol",
        "http",
        "tls",
        "dns",
        "connect_timeout",
        "read_timeout",
        "timeout",
        "connect",
        "connection_lost",
        "response_payload",
        "connection",
        "decode",
        "invalid_data",
        "unknown",
    }
)
_PHASES = frozenset({"request", "response", "unknown"})
# Resolve known types once. Never expose arbitrary exception class names: those
# can contain remote data too. MRO lookup retains useful known base classes.
_ERROR_TYPES: dict[type[BaseException], str] = {
    cls: label
    for cls, label in (
        (ProtocolError, "ProtocolError"),
        (HTTPError, "HTTPError"),
        (socket.gaierror, "gaierror"),
        (ssl.SSLError, "SSLError"),
        (ssl.SSLCertVerificationError, "SSLCertVerificationError"),
        (TimeoutError, "TimeoutError"),
        (ConnectionRefusedError, "ConnectionRefusedError"),
        (ConnectionResetError, "ConnectionResetError"),
        (ConnectionAbortedError, "ConnectionAbortedError"),
        (BrokenPipeError, "BrokenPipeError"),
        (OSError, "OSError"),
        (json.JSONDecodeError, "JSONDecodeError"),
        (UnicodeDecodeError, "UnicodeDecodeError"),
        (ValueError, "ValueError"),
        (RuntimeError, "RuntimeError"),
        (Exception, "Exception"),
    )
}
for _name in (
    "ClientError",
    "ClientConnectionError",
    "ClientConnectorError",
    "ClientConnectorDNSError",
    "ClientConnectorCertificateError",
    "ClientConnectorSSLError",
    "ClientSSLError",
    "ClientOSError",
    "ClientPayloadError",
    "ClientResponseError",
    "ClientProxyConnectionError",
    "ClientHttpProxyError",
    "ServerConnectionError",
    "ServerDisconnectedError",
    "ServerTimeoutError",
    "ServerFingerprintMismatch",
    "ConnectionTimeoutError",
    "SocketTimeoutError",
    "ClientConnectionResetError",
):
    _type = getattr(aiohttp, _name, None)
    if isinstance(_type, type) and issubclass(_type, BaseException):
        _ERROR_TYPES[_type] = _name
_ERROR_NAMES = frozenset(_ERROR_TYPES.values())
_MAX_CAUSES = 3
_MAX_ELAPSED_MS = 31_536_000_000  # Bound external metadata to one year.


def _wrapped_error(error: BaseException) -> object:
    # Read built-in descriptors directly, bypassing custom exception getters.
    values = BaseException.__dict__["__dict__"].__get__(error)
    if type(values) is dict:
        return values.get("_os_error", values.get("_certificate_error"))
    return None


def _exception_details(error: BaseException) -> dict[str, object]:
    label = "Exception"
    for cls in type.__getattribute__(type(error), "__mro__"):
        known = next((name for kind, name in _ERROR_TYPES.items() if cls is kind), "")
        if known:
            label = known
            break
    details: dict[str, object] = {"exception": label}
    if isinstance(error, OSError):
        number = OSError.__dict__["errno"].__get__(error)
        if type(number) is int and -65535 <= number <= 65535:
            details["errno"] = number
    return details


def _causes(error: BaseException) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen = {id(error)}
    for _ in range(_MAX_CAUSES):
        cause = BaseException.__dict__["__cause__"].__get__(error)
        if cause is None:
            cause = _wrapped_error(error)
        if cause is None and not BaseException.__dict__["__suppress_context__"].__get__(error):
            cause = BaseException.__dict__["__context__"].__get__(error)
        if not isinstance(cause, BaseException) or id(cause) in seen:
            break
        seen.add(id(cause))
        result.append(_exception_details(cause))
        error = cause
    return result


def _aiohttp_error(error: Exception, name: str) -> bool:
    # Some subclasses were introduced after our minimum aiohttp version.
    error_type = getattr(aiohttp, name, None)
    return isinstance(error_type, type) and issubclass(error_type, Exception) and isinstance(error, error_type)


def _category(error: Exception, *, protocol: bool) -> str:
    if protocol or isinstance(error, ProtocolError):
        return "protocol"
    if isinstance(error, HTTPError):
        return "http"
    if isinstance(error, (aiohttp.ClientSSLError, ssl.SSLError)) or _aiohttp_error(error, "ServerFingerprintMismatch"):
        return "tls"
    if (
        _aiohttp_error(error, "ClientConnectorDNSError")
        or isinstance(error, socket.gaierror)
        or (isinstance(error, aiohttp.ClientConnectorError) and isinstance(_wrapped_error(error), socket.gaierror))
    ):
        return "dns"
    if _aiohttp_error(error, "ConnectionTimeoutError"):
        return "connect_timeout"
    if _aiohttp_error(error, "SocketTimeoutError"):
        return "read_timeout"
    if isinstance(error, TimeoutError):
        # A bare TimeoutError does not identify a connect/read/total deadline.
        return "timeout"
    if isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return "connect"
    if isinstance(error, (aiohttp.ServerDisconnectedError, ConnectionResetError)) or _aiohttp_error(
        error, "ClientConnectionResetError"
    ):
        return "connection_lost"
    if isinstance(error, aiohttp.ClientPayloadError):
        return "response_payload"
    if isinstance(error, aiohttp.ClientConnectionError):
        return "connection"
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return "decode"
    if isinstance(error, ValueError):
        # Includes request/adapter validation; do not call all ValueErrors decoding.
        return "invalid_data"
    return "unknown"


def _token(value: object, allowed: frozenset[str]) -> str:
    return value if type(value) is str and value in allowed else ""


def _identifier(value: object) -> str:
    return value if type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) else ""


def _exception_diagnostic(value: object) -> str:
    if type(value) is not dict:
        return ""
    label = _token(value.get("exception"), _ERROR_NAMES)
    if not label:
        return ""
    number = value.get("errno")
    return f"{label}(errno={number})" if type(number) is int and -65535 <= number <= 65535 else label


def transport_diagnostic(extra: object) -> str:
    """Validate even plugin-supplied metadata before it reaches server logs.

    Exact built-ins avoid invoking user-defined getters, hashes or formatters.
    Unknown fields and exception text are never inspected or rendered.
    """
    try:
        return _transport_diagnostic(extra)
    except Exception:
        # Even a plain dict can contain custom keys with hostile equality.
        return ""


def exception_diagnostic(error: BaseException) -> str:
    """Format only safe exception structure, without generation scope or logging."""
    try:
        details: dict[str, object] = {
            "category": _category(error, protocol=False) if isinstance(error, Exception) else "unknown",
            "phase": "unknown",
            **_exception_details(error),
        }
        if causes := _causes(error):
            details["causes"] = causes
        return transport_diagnostic({"transport": details})
    except Exception:
        # A diagnostic must not replace the original failure even if a plugin's
        # exception overrides class inspection or another classification getter.
        return "transport=unknown phase=unknown"


def _transport_diagnostic(extra: object) -> str:
    if type(extra) is not dict:
        return ""
    details = extra.get("transport")
    if type(details) is not dict:
        return ""
    category, phase = _token(details.get("category"), _CATEGORIES), _token(details.get("phase"), _PHASES)
    if not category or not phase:
        return ""
    fields = [f"transport={category}", f"phase={phase}"]
    if exception := _exception_diagnostic(details):
        fields.append(f"exception={exception}")
    causes = details.get("causes")
    if type(causes) is list:
        labels = [_exception_diagnostic(item) for item in causes[:_MAX_CAUSES]]
        if labels := [label for label in labels if label]:
            fields.append("causes=" + ">".join(labels))
    for key in ("generation_elapsed_ms", "http_attempt_elapsed_ms"):
        elapsed = details.get(key)
        if type(elapsed) in (int, float):
            elapsed_ms = cast("int | float", elapsed)
            if 0 <= elapsed_ms <= _MAX_ELAPSED_MS and math.isfinite(elapsed_ms):
                fields.append(f"{key}={elapsed_ms:.3f}")
    for key in ("model_call_id", "http_attempt_id"):
        if identifier := _identifier(details.get(key)):
            fields.append(f"{key}={identifier}")
    return " ".join(fields)


def failure_extra(
    error: Exception, started: float, *, phase: FailurePhase = "unknown", protocol: bool = False
) -> dict[str, object]:
    """Record and log fixed tokens, numeric errno/timing and local correlation.

    Elapsed time covers this generate() invocation, including any retries and
    backoff. Phase is unknown unless the transport directly observed it. Never
    serialize exception strings, URLs, headers, bodies, or credential values.
    """
    details: dict[str, object] = {
        "category": _category(error, protocol=protocol),
        "phase": phase,
        "generation_elapsed_ms": max(0, round((monotonic() - started) * 1000, 3)),
        **_exception_details(error),
    }
    if causes := _causes(error):
        details["causes"] = causes
    call_id = scope.get().get("model_call_id")
    if _identifier(call_id):
        details["model_call_id"] = call_id
    request = http_attempt.get(None)
    # A task may inherit its caller's previous attempt. Only include attempts
    # started inside this generation; each actual request owns separate state.
    if request is not None and request.started >= started:
        details["http_attempt_id"] = request.id
        details["http_attempt_elapsed_ms"] = max(0, round((monotonic() - request.started) * 1000, 3))
        if phase == "unknown":
            details["phase"] = request.phase
    extra: dict[str, object] = {"transport": details}
    logger.warning("Provider generation failed diagnostic=%s", transport_diagnostic(extra))
    return extra
