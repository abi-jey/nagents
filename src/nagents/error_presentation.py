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
_REMOTE_MESSAGES = {
    "A2A_AUTH_REQUIRED": "Remote agent sign-in is missing or expired. Open its Remote connection settings and sign in.",
    "A2A_AUTH_REFRESH_UNCERTAIN": "Remote agent access could not be safely refreshed. Sign in again; the previous refresh token will not be reused.",
    "A2A_AUTH_REJECTED": "The remote agent rejected the access token. Sign in again in its Remote connection settings.",
    "A2A_AUTH_STORAGE": "Remote credentials could not be loaded safely. Ask the ngn administrator to check credential storage permissions.",
    "A2A_TOKEN_MISSING": "The remote agent's configured server token is missing or invalid. Ask the ngn administrator to configure it.",
    "A2A_FORBIDDEN": "The remote agent denied access. Its administrator must check this user's permissions and the client's allowed scopes.",
    "A2A_RATE_LIMIT": "The remote agent is rate limited. Check its service limits before trying again later.",
    "A2A_SERVER_ERROR": "The remote server returned an error. Its administrator must check its backend logs.",
    "A2A_CONNECTION": "The remote agent could not be reached. Check its server address and network connection.",
    "A2A_TIMEOUT": "The remote agent request timed out. Check the remote server and its task status.",
    "A2A_TLS": "The remote agent's TLS connection could not be verified. Check its certificate and the additional CA bundle in Remote connection settings.",
    "A2A_PROTOCOL": "The remote agent returned an invalid or unsupported response. Check the remote server's A2A configuration and logs.",
    "A2A_UNAVAILABLE": "Remote agents require nagents[a2a] on the ngn server. Ask the server administrator to install it.",
    "A2A_HOST_SETUP": "The remote host has not advertised a public sign-in client. Its administrator must configure that client and register ngn's callback URL in the identity provider. Open Remote connection settings for the exact URL.",
    "A2A_PERMISSION": "Remote execution was not allowed. Check the approval decision and this runtime's read-only or demo restrictions.",
    "A2A_BINDING_CHANGED": "This conversation belongs to a different remote connection. Start a new chat to use the changed connection.",
    "A2A_OUTCOME_UNKNOWN": "The previous remote request has an uncertain outcome. It was not sent again. Inspect its remote task before starting another request.",
    "A2A_TASK_FAILED": "The remote agent reported that its task failed. Check its task status and server logs.",
    "A2A_TASK_REJECTED": "The remote agent rejected this task. Check its capabilities and execution policy.",
    "A2A_TASK_CANCELLED": "The remote task was cancelled. No successful result was returned.",
    "A2A_RUNTIME": "The local remote-agent runtime failed. Ask the ngn administrator to check the correlated backend logs.",
}


def remote_error_message(event: ErrorEvent) -> str:
    """Explain a known remote code without trusting remote text or metadata."""
    if type(event.code) is not str or event.code not in _REMOTE_MESSAGES:
        return ""
    message = _REMOTE_MESSAGES[event.code]
    if type(event.extra) is dict:
        outcome = event.extra.get("remote_outcome")
        if outcome == "not_sent":
            message += " No task was sent."
        elif outcome == "rejected":
            message += " The remote server did not accept a task. This request was not automatically retried."
        elif outcome == "unknown" and event.code != "A2A_OUTCOME_UNKNOWN":
            message += " The request may have reached the remote agent. It was not automatically retried; inspect its task before sending it again."
    return message


def transport_error_message(event: ErrorEvent) -> str:
    """Return an allowlisted explanation, or leave the caller's fallback intact.

    Only known generation transport codes and fixed category/phase strings are
    considered. Never render other metadata, raw exceptions, or provider data.
    """
    if type(event.code) is not str or event.code not in {"CODEX_CONNECTION", "PROVIDER_REQUEST_FAILED"}:
        return ""
    if type(event.extra) is not dict:
        return ""
    details = event.extra.get("transport")
    if type(details) is not dict:
        return ""
    category, phase = details.get("category"), details.get("phase")
    if type(category) is not str or type(phase) is not str or phase not in _TIMEOUT_MESSAGES:
        return ""
    if category == "timeout":
        return _TIMEOUT_MESSAGES[phase]
    return _TRANSPORT_MESSAGES.get(category, "")
