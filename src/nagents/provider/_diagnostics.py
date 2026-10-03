"""Small, payload-free diagnostics for provider generation failures."""

from __future__ import annotations

import json
import re
import socket
import ssl
from time import monotonic
from typing import Literal

import aiohttp

from ..adapters._validation import ProtocolError
from ..http import HTTPError
from ..observation import scope

FailurePhase = Literal["request", "response", "unknown"]


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
        or (isinstance(error, aiohttp.ClientConnectorError) and isinstance(error.os_error, socket.gaierror))
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


def failure_extra(
    error: Exception, started: float, *, phase: FailurePhase = "unknown", protocol: bool = False
) -> dict[str, object]:
    """Use only fixed categories, local timing, and a validated existing call ID.

    Elapsed time covers this generate() invocation, including any retries and
    backoff. Phase is unknown unless the transport directly observed it. Never
    serialize exception strings, URLs, headers, bodies, or credential values.
    """
    details: dict[str, object] = {
        "category": _category(error, protocol=protocol),
        "phase": phase,
        "generation_elapsed_ms": max(0, round((monotonic() - started) * 1000, 3)),
    }
    call_id = scope.get().get("model_call_id")
    if isinstance(call_id, str) and re.fullmatch(r"[0-9a-f]{32}", call_id):
        details["model_call_id"] = call_id
    return {"transport": details}
