"""
Unified LLM provider for all supported backends.

Supports:
- OpenAI API (and compatible: Gemini via compat, OpenRouter, Ollama, etc.)
- Gemini Native REST API
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.parse import urlsplit

from ..adapters import openai as openai_adapter
from ..adapters._validation import ProtocolError
from ..adapters._validation import list_data
from ..adapters._validation import load_object
from ..adapters._validation import object_data
from ..adapters._validation import string_data
from ..adapters._validation import token_usage
from ..compaction import get_model_context_limit
from ..compactor import Messages
from ..compactor import Tokens
from ..events import ErrorEvent
from ..events import Event
from ..events import FinishReason
from ..events import RateLimitEvent
from ..events import ReasoningChunkEvent
from ..events import TextChunkEvent
from ..events import TextDoneEvent
from ..events import ToolCallEvent
from ..events import Usage
from ..http import HTTPClient
from ..http import HTTPError
from ..media import MediaCapabilities
from ..media import get_media_capabilities
from ..types import GenerationConfig
from ..types import Message
from ..types import RetryConfig
from ..types import ToolCall
from ..types import ToolDefinition
from .gateway import GatewayHTTPClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from ..http import HTTPLogger
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
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 120.0,
        api_version: str | None = None,
        retry_config: RetryConfig | None = None,
        realtime_config: "RealtimeConfig | None" = None,
        api: str = "auto",
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
            api: HTTP contract: auto, chat_completions, responses, messages,
                or completions. Auto preserves native providers and selects
                chat_completions for LiteLLM and OpenRouter. base_url is an API
                prefix, not a full generation endpoint.
        """
        self.provider_type = provider_type
        self.api_key = api_key
        self.model = model
        self.api_version = api_version
        self.retry_config = retry_config or RetryConfig()
        self.realtime_config = realtime_config
        if api not in {"auto", "chat_completions", "responses", "messages", "completions"}:
            raise ValueError("api must be auto, chat_completions, responses, messages, or completions")
        if provider_type == ProviderType.LITELLM and not base_url:
            raise ValueError("LiteLLM requires an explicit base_url pointing to your gateway")
        default_api = "messages" if provider_type == ProviderType.ANTHROPIC else "chat_completions"
        if provider_type == ProviderType.GEMINI_NATIVE:
            default_api = "auto"
        self.api = default_api if api == "auto" else api
        if (
            provider_type not in {ProviderType.LITELLM, ProviderType.OPENAI_COMPATIBLE, ProviderType.OPENROUTER}
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
            if provider_type in {ProviderType.LITELLM, ProviderType.OPENROUTER} or api != "auto"
            else HTTPClient(timeout=timeout)
        )
        self._model_verified: bool | None = None  # None = not checked, True/False = result

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
        if not self.api_key:
            raise ValueError("API key is required for provider initialization")

    def set_http_logger(self, http_logger: "HTTPLogger | None") -> None:
        """
        Set the HTTP logger for debugging/auditing.

        Args:
            http_logger: The HTTP logger to use, or None to disable logging.
        """
        self._http.set_logger(http_logger)

    def set_session_id(self, session_id: str | None) -> None:
        """
        Set the current session ID for HTTP logging.

        Args:
            session_id: The session ID to include in log entries.
        """
        self._http.set_session_id(session_id)

    async def verify_model(self, force: bool = False) -> bool:
        """Verify that the specified model exists in model list endpoint.

        Args:
            force: If True, re-verify even if already cached.

        Returns:
            True if the model is found, False otherwise.

        Note:
            Anthropic doesn't have a models list endpoint, so verification
            is skipped for that provider and always returns True.
        """
        # Return cached result if available and not forcing re-verification
        if not force and self._model_verified is not None:
            return self._model_verified

        # Anthropic and Azure don't have a models list endpoint
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
                url = f"{self.base_url}/models"
                headers = {"Authorization": f"Bearer {self.api_key}"}

                # OpenRouter also accepts an HTTP-Referer header for identification
                if self.provider_type == ProviderType.OPENROUTER:
                    headers["HTTP-Referer"] = "https://github.com/nagents"

                response = await self._http.get_json(url, headers)
                model_count = len(response.get("data", []))
                logger.info(f"Model list response: {model_count} models available")
                models = [m["id"] for m in response.get("data", [])]

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

                logger.warning(f"Model '{self.model}' not found in provider. Available models: {models[:10]}...")
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
        if self.api == "completions":
            return MediaCapabilities()
        if self.api == "responses":
            # The Responses adapter supports user images, not audio or files.
            return get_media_capabilities("openai_compatible", "")
        if self.api == "messages":
            return get_media_capabilities("anthropic", self.model)
        provider = "openai_compatible" if self.provider_type == ProviderType.LITELLM else self.provider_type.value
        return get_media_capabilities(provider, self.model)

    def get_default_compact_on(self) -> Tokens | Messages | None:
        """Get the default compaction trigger for this model.

        Returns model-specific compaction triggers based on known context limits.
        Subclasses can override this for provider-specific defaults.

        Returns:
            Tokens or Messages trigger configuration, or None to use fallback.
        """
        context_limit = get_model_context_limit(self)
        if context_limit == 0:
            return None

        # Default: compact at 80% of context limit, reserve 10% for output
        return Tokens(
            input=int(context_limit * 0.8),
            output=int(context_limit * 0.1),
        )

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
            )
        except ProtocolError as e:
            yield ErrorEvent(message=str(e), code="PROVIDER_PROTOCOL_ERROR", recoverable=False)
        except Exception:
            logger.error("Provider request failed or returned invalid data")
            yield ErrorEvent(
                message="Provider request failed, timed out, or returned invalid data; check the API route and request settings.",
                code="PROVIDER_REQUEST_FAILED",
                recoverable=False,
            )

    async def _dispatch(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Dispatch to the appropriate provider-specific generator."""
        if self.api == "responses":
            async for event in self._generate_responses(messages, tools, config, stream):
                yield event
        elif self.api == "completions":
            async for event in self._generate_completions(messages, tools, config, stream):
                yield event
        elif self.api == "messages":
            async for event in self._generate_anthropic(messages, tools, config, stream):
                yield event
        elif self.provider_type in {ProviderType.OPENAI_COMPATIBLE, ProviderType.LITELLM}:
            async for event in self._generate_openai(messages, tools, config, stream):
                yield event
        elif self.provider_type == ProviderType.OPENROUTER:
            async for event in self._generate_openrouter(messages, tools, config, stream):
                yield event
        elif (
            self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE
            or self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1
        ):
            async for event in self._generate_azure_openai(messages, tools, config, stream):
                yield event
        elif self.provider_type == ProviderType.ANTHROPIC:
            async for event in self._generate_anthropic(messages, tools, config, stream):
                yield event
        else:
            async for event in self._generate_gemini(messages, tools, config, stream):
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
            try:
                async for event in self._dispatch(messages, tools, config, stream):
                    emitted = True
                    yield event
                return  # Success — exit retry loop
            except HTTPError as e:
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

    async def _generate_openai(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using OpenAI-compatible API."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        body: dict[str, Any] = {
            "model": self.model,
            "messages": openai_adapter.format_messages(messages),
            "stream": stream,
        }

        # Request usage data in streaming responses
        if stream:
            body["stream_options"] = {"include_usage": True}

        if tools:
            body["tools"] = openai_adapter.format_tools(tools)

        if config:
            if config.temperature is not None:
                body["temperature"] = config.temperature
            if config.max_tokens is not None:
                body["max_completion_tokens"] = config.max_tokens
            if config.top_p is not None:
                body["top_p"] = config.top_p
            if config.stop:
                body["stop"] = config.stop

        if stream:
            async for event in self._stream_openai(url, body, headers):
                yield event
        else:
            async for event in self._non_stream_openai(url, body, headers):
                yield event

    async def _generate_openrouter(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using OpenRouter API (OpenAI-compatible with reasoning support)."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/nagents",
        }

        body: dict[str, Any] = {
            "model": self.model,
            "messages": openai_adapter.format_messages(messages),
            "stream": stream,
        }

        if stream:
            body["stream_options"] = {"include_usage": True}

        if tools:
            body["tools"] = openai_adapter.format_tools(tools)

        if config:
            if config.temperature is not None:
                body["temperature"] = config.temperature
            if config.max_tokens is not None:
                body["max_completion_tokens"] = config.max_tokens
            if config.top_p is not None:
                body["top_p"] = config.top_p
            if config.stop:
                body["stop"] = config.stop
            if config.reasoning:
                body["reasoning"] = config.reasoning

        if stream:
            async for event in self._stream_openai(url, body, headers):
                yield event
        else:
            async for event in self._non_stream_openai(url, body, headers):
                yield event

    async def _generate_azure_openai(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using Azure OpenAI Service API.

        Two variants are supported:

        AZURE_OPENAI_COMPATIBLE (deployment-based):
        - URL: https://{resource}.cognitiveservices.azure.com/openai/deployments/{deployment}/chat/completions
        - Auth: api-key header
        - Requires api-version query parameter

        AZURE_OPENAI_COMPATIBLE_V1 (OpenAI-compatible):
        - URL: https://{resource}.openai.azure.com/openai/v1/chat/completions
        - Auth: Authorization Bearer token
        - Model specified in request body (like standard OpenAI)
        """
        # Azure URL format: {base_url}/deployments/{deployment}/chat/completions?api-version={version}
        # Note: base_url should include /openai suffix (e.g., https://{resource}.cognitiveservices.azure.com/openai)
        if self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE:
            url = f"{self.base_url}/deployments/{self.model}/chat/completions?api-version={self.api_version}"
            # Azure uses api-key header instead of Authorization Bearer
            headers = {
                "api-key": self.api_key,
                "Content-Type": "application/json",
            }
        elif self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
            # V1 Azure OpenAI format - standard OpenAI-compatible endpoint
            # URL: https://{resource}.openai.azure.com/openai/v1/chat/completions
            # Uses Bearer token auth and model in body (like standard OpenAI)
            prefix = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"
            url = f"{prefix}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        else:
            raise ValueError(f"Unexpected provider type for Azure generation: {self.provider_type}")

        body: dict[str, Any] = {
            "messages": openai_adapter.format_messages(messages),
            "stream": stream,
        }

        # V1 API requires model in body (like standard OpenAI)
        if self.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
            body["model"] = self.model

        # Request usage data in streaming responses
        if stream:
            body["stream_options"] = {"include_usage": True}

        if tools:
            body["tools"] = openai_adapter.format_tools(tools)

        if config:
            if config.temperature is not None:
                body["temperature"] = config.temperature
            if config.max_tokens is not None:
                body["max_tokens"] = config.max_tokens  # Azure uses max_tokens, not max_completion_tokens
            if config.top_p is not None:
                body["top_p"] = config.top_p
            if config.stop:
                body["stop"] = config.stop

        # Reuse the same streaming/non-streaming handlers as OpenAI
        if stream:
            async for event in self._stream_openai(url, body, headers):
                yield event
        else:
            async for event in self._non_stream_openai(url, body, headers):
                yield event

    async def _stream_openai(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        text_only: bool = False,
    ) -> AsyncIterator[Event]:
        """Handle streaming OpenAI response."""
        full_text = ""
        tool_accumulator = openai_adapter.StreamingToolCallAccumulator()
        latest_usage = Usage()
        finish_reason = FinishReason.UNKNOWN
        extra: dict[str, Any] = {}
        completed = False
        reasoning_details: dict[int, dict[str, Any]] = {}
        terminal_reason = ""

        async for data in self._http.post_stream(url, body, headers):
            if data == "[DONE]":
                completed = True
                break

            chunk = load_object(data)
            if chunk.get("error"):
                raise ProtocolError("Provider stream reported a generation error; no tools were released.")

            # Capture extra metadata
            if "id" in chunk:
                extra["id"] = chunk["id"]
            if "model" in chunk:
                extra["model"] = chunk["model"]
            if "created" in chunk:
                extra["created"] = chunk["created"]

            # Handle usage (OpenAI sends this in a separate final chunk with empty choices)
            usage = chunk.get("usage")
            if usage:
                latest_usage = token_usage(usage, "chat_completions")

            choices = list_data(chunk.get("choices") or [])
            if not choices:
                continue

            if len(choices) != 1:
                raise ProtocolError("Provider returned multiple choices for a single-choice request.")
            choice = object_data(choices[0])
            delta = object_data(choice.get("delta") or {})
            if text_only:
                if delta or choice.get("tool_calls") or choice.get("message"):
                    raise ProtocolError("The completions endpoint returned non-text output.")
                delta = {"content": choice.get("text", "")}
            if terminal_reason and any(
                delta.get(key)
                for key in ("content", "refusal", "tool_calls", "reasoning", "reasoning_content", "reasoning_details")
            ):
                raise ProtocolError("Provider sent generation data after the terminal choice; no tools were released.")

            # Parse finish_reason
            fr = choice.get("finish_reason")
            if fr:
                if terminal_reason and fr != terminal_reason:
                    raise ProtocolError("Provider returned inconsistent terminal finish reasons.")
                terminal_reason = string_data(fr)
                if fr == "stop":
                    finish_reason = FinishReason.STOP
                elif fr == "tool_calls":
                    finish_reason = FinishReason.TOOL_CALLS
                elif fr == "length":
                    finish_reason = FinishReason.LENGTH
                elif fr == "content_filter":
                    finish_reason = FinishReason.CONTENT_FILTER
                else:
                    raise ProtocolError(
                        "Provider stream returned an unsuccessful finish reason; no tools were released."
                    )

            # Handle reasoning content (e.g., from Kimi, DeepSeek, o1 models, Ollama)
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield ReasoningChunkEvent(chunk=string_data(reasoning), usage=latest_usage)
            for raw_detail in list_data(delta.get("reasoning_details") or []):
                detail = object_data(raw_detail)
                index = detail.get("index", 0)
                if type(index) is not int or not 0 <= index < 1024:
                    raise ProtocolError("Provider returned an invalid reasoning index.")
                previous = reasoning_details.setdefault(index, {})
                for key, value in detail.items():
                    previous[key] = (
                        previous.get(key, "") + string_data(value) if key in {"text", "data", "summary"} else value
                    )

            # Handle text content
            content = delta.get("content") or delta.get("refusal")
            if content:
                text = string_data(content)
                full_text += text
                yield TextChunkEvent(chunk=text, usage=latest_usage, extra=extra)

            # Handle tool calls
            if "tool_calls" in delta:
                tool_accumulator.add_delta(delta)

        if not completed or finish_reason == FinishReason.UNKNOWN:
            raise ProtocolError("Provider stream ended before completion; no tools were released.")
        tool_calls = tool_accumulator.get_complete_tool_calls()
        if tool_calls and finish_reason not in {FinishReason.TOOL_CALLS, FinishReason.STOP}:
            raise ProtocolError("Provider returned incomplete tool calls; no tools were released.")
        if finish_reason == FinishReason.TOOL_CALLS and not tool_calls:
            raise ProtocolError("Provider finished with tools but returned no complete calls.")
        if tool_calls and reasoning_details:
            tool_calls[0].metadata["reasoning_details"] = json.dumps(
                [detail for _, detail in sorted(reasoning_details.items())]
            )
        yield TextDoneEvent(
            text=full_text,
            usage=latest_usage,
            finish_reason=FinishReason.TOOL_CALLS if tool_calls else finish_reason,
            extra=extra,
        )
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )

    async def _non_stream_openai(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        text_only: bool = False,
    ) -> AsyncIterator[Event]:
        """Handle non-streaming OpenAI response."""
        response = await self._http.post_json(url, body, headers)
        if response.get("error"):
            raise ProtocolError("Provider reported a generation error; no tools were released.")

        choices = response.get("choices") or []
        if not choices:
            yield ErrorEvent(message="No choices in response")
            return

        choice = choices[0]
        message = choice.get("message") or {}
        if len(choices) != 1:
            raise ProtocolError("Provider returned multiple choices for a single-choice request.")
        if text_only:
            if message or choice.get("tool_calls"):
                raise ProtocolError("The completions endpoint returned non-text output.")
            message = {"content": string_data(choice.get("text"))}

        # Parse finish_reason
        finish_reason = FinishReason.UNKNOWN
        fr = choice.get("finish_reason")
        if fr == "stop":
            finish_reason = FinishReason.STOP
        elif fr == "tool_calls":
            finish_reason = FinishReason.TOOL_CALLS
        elif fr == "length":
            finish_reason = FinishReason.LENGTH
        elif fr == "content_filter":
            finish_reason = FinishReason.CONTENT_FILTER
        else:
            raise ProtocolError("Provider returned an unsuccessful finish reason; no tools were released.")

        # Parse extra metadata
        extra: dict[str, Any] = {}
        if "id" in response:
            extra["id"] = response["id"]
        if "model" in response:
            extra["model"] = response["model"]
        if "created" in response:
            extra["created"] = response["created"]

        # Handle usage with detailed breakdown
        latest_usage = token_usage(response.get("usage"), "chat_completions")

        # Handle reasoning content (from chain-of-thought models, Ollama)
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            yield ReasoningChunkEvent(chunk=reasoning, usage=latest_usage, extra=extra)

        # Handle tool calls
        tool_calls = [] if text_only else openai_adapter.parse_tool_calls(choice)
        if tool_calls and finish_reason not in {FinishReason.STOP, FinishReason.TOOL_CALLS}:
            raise ProtocolError("Provider returned incomplete tool calls; no tools were released.")
        if finish_reason == FinishReason.TOOL_CALLS and not tool_calls:
            raise ProtocolError("Provider finished with tools but returned no complete calls.")
        if tool_calls and message.get("reasoning_details"):
            tool_calls[0].metadata["reasoning_details"] = json.dumps(list_data(message["reasoning_details"]))
        content = string_data(message.get("content") or message.get("refusal") or "")
        yield TextDoneEvent(
            text=content,
            usage=latest_usage,
            finish_reason=FinishReason.TOOL_CALLS if tool_calls else finish_reason,
            extra=extra,
        )
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )

    async def _generate_gemini(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using native Gemini REST API."""
        # Import gemini adapter (lazy import to allow optional usage)
        from ..adapters import gemini as gemini_adapter

        endpoint = "streamGenerateContent" if stream else "generateContent"
        url = f"{self.base_url}/models/{self.model}:{endpoint}?key={self.api_key}"

        if stream:
            url += "&alt=sse"

        headers = {"Content-Type": "application/json"}

        body = gemini_adapter.format_request(messages, tools, config)

        if stream:
            async for event in self._stream_gemini(url, body, headers):
                yield event
        else:
            async for event in self._non_stream_gemini(url, body, headers):
                yield event

    async def _stream_gemini(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle streaming Gemini response."""
        from ..adapters import gemini as gemini_adapter

        full_text = ""
        all_tool_calls: list[ToolCall] = []
        latest_usage = Usage()
        finish_reason = FinishReason.UNKNOWN
        extra: dict[str, Any] = {}

        async for data in self._http.post_stream(url, body, headers):
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            text, tool_calls, usage = gemini_adapter.parse_response(chunk)

            if usage:
                latest_usage = Usage(
                    prompt_tokens=usage.get("promptTokenCount", 0),
                    completion_tokens=usage.get("candidatesTokenCount", 0),
                    total_tokens=usage.get("totalTokenCount", 0),
                )

            if text:
                full_text += text
                yield TextChunkEvent(chunk=text, usage=latest_usage, extra=extra)

            if tool_calls:
                all_tool_calls.extend(tool_calls)

        # Emit tool calls
        if all_tool_calls:
            for tc in all_tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )
        elif full_text:
            yield TextDoneEvent(text=full_text, usage=latest_usage, finish_reason=finish_reason, extra=extra)

    async def _non_stream_gemini(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming Gemini response."""
        from ..adapters import gemini as gemini_adapter

        response = await self._http.post_json(url, body, headers)
        text, tool_calls, usage = gemini_adapter.parse_response(response)

        # Parse usage
        latest_usage = Usage()
        if usage:
            latest_usage = Usage(
                prompt_tokens=usage.get("promptTokenCount", 0),
                completion_tokens=usage.get("candidatesTokenCount", 0),
                total_tokens=usage.get("totalTokenCount", 0),
            )

        # Parse finish_reason from Gemini response
        finish_reason = FinishReason.UNKNOWN
        candidates = response.get("candidates", [])
        if candidates:
            finish_reason_str = candidates[0].get("finishReason", "")
            if finish_reason_str == "STOP":
                finish_reason = FinishReason.STOP
            elif finish_reason_str == "MAX_TOKENS":
                finish_reason = FinishReason.LENGTH
            elif finish_reason_str == "SAFETY":
                finish_reason = FinishReason.CONTENT_FILTER

        extra: dict[str, Any] = {}
        if "model" in response:
            extra["model"] = response["model"]

        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )
        elif text:
            yield TextDoneEvent(text=text, usage=latest_usage, finish_reason=finish_reason, extra=extra)

    async def _generate_anthropic(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using Anthropic Claude API."""
        from ..adapters import anthropic as anthropic_adapter

        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if self.provider_type != ProviderType.ANTHROPIC:
            prefix = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"
            url = f"{prefix}/messages"
            headers.pop("x-api-key")
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Format messages (extracts system prompt separately)
        system_prompt, formatted_messages = anthropic_adapter.format_messages(messages)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "max_tokens": config.max_tokens if config and config.max_tokens else 4096,
            "stream": stream,
        }

        if system_prompt:
            body["system"] = system_prompt

        if tools:
            body["tools"] = anthropic_adapter.format_tools(tools)

        if config:
            if config.temperature is not None:
                body["temperature"] = config.temperature
            if config.top_p is not None:
                body["top_p"] = config.top_p
            if config.stop:
                body["stop_sequences"] = config.stop

        if stream:
            body["stream"] = True
            async for event in self._stream_anthropic(url, body, headers):
                yield event
        else:
            async for event in self._non_stream_anthropic(url, body, headers):
                yield event

    async def _stream_anthropic(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle streaming Anthropic response."""
        from ..adapters import anthropic as anthropic_adapter

        full_text = ""
        tool_accumulator = anthropic_adapter.StreamingToolCallAccumulator()
        current_block_index = 0
        latest_usage = Usage()
        usage_data: dict[str, Any] = {}
        finish_reason = FinishReason.UNKNOWN
        completed = False
        open_blocks: set[int] = set()
        seen_blocks: set[int] = set()

        async for data in self._http.post_stream(url, body, headers):
            chunk = load_object(data)

            event_type = chunk.get("type")
            if event_type == "error" or chunk.get("error"):
                raise ProtocolError("Messages stream reported a generation error; no tools were released.")
            if event_type == "message_stop":
                completed = True
                break

            if event_type == "content_block_start":
                # New content block starting
                index = chunk.get("index", 0)
                if type(index) is not int or not 0 <= index < 1024 or index in seen_blocks:
                    raise ProtocolError("Messages returned a duplicate or invalid content block index.")
                current_block_index = index
                open_blocks.add(index)
                seen_blocks.add(index)
                content_block = object_data(chunk.get("content_block", {}))
                block_type = content_block.get("type")

                if block_type == "tool_use":
                    # Start of a tool call
                    tool_accumulator.start_tool_call(
                        index=current_block_index,
                        tool_id=string_data(content_block.get("id", "")),
                        name=string_data(content_block.get("name", "")),
                        initial_input=content_block.get("input"),
                    )
                elif block_type == "text" and content_block.get("text"):
                    text = string_data(content_block["text"])
                    full_text += text
                    yield TextChunkEvent(chunk=text, usage=latest_usage)

            elif event_type == "content_block_stop":
                index = chunk.get("index")
                if type(index) is not int or index not in open_blocks:
                    raise ProtocolError("Messages stopped an unknown content block.")
                open_blocks.remove(index)

            elif event_type == "content_block_delta":
                index = chunk.get("index", 0)
                if type(index) is not int or index not in open_blocks:
                    raise ProtocolError("Messages returned a delta outside an open content block.")
                current_block_index = index
                delta = object_data(chunk.get("delta", {}))
                delta_type = delta.get("type")

                if delta_type == "text_delta":
                    # Text content
                    text = string_data(delta.get("text", ""))
                    if text:
                        full_text += text
                        yield TextChunkEvent(chunk=text, usage=latest_usage)

                elif delta_type == "input_json_delta":
                    # Tool call argument fragment
                    partial_json = string_data(delta.get("partial_json", ""))
                    tool_accumulator.add_input_delta(current_block_index, partial_json)

            elif event_type == "message_delta":
                # Message-level update (contains usage info at end)
                usage = chunk.get("usage")
                if usage:
                    usage_data.update(object_data(usage))
                    latest_usage = token_usage(usage_data, "messages")
                stop_reason = object_data(chunk.get("delta") or {}).get("stop_reason")
                if stop_reason:
                    finish_reason = {
                        "end_turn": FinishReason.STOP,
                        "stop_sequence": FinishReason.STOP,
                        "tool_use": FinishReason.TOOL_CALLS,
                        "max_tokens": FinishReason.LENGTH,
                        "refusal": FinishReason.CONTENT_FILTER,
                    }.get(string_data(stop_reason), FinishReason.UNKNOWN)

            elif event_type == "message_start":
                # Initial message info (may contain usage)
                message = object_data(chunk.get("message", {}))
                usage = message.get("usage")
                if usage:
                    usage_data.update(object_data(usage))
                    latest_usage = token_usage(usage_data, "messages")

        if not completed or open_blocks or finish_reason == FinishReason.UNKNOWN:
            raise ProtocolError("Messages stream ended before completion; no tools were released.")

        # Emit accumulated tool calls
        tool_calls = tool_accumulator.get_complete_tool_calls()
        if tool_calls and finish_reason != FinishReason.TOOL_CALLS:
            raise ProtocolError("Messages returned incomplete tool calls; no tools were released.")
        if finish_reason == FinishReason.TOOL_CALLS and not tool_calls:
            raise ProtocolError("Messages finished with tools but returned no complete calls.")
        yield TextDoneEvent(text=full_text, usage=latest_usage, finish_reason=finish_reason)
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    metadata=tc.metadata,
                )

    async def _non_stream_anthropic(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming Anthropic response."""
        from ..adapters import anthropic as anthropic_adapter

        response = await self._http.post_json(url, body, headers)
        if response.get("error") or response.get("type") == "error":
            raise ProtocolError("Messages reported a generation error; no tools were released.")
        text, tool_calls, usage = anthropic_adapter.parse_response(response)

        # Parse finish_reason
        finish_reason = FinishReason.UNKNOWN
        stop_reason = response.get("stop_reason")
        if stop_reason in {"end_turn", "stop_sequence"}:
            finish_reason = FinishReason.STOP
        elif stop_reason == "tool_use":
            finish_reason = FinishReason.TOOL_CALLS
        elif stop_reason == "max_tokens":
            finish_reason = FinishReason.LENGTH
        elif stop_reason == "refusal":
            finish_reason = FinishReason.CONTENT_FILTER
        else:
            raise ProtocolError("Messages returned an unsuccessful stop reason; no tools were released.")
        if tool_calls and finish_reason != FinishReason.TOOL_CALLS:
            raise ProtocolError("Messages returned incomplete tool calls; no tools were released.")
        if finish_reason == FinishReason.TOOL_CALLS and not tool_calls:
            raise ProtocolError("Messages finished with tools but returned no complete calls.")

        # Parse extra metadata
        extra: dict[str, Any] = {}
        if "id" in response:
            extra["id"] = response["id"]
        if "model" in response:
            extra["model"] = response["model"]

        # Parse usage
        latest_usage = token_usage(usage, "messages")

        yield TextDoneEvent(text=text, usage=latest_usage, finish_reason=finish_reason, extra=extra)
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )

    async def _generate_responses(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        from ..adapters.responses import ResponseAccumulator
        from ..adapters.responses import format_request

        body = format_request(self.model, messages, tools, config, stream)
        url = f"{self.base_url}/responses"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.provider_type == ProviderType.OPENROUTER:
            headers["HTTP-Referer"] = "https://github.com/nagents"
        accumulator = ResponseAccumulator()
        if stream:
            async with aclosing(
                cast("AsyncGenerator[str, None]", self._http.post_stream(url, body, headers))
            ) as chunks:
                async for data in chunks:
                    if data == "[DONE]":
                        break
                    events = accumulator.add(load_object(data))
                    if accumulator.completed:
                        break
                    for event in events:
                        yield event
            if not accumulator.completed:
                raise ProtocolError("Responses stream ended before completion; no tools were released.")
        else:
            events = accumulator.finish(await self._http.post_json(url, body, headers))
        for event in events:
            yield event

    async def _generate_completions(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        from ..types import TextContent

        if tools or any(message.tool_calls or message.tool_call_id for message in messages):
            raise ProtocolError(
                "The legacy completions API is text-only and does not support tools; use chat_completions, responses, or messages."
            )
        if len(messages) != 1 or messages[0].role != "user":
            raise ProtocolError(
                "The legacy completions API requires exactly one user prompt; system instructions and conversation history cannot be flattened safely."
            )
        content = messages[0].content
        if isinstance(content, str):
            prompt = content
        elif isinstance(content, list) and all(isinstance(part, TextContent) for part in content):
            prompt = "".join(part.text for part in content if isinstance(part, TextContent))
        else:
            raise ProtocolError("The legacy completions API accepts text only; multimodal input is unsupported.")
        body: dict[str, object] = {"model": self.model, "prompt": prompt, "stream": stream}
        # LiteLLM 1.100.0 fails to serialize usage-only legacy stream chunks
        # (MockValSer). Omit the optional opt-in, but still parse supplied usage.
        if stream and self.provider_type != ProviderType.LITELLM:
            body["stream_options"] = {"include_usage": True}
        if config:
            if config.reasoning or config.thinking_config:
                raise ProtocolError("The legacy completions API does not support reasoning configuration.")
            for name in ("temperature", "max_tokens", "top_p", "stop"):
                value = getattr(config, name)
                if value is not None:
                    body[name] = value
        # Text completions share the chat stream envelope but never chat roles.
        url = f"{self.base_url}/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.provider_type == ProviderType.OPENROUTER:
            headers["HTTP-Referer"] = "https://github.com/nagents"
        if stream:
            async for event in self._stream_openai(url, body, headers, text_only=True):
                yield event
        else:
            async for event in self._non_stream_openai(url, body, headers, text_only=True):
                yield event

    async def close(self) -> None:
        """Close the provider and release resources."""
        await self._http.close()

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


class PlaceholderProvider(Provider):
    """Placeholder provider that raises errors when used.

    Used by Compactor when provider is not yet set. The main agent
    will inject its provider before calling run().
    """

    def __init__(self) -> None:
        # Don't call super().__init__ - we don't have real values
        self._is_placeholder = True
        self._http = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return "<PlaceholderProvider>"

    def _raise_placeholder_error(self) -> None:
        raise RuntimeError(
            "PlaceholderProvider has not been replaced. The main agent must inject its provider before calling run()."
        )

    @property
    def provider_type(self) -> ProviderType:  # type: ignore[override]
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    @property
    def api_key(self) -> str:  # type: ignore[override]
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    @property
    def model(self) -> str:  # type: ignore[override]
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    @property
    def base_url(self) -> str:  # type: ignore[override]
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    def set_http_logger(self, http_logger: "HTTPLogger | None") -> None:
        self._raise_placeholder_error()

    def set_session_id(self, session_id: str | None) -> None:
        self._raise_placeholder_error()

    async def verify_model(self, force: bool = False) -> bool:
        self._raise_placeholder_error()
        raise AssertionError("unreachable")

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        self._raise_placeholder_error()
        if False:  # pragma: no cover
            yield

    async def close(self) -> None:
        pass


# Placeholder instance for DEFAULT_COMPACTOR
PLACEHOLDER_PROVIDER = PlaceholderProvider()
