"""Local-only provider readiness and safe, bounded web error explanations.

Configured means a credential source is present, not that a remote account,
model, subscription or network request has been verified.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from typing import TypedDict

from nagents.error_presentation import transport_error_message
from nagents.harness.provider import HarnessProvider
from nagents.provider import OpenAIProvider

if TYPE_CHECKING:
    from nagents.events import ErrorEvent
    from nagents.harness import Harness


class ProviderSetup(TypedDict):
    configured: bool
    message: str


def provider_setup(harness: Harness) -> ProviderSetup:
    """Never contact a provider or return credential values to the browser."""
    config = harness.config
    if config.demo:
        return {"configured": True, "message": ""}
    provider = harness.agent.provider
    if isinstance(provider, OpenAIProvider) and provider.uses_chatgpt_auth:
        if harness.openai_auth.logged_in() or config.auth == "codex":
            return {"configured": True, "message": ""}
        return {
            "configured": False,
            "message": "ChatGPT login is missing or unreadable in this container. Run ngn login chatgpt "
            "with persistent XDG_DATA_HOME, or choose API-key authentication in Settings.",
        }
    if not isinstance(provider, HarnessProvider) and not (config.provider_id and config.auth == "api-key"):
        # Entra and other provider-specific credential sources are resolved on request.
        return {"configured": True, "message": ""}
    key_env = provider.harness_config.api_key_env if isinstance(provider, HarnessProvider) else config.api_key_env
    if os.environ.get(key_env, "").strip() or (not config.provider_id and harness.login_store.key_for(config.provider)):
        return {"configured": True, "message": ""}
    return {
        "configured": False,
        "message": f"No provider API key is configured. Set ${key_env} in the ngn container "
        "and restart it, or configure a provider in Settings. Live inference needs credentials.",
    }


def provider_error(event: ErrorEvent) -> tuple[str, str]:
    """Classify only known provider codes; upstream messages may contain secrets."""
    code = event.code
    if message := transport_error_message(event):
        return ("chatgpt_network" if code == "CODEX_CONNECTION" else "provider_failure"), message
    if code == "CODEX_AUTH" or code == "CODEX_HTTP_401":
        return (
            "chatgpt_auth",
            "ChatGPT login is unavailable, expired, or rejected. Sign in again with ngn login chatgpt "
            "in the container using persistent XDG_DATA_HOME, or switch to API-key authentication.",
        )
    if code == "CODEX_HTTP_403":
        return "chatgpt_access", "Codex access was denied. Check the ChatGPT account, plan, workspace and model access."
    if code == "CODEX_HTTP_429":
        return "chatgpt_limit", "Codex usage is limited right now. Check your plan limits and retry later."
    if code in {"CODEX_HTTP_400", "CODEX_HTTP_404"}:
        return "chatgpt_model", "Codex rejected this request. Check the selected model and account access."
    if code == "CODEX_CONNECTION":
        return "chatgpt_network", "Codex connection failed or timed out. Check container egress and retry later."
    if code in {"CODEX_REQUEST_INVALID", "CODEX_STREAM_INVALID"}:
        return "chatgpt_request", "Codex could not complete this request. Check the selected model and retry."
    if code and code.isascii() and code.isdigit() and len(code) == 3:
        if code in {"401", "403"}:
            return "api_auth", "Provider rejected the request. Check the container API key and model access."
        if code == "429":
            return "api_limit", "Provider rate or usage limit reached. Check your account and retry later."
        return "api_http", f"Provider returned HTTP {code}. Check the model, API route and provider availability."
    return (
        "provider_failure",
        "Provider request failed. Check the selected connection, model, credentials and server configuration.",
    )
