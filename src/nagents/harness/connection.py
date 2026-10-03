"""Construct concrete providers from a named connection without persisting keys."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from nagents.provider import FoundryProvider
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.openai import CodexConfigError
from nagents.provider.openai import _load_config
from nagents.types import RetryConfig

from .provider import HarnessProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nagents.events import Event
    from nagents.live import LiveConfig
    from nagents.provider.foundry import AsyncTokenCredential
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition

    from .auth import OpenAIAuth
    from .config import HarnessConfig
    from .providers import ProviderProfile


class _EntraFoundryProvider(FoundryProvider):
    def __init__(
        self,
        credential: AsyncTokenCredential,
        profile: ProviderProfile,
        model: str,
        live: LiveConfig | None = None,
    ) -> None:
        self._owned_credential = credential
        super().__init__(
            base_url=profile.base_url,
            model=model,
            credential=credential,
            scope=profile.scope,
            api="responses" if profile.api == "auto" else profile.api,
            live_config=live,
            timeout=profile.request_timeout,
        )

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self._owned_credential.close()  # type: ignore[attr-defined]


class _EnvFoundryProvider(FoundryProvider):
    """Read the current environment on each call, including Live authentication."""

    def __init__(self, profile: ProviderProfile, model: str) -> None:
        self._env = profile.key_env
        super().__init__(
            base_url=profile.base_url,
            model=model,
            api_key="deferred-until-live-request",
            api="responses" if profile.api == "auto" else profile.api,
            timeout=profile.request_timeout,
        )

    def _key(self) -> None:
        key = os.environ.get(self._env, "").strip()
        if not key:
            raise ValueError(f"Set ${self._env} before a Foundry request")
        self.api_key = key

    async def auth_headers(self, url: str) -> dict[str, str]:
        self._key()
        return await super().auth_headers(url)

    async def get_model_list(self) -> list[str]:
        self._key()
        return await super().get_model_list()

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        self._key()
        async for event in super().generate(messages, tools, config, stream, verify_model):
            yield event


def _codex_files_exist() -> bool:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return (home / "auth.json").exists() or (home / "config.toml").exists()


def build_provider(profile: ProviderProfile, config: HarnessConfig, auth: OpenAIAuth) -> Provider:
    """Use the library's provider-specific discovery and keep Azure SDK optional."""
    profile.validate()
    if config.demo:
        return HarnessProvider(config, request_timeout=profile.request_timeout)
    if profile.auth == "entra":
        try:
            from azure.identity.aio import DefaultAzureCredential
        except ImportError:
            raise ValueError("Foundry Entra authentication requires the optional azure-identity package") from None
        return _EntraFoundryProvider(DefaultAzureCredential(), profile, config.model)
    if profile.kind in {"foundry", "azure_openai_compatible_v1"}:
        return _EnvFoundryProvider(profile, config.model)
    if profile.kind == "openai" and profile.auth == "chatgpt":
        return OpenAIProvider(auth.credentials, model=config.model, timeout=profile.request_timeout)
    if profile.kind == "openai" and profile.auth in {"codex", "auto"}:
        if profile.auth == "auto" and auth.logged_in():
            return OpenAIProvider(auth.credentials, model=config.model, timeout=profile.request_timeout)
        if profile.auth == "codex" and not _codex_files_exist():
            raise CodexConfigError("Local Codex configuration was not found; sign in with Codex first")
        if profile.auth == "auto" and not _codex_files_exist():
            return HarnessProvider(replace(config, auth="api-key"), None, request_timeout=profile.request_timeout)
        try:
            return OpenAIProvider(model=config.model, timeout=profile.request_timeout)
        except CodexConfigError:
            if profile.auth == "codex":
                raise
            # A broken/expired Codex login is not permission to silently send
            # the same prompt using a different API account.
            if _codex_files_exist():
                raise
            # No usable Codex login: use the named environment variable lazily.
    # No fallback to the legacy single-login key for named connections.
    selected = replace(config, auth="api-key") if config.auth != "api-key" else config
    return HarnessProvider(selected, None, request_timeout=profile.request_timeout)


def build_live_provider(profile: ProviderProfile, options: LiveConfig, key: str, model: str) -> Provider:
    """Use the same connection identity and auth reference as the text provider."""
    if profile.kind in {"foundry", "azure_openai_compatible_v1"}:
        if profile.auth == "entra":
            try:
                from azure.identity.aio import DefaultAzureCredential
            except ImportError:
                raise ValueError("Foundry Entra authentication requires azure-identity") from None
            return _EntraFoundryProvider(DefaultAzureCredential(), profile, model, options)
        return FoundryProvider(
            base_url=profile.base_url,
            model=model,
            api_key=key,
            live_config=options,
            timeout=profile.request_timeout,
            retry_config=RetryConfig(max_retries=0),
        )
    if profile.kind not in {"openai", "openai_compatible"}:
        raise ValueError("This provider does not support GPT-Live")
    if profile.kind == "openai" and profile.auth in {"codex", "auto"} and not key:
        return OpenAIProvider(model=model, live_config=options, timeout=profile.request_timeout)
    return Provider(
        ProviderType.OPENAI_COMPATIBLE,
        api_key=key,
        model=model,
        base_url=profile.base_url or None,
        api="responses",
        live_config=options,
        timeout=profile.request_timeout,
        retry_config=RetryConfig(max_retries=0),
    )


def live_auth_available(profile: ProviderProfile, model: str) -> bool:
    """Local readiness only. Never returns or logs credentials."""
    if profile.auth == "entra":
        return True
    if os.environ.get(profile.key_env, ""):
        return True
    if profile.kind == "openai" and profile.auth in {"codex", "auto"}:
        if profile.api_key_env and profile.key_env != "OPENAI_API_KEY" and not _codex_files_exist():
            return False
        try:
            return bool(_load_config(model=model, for_live=True).api_key)
        except CodexConfigError:
            return False
    return False
