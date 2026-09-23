"""Foundry v1 inference using caller-owned credentials or an API key.

No Azure SDK imports: Azure Identity implements these structural protocols.
Credential selection, token caching and refresh belong to the supplied credential.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import TYPE_CHECKING
from typing import Protocol

from .auth import validate_prefix
from .base import Provider
from .base import ProviderType

if TYPE_CHECKING:
    from ..live import LiveConfig
    from ..types import RetryConfig


class AccessToken(Protocol):
    """Minimal read-only view of an Azure-compatible access token."""

    @property
    def token(self) -> str: ...


class AsyncTokenCredential(Protocol):
    """Structural subset of azure.core.credentials_async.AsyncTokenCredential.

    No close method is required because
    FoundryProvider never takes ownership of the credential.
    """

    def get_token(self, *scopes: str) -> Awaitable[AccessToken]: ...


class TokenCredential(Protocol):
    """Structural subset of azure.core.credentials.TokenCredential.

    Synchronous get_token calls run in a worker thread, never on the voice loop.
    The caller owns the credential and its close lifecycle.
    """

    def get_token(self, *scopes: str) -> AccessToken: ...


class FoundryProvider(Provider):
    """Text and GPT-Live over Foundry's OpenAI-compatible v1 API.

    Pass a sync or async DefaultAzureCredential, ManagedIdentityCredential,
    WorkloadIdentityCredential, ClientSecretCredential, or a structural equivalent.
    Keep it alive until every sharing provider/agent closes, then close it yourself.
    API-key authentication is mutually exclusive with credential authentication.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        credential: AsyncTokenCredential | TokenCredential | None = None,
        api_key: str = "",
        scope: str = "https://ai.azure.com/.default",
        api: str = "responses",
        timeout: float = 120.0,
        retry_config: RetryConfig | None = None,
        live_config: LiveConfig | None = None,
    ) -> None:
        if credential is not None and api_key:
            raise ValueError("Choose credential or api_key, not both")
        if credential is None and not api_key:
            raise ValueError("FoundryProvider requires credential or api_key")
        if not scope or any(char.isspace() for char in scope):
            raise ValueError("scope must be a non-empty token scope without whitespace")
        validate_prefix(base_url)

        async def token() -> str:
            assert credential is not None
            result: AccessToken | Awaitable[AccessToken]
            if inspect.iscoroutinefunction(credential.get_token):
                result = credential.get_token(scope)
            else:
                result = await asyncio.to_thread(credential.get_token, scope)
            # Also supports decorated async methods that expose a synchronous
            # wrapper returning an awaitable. Only invocation goes to the worker;
            # the returned coroutine always executes on the calling event loop.
            if isinstance(result, Awaitable):
                result = await result
            return result.token

        super().__init__(
            ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
            api_key=api_key,
            model=model,
            base_url=base_url,
            api=api,
            timeout=timeout,
            retry_config=retry_config,
            live_config=live_config,
            bearer_token_provider=token if credential is not None else None,
        )

    async def auth_headers(self, url: str) -> dict[str, str]:
        """Foundry Live WS keys use api-key; REST and Entra retain Bearer auth."""
        headers = await super().auth_headers(url)
        if self.bearer_token_provider is None and url.startswith(("ws://", "wss://")):
            return {"api-key": self.api_key}
        return headers
