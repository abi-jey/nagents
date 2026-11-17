"""
Unified LLM provider for all supported backends.

Supports:
- OpenAI API (and compatible: Gemini via compat, OpenRouter, Ollama, etc.)
- Gemini Native REST API
"""

import asyncio
import hashlib
import logging
import math
import unicodedata
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import replace
from enum import Enum
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import quote
from urllib.parse import urlsplit

from ..adapters._validation import ProtocolError
from ..adapters._validation import list_data
from ..events import ErrorEvent
from ..events import Event
from ..events import RateLimitEvent
from ..events import ToolCallProgressEvent
from ..events import Usage
from ..exceptions import ModelListError
from ..http import HTTPClient
from ..http import HTTPError
from ..media import MediaCapabilities
from ..media import get_media_capabilities
from ..types import GenerationConfig
from ..types import Message
from ..types import ReasoningEffort
from ..types import RetryConfig
from ..types import ToolDefinition
from ._diagnostics import failure_extra
from .auth import BearerTokenProvider
from .auth import bearer_headers
from .auth import validate_endpoint
from .auth import validate_prefix
from .cache import ConversationCacheKey
from .cache import ConversationCacheSnapshot
from .cache import ConversationCacheTracker
from .context import ContextMetadata
from .context import ContextWindow
from .context import fetch_context_metadata
from .context import validate_context_window
from .context import validate_model_context_windows
from .gateway import GatewayHTTPClient
from .reasoning import native_openai_api
from .reasoning import reasoning_effort_options
from .reasoning import validate_reasoning_choice
from .session_header import validate_session_header_value
from .session_header import validate_session_id_header

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Sequence

    from ..adapters._additional_tools import ToolTimeline
    from ..compaction import CompactionSnapshot
    from ..context_stats import ContextStats
    from ..events import TokenUsage
    from ..http import HTTPLogger
    from ..live import LiveConfig
    from ..realtime import RealtimeConfig

logger = logging.getLogger(__name__)


class ProviderType(Enum):
    """Supported provider types."""

    OPENAI_COMPATIBLE = "openai_compatible"  # OpenAI, Gemini-via-compat, Ollama, etc.
    OPENROUTER = "openrouter"  # OpenRouter (OpenAI-compatible with reasoning support)
    LITELLM = "litellm"  # Self-hosted API-key gateway; requires an explicit base URL
    GEMINI_NATIVE = "gemini_native"  # Native Gemini REST API
    ANTHROPIC = "anthropic"  # Anthropic Claude API
    AZURE_OPENAI_COMPATIBLE_V1 = "azure_openai_compatible_v1"  # Azure OpenAI Service
    AZURE_OPENAI_COMPATIBLE = (
        "azure_openai_compatible"  # Azure OpenAI Service (new name for v1, will be default in future)
    )


class Provider:
    """
    Unified LLM provider for all supported backends.

    Example:
        provider = Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key="sk-...",
            model="gpt-4o",
        )

        async for event in provider.generate(messages, tools):
            print(event)
    """

    def __init__(
        self,
        provider_type: ProviderType,
        api_key: str = "",
        model: str = "",
        base_url: str | None = None,
        timeout: float = 120.0,
        api_version: str | None = None,
        retry_config: RetryConfig | None = None,
        realtime_config: "RealtimeConfig | None" = None,
        api: str = "auto",
        live_config: "LiveConfig | None" = None,
        *,
        bearer_token_provider: BearerTokenProvider | None = None,
        context_window: int = 0,
        session_id_header: str = "",
    ):
        """
        Initialize the provider.

        Args:
            provider_type: The type of provider to use
            api_key: API key for authentication
            model: Model identifier to use (for Azure, this is the deployment name)
            base_url: Optional custom base URL (overrides defaults).
                      For Azure OpenAI: https://{resource}.cognitiveservices.azure.com
            timeout: Request timeout in seconds
            api_version: API version (required for Azure OpenAI, e.g., "2024-05-01-preview")
            retry_config: Retry configuration for rate limit and server error handling.
                          Defaults to RetryConfig() (3 retries, 5s base delay).
                          Set RetryConfig(max_retries=0) to disable retry.
            realtime_config: Optional :class:`~nagents.realtime.RealtimeConfig`
                for Realtime speech-to-speech sessions. When set, voice sessions
                reuse the provider's model and this configuration.
            api: HTTP contract: auto, chat_completions, responses, or messages.
                Auto preserves native providers and selects
                chat_completions for LiteLLM and OpenRouter. base_url is an API
                 prefix, not a full generation endpoint.
            live_config: GPT-Live voice/delegation settings for Agent.run() with audio.
            bearer_token_provider: Optional async callable returning a bearer token.
                Mutually exclusive with api_key; supports OpenAI-compatible v1
                chat/Responses and GPT-Live. Called per request attempt/connection.
                The caller owns the credential and its cache/refresh/close lifecycle.
            session_id_header: Optional HTTP header carrying the current conversation ID.
                Empty disables it. Agent supplies the ID; direct callers use
                set_session_id() or session_context(). Only generation requests carry it.
        """
        self.session_id_header = session_id_header
        self.context_window = validate_context_window(context_window)
        self.model_context_windows: dict[str, int] = {}
        self._conversation_cache = ConversationCacheTracker()
        self._responses_tool_timelines: dict[tuple[str, str, str], ToolTimeline] = {}
        self._responses_tools_unsupported: set[tuple[str, str]] = set()
        self._context_auth_identity = ""
        self._context_metadata = ContextMetadata()
        self._context_metadata_candidate = ContextMetadata()
        self._context_metadata_lock = asyncio.Lock()
        self.provider_type = provider_type
        self.api_key = api_key
        self.bearer_token_provider = bearer_token_provider
        if bearer_token_provider is not None:
            if api_key:
                raise ValueError("Choose api_key or bearer_token_provider, not both")
            if not callable(bearer_token_provider):
                raise ValueError("bearer_token_provider must be an async callable")
            if provider_type not in {ProviderType.OPENAI_COMPATIBLE, ProviderType.AZURE_OPENAI_COMPATIBLE_V1}:
                raise ValueError("Bearer token authentication requires an OpenAI-compatible v1 provider")
            if api not in {"auto", "chat_completions", "responses"} or realtime_config is not None:
                raise ValueError("Bearer token authentication supports text chat/responses and GPT-Live, not Realtime")
        self.model = model
        self.api_version = api_version
        self.retry_config = retry_config or RetryConfig()
        self.realtime_config = realtime_config
        self.live_config = live_config
        if live_config is not None and realtime_config is not None:
            raise ValueError("Choose live_config or realtime_config, not both")
        if live_config is not None and provider_type not in {
            ProviderType.OPENAI_COMPATIBLE,
            ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
        }:
            raise ValueError("GPT-Live requires an OpenAI-compatible v1 provider")
        if api not in {"auto", "chat_completions", "responses", "messages"}:
            raise ValueError("api must be auto, chat_completions, responses, or messages")
        if provider_type == ProviderType.LITELLM and not base_url:
            raise ValueError("LiteLLM requires an explicit base_url pointing to your gateway")
        default_api = "messages" if provider_type == ProviderType.ANTHROPIC else "chat_completions"
        if provider_type == ProviderType.OPENAI_COMPATIBLE:
            default_api = native_openai_api(model, base_url=base_url or "")
            if default_api == "auto":
                default_api = "chat_completions"
        if provider_type == ProviderType.GEMINI_NATIVE:
            default_api = "auto"
        self.api = default_api if api == "auto" else api
        if provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1 and self.api not in {
            "chat_completions",
            "responses",
        }:
            raise ValueError("Azure v1 supports chat_completions or responses")
        if (
            provider_type
            not in {
                ProviderType.LITELLM,
                ProviderType.OPENAI_COMPATIBLE,
                ProviderType.OPENROUTER,
                ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
            }
            and self.api != default_api
        ):
            raise ValueError("This provider does not support the selected HTTP API contract")
        if provider_type == ProviderType.OPENAI_COMPATIBLE and self.api == "messages" and not base_url:
            raise ValueError("The messages API requires an explicit compatible base_url or an Anthropic provider")
        if base_url:
            parsed = urlsplit(base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or any(char.isspace() for char in base_url)
            ):
                raise ValueError("base_url must be an HTTP(S) API prefix")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base_url must not contain credentials, query parameters, or fragments")
            if parsed.path.rstrip("/").endswith(("/chat/completions", "/responses", "/messages", "/completions")):
                raise ValueError("base_url must be an API prefix, not a generation endpoint")
        self._http = (
            GatewayHTTPClient(timeout=timeout)
            if bearer_token_provider is not None
            or provider_type in {ProviderType.LITELLM, ProviderType.OPENROUTER, ProviderType.AZURE_OPENAI_COMPATIBLE_V1}
            or api != "auto"
            or self.api == "responses"
            else HTTPClient(timeout=timeout)
        )
        self._model_verified: bool | None = None  # None = not checked, True/False = result
        self._model_list_timeout = min(timeout, 30.0) if math.isfinite(timeout) and timeout > 0 else 30.0

        # Validate Azure-specific requirements
        if provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1 and not base_url:
            raise ValueError(
                "base_url is required for Azure OpenAI. Format: https://{resource}.cognitiveservices.azure.com/openai"
            )
        if provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE:
            if not api_version:
                raise ValueError("api_version is required for Azure OpenAI")
            if not base_url:
                raise ValueError("base_url is required for Azure OpenAI")
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif provider_type == ProviderType.OPENAI_COMPATIBLE:
            self.base_url = "https://api.openai.com/v1"
        elif provider_type == ProviderType.OPENROUTER:
            self.base_url = "https://openrouter.ai/api/v1"
        elif provider_type == ProviderType.ANTHROPIC:
            self.base_url = "https://api.anthropic.com/v1"
        elif provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
            # Azure requires base_url, this should not be reached due to validation above
            raise ValueError("base_url is required for Azure OpenAI")
        else:  # GEMINI_NATIVE
            self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        if bearer_token_provider is not None or live_config is not None:
            validate_prefix(self.base_url)
        if not self.api_key and bearer_token_provider is None:
            raise ValueError("API key is required for provider initialization")

    def configure_context_window(self, context_window: int, model_context_windows: dict[str, int]) -> None:
        """Apply a trusted connection's defaults and exact-model overrides."""
        self.context_window = validate_context_window(context_window)
        self.model_context_windows = validate_model_context_windows(model_context_windows)

    def context_stats(
        self,
        *,
        messages: "Sequence[Message]" = (),
        tools: "Sequence[ToolDefinition]" = (),
        system_prompt: str = "",
        instructions: str = "",
        skill_manifest: str = "",
        skill_text: str = "",
        observed: "TokenUsage | None" = None,
        additional_messages: "Sequence[Message]" = (),
        streaming_reply: str = "",
        compaction: "CompactionSnapshot | None" = None,
        context_window: "ContextWindow | None" = None,
    ) -> "ContextStats":
        """Account for context through the library's normalized provider usage.

        The last measured response includes input, output and reasoning once,
        according to this provider's inclusion semantics. Newly appended data
        and unsaved text are explicitly estimated. ``total_tokens`` and its
        breakdown describe estimated next input; ``context_tokens`` describes
        the measured response budget plus that pending data. Neither is the
        session's accumulated billing total or a tokenizer count of an unseen
        future request. Call ``resolve_context_window`` first to refresh optional
        model metadata; this snapshot itself makes no HTTP requests.
        """
        from ..context_stats import estimate_context_stats

        window = context_window if context_window is not None else self.cached_context_window()
        return estimate_context_stats(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            instructions=instructions,
            skill_manifest=skill_manifest,
            skill_text=skill_text,
            observed=observed,
            additional_messages=additional_messages,
            streaming_reply=streaming_reply,
            compaction=compaction,
            context_window=window.tokens,
            context_window_source=window.source,
            provider=self.provider_type.value,
            model=self.model,
        )

    def _context_metadata_identity(self) -> tuple[str, str, str]:
        auth = hashlib.sha256(
            f"{self.api_key}:{id(self.bearer_token_provider)}:{self._context_auth_identity}".encode()
        ).hexdigest()
        return self.provider_type.value, self.base_url, auth

    def _context_usage_identity(self) -> str:
        """Usage follows a credential source, not routine bearer token rotation."""
        source = f"bearer:{id(self.bearer_token_provider)}" if self.bearer_token_provider is not None else self.api_key
        return hashlib.sha256(source.encode()).hexdigest()

    def _prepare_context_usage(self) -> None:
        """Deferred API-key providers may resolve local credentials without HTTP calls."""

    async def _prepare_context_metadata(self) -> None:
        """Resolve local keys only; remote token callbacks belong to actual HTTP requests."""
        self._prepare_context_usage()

    def cached_context_window(self) -> ContextWindow:
        """Resolve precedence synchronously, without network I/O."""
        from ..compaction import guess_model_context_limit

        configured = self.model_context_windows.get(self.model, self.context_window)
        if configured:
            return ContextWindow(configured, "configured")
        identity = self._context_metadata_identity()
        if self._context_metadata_candidate.identity == identity:
            self._context_metadata = self._context_metadata_candidate
            self._context_metadata_candidate = ContextMetadata()
        self._context_metadata.select(identity)
        discovered = self._context_metadata.limits.get(self.model, 0)
        if discovered:
            return ContextWindow(discovered, "provider")
        return ContextWindow(guess_model_context_limit(self.model), "heuristic")

    def context_metadata_snapshot(self) -> ContextMetadata:
        """Copy bounded model capabilities and backoff; no credentials or transport ownership."""
        self._context_metadata.select(self._context_metadata_identity())
        return self._context_metadata.copy()

    def cached_reasoning_efforts(self, model: str = "") -> tuple[ReasoningEffort, ...]:
        """Return exact catalog choices or conservative documented route defaults."""
        identity = self._context_metadata_identity()
        if self._context_metadata_candidate.identity == identity:
            self._context_metadata = self._context_metadata_candidate
            self._context_metadata_candidate = ContextMetadata()
        self._context_metadata.select(identity)
        selected = model or self.model
        return self._context_metadata.reasoning_efforts.get(
            selected,
            reasoning_effort_options(self.provider_type.value, selected, api=self.api, base_url=self.base_url),
        )

    async def resolve_reasoning_efforts(self) -> tuple[ReasoningEffort, ...]:
        """Bound capability discovery to the same credential-scoped catalog as context."""
        prepared = False
        try:
            async with asyncio.timeout(min(self._model_list_timeout, 3.0)):
                await self._prepare_context_metadata()
                prepared = True
                self.cached_reasoning_efforts()
                await self._resolve_prepared_context_window()
        except Exception:
            if not prepared:
                self._context_metadata = ContextMetadata()
                self._context_metadata_candidate = ContextMetadata()
            logger.debug("Reasoning effort metadata unavailable; using documented model choices")
        return self.cached_reasoning_efforts()

    def restore_context_metadata(self, snapshot: ContextMetadata) -> None:
        """The caller must also match its trusted connection profile before reuse."""
        self._context_metadata_candidate = snapshot.copy()
        self.cached_context_window()

    def copy_context_metadata(self, source: "Provider") -> None:
        """Reuse discovery only after the caller verifies the same trusted connection."""
        self.restore_context_metadata(source.context_metadata_snapshot())

    async def resolve_context_window(self) -> ContextWindow:
        """Bound credentials, coalesced waiting, and metadata to one three-second deadline."""
        if self.cached_context_window().source == "configured":
            return self.cached_context_window()
        prepared = False
        try:
            async with asyncio.timeout(min(self._model_list_timeout, 3.0)):
                await self._prepare_context_metadata()
                prepared = True
                return await self._resolve_prepared_context_window()
        except Exception:
            if not prepared:
                self._context_metadata = ContextMetadata()
                self._context_metadata_candidate = ContextMetadata()
            # No upstream body, credential, or exception text enters diagnostics.
            logger.debug("Model context metadata unavailable; using configured or estimated window")
            return self.cached_context_window()

    async def _resolve_prepared_context_window(self) -> ContextWindow:
        self.cached_context_window()  # Apply only a matching metadata snapshot after auth resolution.
        if not self._context_metadata_lock.locked() and monotonic() < self._context_metadata.retry_at.get(
            self.model, 0.0
        ):
            return self.cached_context_window()
        async with self._context_metadata_lock:
            self.cached_context_window()
            if monotonic() < self._context_metadata.retry_at.get(self.model, 0.0):
                return self.cached_context_window()
            self._context_metadata.backoff(self.model)
            await self._fetch_context_metadata()
        return self.cached_context_window()

    async def _fetch_context_metadata(self) -> None:
        """Only known metadata contracts; never guess extra hosts or follow redirects."""
        kind = self.provider_type
        if kind not in {
            ProviderType.LITELLM,
            ProviderType.OPENROUTER,
            ProviderType.ANTHROPIC,
            ProviderType.GEMINI_NATIVE,
        }:
            # OpenAI's public /models has no context field. Compatible catalogs
            # can populate it during explicit discovery without another request.
            return
        validate_prefix(self.base_url)
        route = (self.provider_type, self.base_url)
        prefix = self._api_prefix()
        headers = await self.auth_headers(prefix)
        if route != (self.provider_type, self.base_url):
            return
        # Bind the response to the credential actually accepted for this HTTP
        # request, not the earlier cache state before a bearer refresh.
        identity = self._context_metadata_identity()
        self._context_metadata.select(identity)
        model = self.model
        self._context_metadata.backoff(model)
        key, nested = "id", False
        if kind == ProviderType.LITELLM:
            url, key, nested = prefix.removesuffix("/v1") + "/model/info", "model_name", True
        elif kind == ProviderType.ANTHROPIC:
            url = prefix + "/models/" + quote(model, safe="")
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        elif kind == ProviderType.GEMINI_NATIVE:
            url = prefix + "/models/" + quote(model.removeprefix("models/"), safe="")
            headers = {"x-goog-api-key": self.api_key}
            key = "name"
        else:
            url = prefix + "/models"
        response = await fetch_context_metadata(url, headers, min(self._model_list_timeout, 3.0))
        if identity != self._context_metadata_identity():
            return
        rows = response.get("data", []) if kind in {ProviderType.LITELLM, ProviderType.OPENROUTER} else [response]
        self._context_metadata_candidate = ContextMetadata()
        self._context_metadata.update(rows, id_key=key, nested=nested)
        if kind == ProviderType.GEMINI_NATIVE:
            prefixed = "models/" + model.removeprefix("models/")
            if prefixed in self._context_metadata.limits:
                self._context_metadata.limits[model] = self._context_metadata.limits[prefixed]

    async def auth_headers(self, url: str) -> dict[str, str]:
        """Fresh authentication per attempt; caller owns the credential lifetime."""
        validate_prefix(self.base_url)
        validate_endpoint(url, websocket=url.startswith(("ws:", "wss:")))
        if self.bearer_token_provider is not None:
            headers = await bearer_headers(self.bearer_token_provider)
            self._context_auth_identity = hashlib.sha256(headers["Authorization"].encode()).hexdigest()
            return headers
        return {"Authorization": f"Bearer {self.api_key}"}

    def live_endpoint(self, *, websocket: bool = False) -> str:
        """Derive GPT-Live endpoints independently of Realtime/Voice Live."""
        validate_prefix(self.base_url)
        url = self._api_prefix() + "/live/sessions"
        return url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) if websocket else url

    def _api_prefix(self) -> str:
        prefix = self.base_url.rstrip("/")
        if self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1 and not prefix.endswith("/v1"):
            prefix += "/v1"
        return prefix

    def set_http_logger(self, http_logger: "HTTPLogger | None") -> None:
        """
        Set the HTTP logger for debugging/auditing.

        Args:
            http_logger: The HTTP logger to use, or None to disable logging.
        """
        self._http.set_logger(http_logger)

    def set_session_id(self, session_id: str | None) -> None:
        """
        Set the calling task's conversation ID for logging and the optional header.

        Args:
            session_id: Explicit conversation ID; None or empty clears it. With a
                session header configured, use 1-1024 visible ASCII characters.
        """
        if self.session_id_header and session_id:
            validate_session_header_value(session_id)
        self._http.set_session_id(session_id)

    @property
    def session_id_header(self) -> str:
        return self._session_id_header

    @session_id_header.setter
    def session_id_header(self, value: str) -> None:
        self._session_id_header = validate_session_id_header(value)

    @contextmanager
    def session_context(self, session_id: str) -> "Iterator[None]":
        """Scope an explicit ID to this task, restoring its previous value on exit."""
        if self.session_id_header and session_id:
            validate_session_header_value(session_id)
        with self._http.session_context(session_id):
            yield

    def conversation_cache_snapshot(self) -> tuple[ConversationCacheSnapshot, ...]:
        """Inspect bounded Responses/Codex telemetry across explicit conversations.

        Snapshots contain only route/identity metadata and reported usage, never
        prompts or credentials. They describe observed cache use, not server
        cache contents or a guarantee of future hits. Unscoped calls are omitted.
        """
        return self._conversation_cache.snapshot()

    def _record_conversation_cache(self, key: ConversationCacheKey, usage: Usage) -> None:
        from ..observation import observe

        snapshot = self._conversation_cache.record(key, usage)
        if snapshot is not None:
            observe("conversation_cache_usage", snapshot=snapshot)

    def _session_headers(self) -> dict[str, str]:
        if not self.session_id_header or not self._http.session_id:
            return {}
        return {self.session_id_header: validate_session_header_value(self._http.session_id)}

    async def get_model_list(self) -> list[str]:
        """Fetch model IDs explicitly, without changing the model or verification cache.

        OpenAI-compatible, OpenRouter, and LiteLLM providers use GET on the
        configured API prefix plus /models. IDs retain upstream order and spelling;
        duplicates are removed. An empty catalog is valid, not a fallback on error.

        Raises:
            NotImplementedError: Discovery is not implemented for this provider.
            ModelListError: Credentials, configuration, transport, or catalog data
                are invalid. Upstream bodies and credentials are never included.

        Catalog membership does not guarantee generation access or capabilities.
        Each call makes a fresh bounded request, with no redirects or raw logging.
        """
        if self.provider_type not in {
            ProviderType.OPENAI_COMPATIBLE,
            ProviderType.OPENROUTER,
            ProviderType.LITELLM,
            ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
        }:
            raise NotImplementedError(
                "Model discovery is not implemented for this provider; enter a model ID manually."
            )
        try:
            # base_url is mutable: validate the current prefix before sending a key.
            prefix = self.base_url
            parsed = urlsplit(prefix)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(char.isspace() or unicodedata.category(char).startswith("C") for char in prefix)
                or parsed.path.rstrip("/").endswith(("/chat/completions", "/responses", "/messages", "/completions"))
            ):
                raise ValueError
            if not self.api_key.strip() and self.bearer_token_provider is None:
                raise ValueError
            headers = (
                await self.auth_headers(prefix)
                if self.bearer_token_provider
                else {"Authorization": f"Bearer {self.api_key}"}
            )
            if self.provider_type == ProviderType.OPENROUTER:
                headers["HTTP-Referer"] = "https://github.com/nagents"
            catalog_identity = self._context_metadata_identity()
            # A short-lived secure transport also isolates catalogs from generation
            # cookies, loggers, and the ordinary HTTPClient's redirect behavior.
            client = GatewayHTTPClient(timeout=self._model_list_timeout)
            async with client:
                response = await client.get_json(f"{self._api_prefix()}/models", headers)
        except Exception:
            raise ModelListError(
                "Model discovery failed; check provider configuration, credentials, and availability."
            ) from None
        if "error" in response or not isinstance(response.get("data"), list):
            raise ModelListError("Provider returned an invalid model catalog.")
        models: list[str] = []
        seen: set[str] = set()
        for item in list_data(response["data"]):
            model_id = item.get("id") if isinstance(item, dict) else None
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or any(unicodedata.category(char).startswith("C") for char in model_id)
            ):
                raise ModelListError("Provider returned an invalid model ID.")
            if model_id not in seen:
                models.append(model_id)
                seen.add(model_id)
        if catalog_identity == self._context_metadata_identity():
            self._context_metadata_candidate = ContextMetadata()
            self._context_metadata.select(catalog_identity)
            self._context_metadata.update(response["data"])
        return models

    async def verify_model(self, force: bool = False) -> bool:
        """Verify that the specified model exists in model list endpoint.

        Args:
            force: If True, re-verify even if already cached.

        Returns:
            True if the model is found, False otherwise.

        Note:
            Verification is not implemented here for Anthropic or Azure;
            those providers continue to return True without a catalog request.
        """
        # Return cached result if available and not forcing re-verification
        if not force and self._model_verified is not None:
            return self._model_verified

        # Preserve local verification for Anthropic and Azure.
        if self.provider_type == ProviderType.ANTHROPIC:
            logger.info(f"Skipping model verification for Anthropic (model: {self.model})")
            self._model_verified = True
            return True

        if self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
            logger.info(f"Skipping model verification for Azure OpenAI (deployment: {self.model})")
            self._model_verified = True
            return True
        if self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE:
            logger.info(f"Skipping model verification for Azure OpenAI (deployment: {self.model})")
            self._model_verified = True
            return True
        try:
            if self.provider_type in (ProviderType.OPENAI_COMPATIBLE, ProviderType.OPENROUTER, ProviderType.LITELLM):
                models = await self.get_model_list()

                # Check for exact match first
                if self.model in models:
                    logger.info(f"Model '{self.model}' verified successfully")
                    self._model_verified = True
                    return True

                # Check for match with "models/" prefix (Gemini via OpenAI compat)
                prefixed_model = f"models/{self.model}"
                if prefixed_model in models:
                    logger.info(f"Model '{self.model}' verified successfully (with models/ prefix)")
                    self._model_verified = True
                    return True

                logger.warning("Selected model not found in provider catalog")
                self._model_verified = False
                return False
            else:  # GEMINI_NATIVE
                url = f"{self.base_url}/models?key={self.api_key}"
                response = await self._http.get_json(url)
                models = [m["name"] for m in response.get("models", [])]

                # Check for exact match first
                if self.model in models:
                    logger.info(f"Model '{self.model}' verified successfully")
                    self._model_verified = True
                    return True

                # Check for match with "models/" prefix
                prefixed_model = f"models/{self.model}"
                if prefixed_model in models:
                    logger.info(f"Model '{self.model}' verified successfully (with models/ prefix)")
                    self._model_verified = True
                    return True

                # Check for match without "models/" prefix (user might provide full name)
                if self.model.startswith("models/"):
                    stripped_model = self.model[7:]  # Remove "models/" prefix
                    if f"models/{stripped_model}" in models:
                        logger.info(f"Model '{self.model}' verified successfully")
                        self._model_verified = True
                        return True

                logger.warning(f"Model '{self.model}' not found in provider. Available models: {models[:10]}...")
                self._model_verified = False
                return False
        except HTTPError as e:
            logger.error("Model verification failed (HTTP %s)", e.status)
            self._model_verified = False
            return False
        except Exception:
            logger.error("Model verification failed")
            self._model_verified = False
            return False

    @property
    def is_model_verified(self) -> bool | None:
        """Check if the model has been verified.

        Returns:
            True if verified successfully, False if verification failed,
            None if verification has not been performed yet.
        """
        return self._model_verified

    @property
    def supported_media_formats(self) -> MediaCapabilities:
        """Get the media format capabilities for this provider+model.

        Returns model-specific overrides when available, otherwise
        falls back to provider-level defaults.

        Returns:
            MediaCapabilities describing supported audio, image, and document formats.
        """
        if self.api == "responses":
            # The Responses adapter supports user images, not audio or files.
            return get_media_capabilities("openai_compatible", "")
        if self.api == "messages":
            return get_media_capabilities("anthropic", self.model)
        provider = "openai_compatible" if self.provider_type == ProviderType.LITELLM else self.provider_type.value
        return get_media_capabilities(provider, self.model)

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        """
        Generate a response from the model.

        Args:
            messages: Conversation history
            tools: Optional list of tools the model can call
            config: Optional generation configuration
            stream: Whether to stream the response
            verify_model: If True, verify the model exists before generating

        Yields:
            Events as they occur (text chunks, tool calls, etc.)
        """
        started = monotonic()
        # Verify model if requested
        if verify_model:
            is_valid = await self.verify_model()
            if not is_valid:
                yield ErrorEvent(
                    message=f"Model '{self.model}' not found in provider",
                    code="MODEL_NOT_FOUND",
                    recoverable=False,
                )
                return

        try:
            async for event in self._with_retry(messages, tools, config, stream):
                yield event
        except HTTPError as e:
            logger.error("Generation failed (HTTP %s)", e.status)
            yield ErrorEvent(
                message=f"Provider request failed (HTTP {e.status}); check the API route, model access, credentials, or gateway availability.",
                code=str(e.status),
                recoverable=e.is_retryable(),
                extra=failure_extra(e, started),
            )
        except ProtocolError as e:
            yield ErrorEvent(
                message=str(e), code="PROVIDER_PROTOCOL_ERROR", recoverable=False, extra=failure_extra(e, started)
            )
        except Exception as e:
            logger.error("Provider request failed or returned invalid data")
            yield ErrorEvent(
                message="Provider request failed, timed out, or returned invalid data; check the API route and request settings.",
                code="PROVIDER_REQUEST_FAILED",
                recoverable=False,
                extra=failure_extra(e, started),
            )

    async def _dispatch(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Dispatch to the appropriate provider-specific generator."""
        if config:
            try:
                validate_reasoning_choice(config.reasoning_effort, self.cached_reasoning_efforts())
            except ValueError as error:
                raise ProtocolError(str(error)) from None
        from .factory import create_backend

        backend = create_backend(self)
        async for event in backend.generate(messages, tools, config, stream):
            yield event

    async def _with_retry(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Wrap dispatch with retry logic for rate limit and server errors.

        On retryable errors (429, 5xx):
        - Yields a RateLimitEvent so callers have visibility
        - Logs a WARNING with retry details
        - Sleeps for the calculated delay
        - Retries the request

        On retry exhaustion:
        - Logs an ERROR
        - Re-raises the HTTPError (caught by generate()'s outer handler)
        """
        max_retries = self.retry_config.max_retries

        for attempt in range(max_retries + 1):
            emitted = False
            pending: dict[tuple[str, int], ToolCallProgressEvent] = {}
            try:
                async for event in self._dispatch(messages, tools, config, stream):
                    if isinstance(event, ToolCallProgressEvent):
                        key = (event.generation_id, event.index)
                        if event.status == "streaming":
                            pending[key] = event
                        else:
                            pending.pop(key, None)
                    else:
                        emitted = True
                    yield event
                return  # Success — exit retry loop
            except Exception as e:
                # A preview never authorizes execution. Retire it before an
                # error or retry so the client cannot leave a stale skeleton.
                for snapshot in pending.values():
                    yield replace(snapshot, status="abandoned")
                if not isinstance(e, HTTPError):
                    raise
                is_last_attempt = attempt >= max_retries
                if emitted or not e.is_retryable() or is_last_attempt:
                    if attempt > 0:
                        logger.error("HTTP %s failed after %s retries", e.status, attempt)
                    raise  # Re-raise to generate()'s outer except

                delay = self._calculate_delay(e, attempt)
                error_label = "RateLimitReached" if e.is_rate_limited() else "ServerError"

                logger.warning(
                    f"Retrying HTTP {e.status} in {delay}s (attempt {attempt + 1}/{max_retries}): {error_label}"
                )

                yield RateLimitEvent(
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    retry_after=delay,
                    status_code=e.status,
                    message=f"Retrying HTTP {e.status} in {delay}s "
                    f"(attempt {attempt + 1}/{max_retries}): {error_label}",
                    rate_limit_info={},
                )

                await asyncio.sleep(delay)

    def _calculate_delay(self, error: HTTPError, attempt: int) -> float:
        """Calculate retry delay, preferring Retry-After header when available.

        Args:
            error: The HTTP error with response headers
            attempt: Zero-based attempt index

        Returns:
            Delay in seconds before next retry
        """
        if self.retry_config.respect_retry_after:
            retry_after = error.get_retry_after()
            if retry_after is not None and retry_after >= 0:
                return min(retry_after, self.retry_config.max_delay)

        # Exponential backoff: 5s, 10s, 20s, ...
        delay: float = min(
            self.retry_config.base_delay * (2**attempt),
            self.retry_config.max_delay,
        )
        return delay

    async def close(self) -> None:
        """Close the provider and release resources."""
        await self._http.close()

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
