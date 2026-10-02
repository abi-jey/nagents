"""Resolve voice authentication without mixing ChatGPT tokens and API keys."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Literal

from nagents.harness.connection import _codex_files_exist
from nagents.provider.openai import CodexConfigError
from nagents.provider.openai import _load_config

if TYPE_CHECKING:
    from nagents.harness.auth import OpenAIAuth
    from nagents.harness.providers import ProviderProfile
    from nagents.provider.openai import CodexCredentials

VoiceAuth = Literal["api-key", "chatgpt", "entra"]
LoginCredentials = Callable[[], Awaitable["CodexCredentials"]]
LOGIN_VOICES = ("arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale")


def resolve_voice_auth(
    profile: ProviderProfile, auth: OpenAIAuth, model: str
) -> tuple[VoiceAuth, LoginCredentials | None]:
    if profile.auth == "entra":
        return "entra", None
    if profile.kind != "openai" or profile.auth == "api-key":
        return "api-key", None
    if profile.auth == "chatgpt":
        return "chatgpt", auth.credentials if auth.logged_in() else None
    if profile.auth == "auto" and auth.logged_in():
        return "chatgpt", auth.credentials
    if profile.auth in {"auto", "codex"}:
        codex_configured = _codex_files_exist()
        if profile.auth == "codex" and not codex_configured:
            return "chatgpt", None
        try:
            selected = _load_config(model=model)
        except CodexConfigError:
            # An invalid selected Codex login is not permission to send speech
            # through another account's environment API key.
            return ("chatgpt" if profile.auth == "codex" or codex_configured else "api-key"), None
        if selected.oauth:
            return "chatgpt", selected.credentials
    return "api-key", None
