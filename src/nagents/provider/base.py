"""
Unified LLM provider for all supported backends.

Supports:
- OpenAI API (and compatible: Gemini via compat, OpenRouter, Ollama, etc.)
- Gemini Native REST API
"""

import json
import logging
from collections.abc import AsyncIterator
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any

from ..adapters import openai as openai_adapter
from ..events import ErrorEvent
from ..events import Event
from ..events import ReasoningChunkEvent
from ..events import TextChunkEvent
from ..events import TextDoneEvent
from ..events import ToolCallEvent
from ..events import Usage
from ..http import HTTPClient
from ..http import HTTPError
from ..types import GenerationConfig
from ..types import Message
from ..types import ToolCall
from ..types import ToolDefinition

if TYPE_CHECKING:
    from ..http import HTTPLogger

logger = logging.getLogger(__name__)


class ProviderType(Enum):
    """Supported provider types."""

    OPENAI_COMPATIBLE = "openai_compatible"  # OpenAI, Gemini-via-compat, OpenRouter, etc.
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
        """
        self.provider_type = provider_type
        self.api_key = api_key
        self.model = model
        self.api_version = api_version
        self._http = HTTPClient(timeout=timeout)
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
            if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
                url = f"{self.base_url}/models"
                headers = {"Authorization": f"Bearer {self.api_key}"}
                response = await self._http.get_json(url, headers)
                logger.info(f"Model list response: {response}")
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
            logger.error(f"HTTP error during model verification: {e}")
            self._model_verified = False
            return False
        except Exception:
            logger.exception("Unexpected error during model verification")
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
            if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
                async for event in self._generate_openai(messages, tools, config, stream):
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
        except HTTPError as e:
            logger.error(f"HTTP error during generation: {e}")
            yield ErrorEvent(
                message=str(e),
                code=str(e.status),
                recoverable=e.status >= 500,
            )
        except Exception as e:
            logger.exception("Unexpected error during generation")
            yield ErrorEvent(
                message=str(e),
                recoverable=False,
            )

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
            url = f"{self.base_url}/v1/chat/completions"
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
    ) -> AsyncIterator[Event]:
        """Handle streaming OpenAI response."""
        full_text = ""
        full_reasoning = ""
        tool_accumulator = openai_adapter.StreamingToolCallAccumulator()
        latest_usage = Usage()

        async for data in self._http.post_stream(url, body, headers):
            if data == "[DONE]":
                break

            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            # Handle usage (OpenAI sends this in a separate final chunk with empty choices)
            usage = chunk.get("usage")
            if usage:
                latest_usage = Usage(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )

            choices = chunk.get("choices") or []
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}
            _ = choice.get("finish_reason")  # Reserved for future use

            # Handle reasoning content (e.g., from Kimi, DeepSeek, o1 models)
            reasoning = delta.get("reasoning_content")
            if reasoning:
                full_reasoning += reasoning
                yield ReasoningChunkEvent(chunk=reasoning, usage=latest_usage)

            # Handle text content
            content = delta.get("content")
            if content:
                full_text += content
                yield TextChunkEvent(chunk=content, usage=latest_usage)

            # Handle tool calls
            if "tool_calls" in delta:
                tool_accumulator.add_delta(delta)

        # Emit accumulated tool calls
        tool_calls = tool_accumulator.get_complete_tool_calls()
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                )
        elif full_text:
            yield TextDoneEvent(text=full_text, usage=latest_usage)

    async def _non_stream_openai(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming OpenAI response."""
        response = await self._http.post_json(url, body, headers)

        choices = response.get("choices", [])
        if not choices:
            yield ErrorEvent(message="No choices in response")
            return

        choice = choices[0]
        message = choice.get("message", {})

        # Handle usage
        latest_usage = Usage()
        usage = response.get("usage")
        if usage:
            latest_usage = Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )

        # Handle tool calls
        tool_calls = openai_adapter.parse_tool_calls(choice)
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                )
        else:
            # Handle text response
            content = message.get("content", "")
            if content:
                yield TextDoneEvent(text=content, usage=latest_usage)

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
                yield TextChunkEvent(chunk=text, usage=latest_usage)

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
                )
        elif full_text:
            yield TextDoneEvent(text=full_text, usage=latest_usage)

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

        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                )
        elif text:
            yield TextDoneEvent(text=text, usage=latest_usage)

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

        # Format messages (extracts system prompt separately)
        system_prompt, formatted_messages = anthropic_adapter.format_messages(messages)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "max_tokens": config.max_tokens if config and config.max_tokens else 4096,
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

        async for data in self._http.post_stream(url, body, headers):
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            event_type = chunk.get("type")

            if event_type == "content_block_start":
                # New content block starting
                current_block_index = chunk.get("index", 0)
                content_block = chunk.get("content_block", {})
                block_type = content_block.get("type")

                if block_type == "tool_use":
                    # Start of a tool call
                    tool_accumulator.start_tool_call(
                        index=current_block_index,
                        tool_id=content_block.get("id", ""),
                        name=content_block.get("name", ""),
                    )

            elif event_type == "content_block_delta":
                current_block_index = chunk.get("index", 0)
                delta = chunk.get("delta", {})
                delta_type = delta.get("type")

                if delta_type == "text_delta":
                    # Text content
                    text = delta.get("text", "")
                    if text:
                        full_text += text
                        yield TextChunkEvent(chunk=text, usage=latest_usage)

                elif delta_type == "input_json_delta":
                    # Tool call argument fragment
                    partial_json = delta.get("partial_json", "")
                    tool_accumulator.add_input_delta(current_block_index, partial_json)

            elif event_type == "message_delta":
                # Message-level update (contains usage info at end)
                usage = chunk.get("usage")
                if usage:
                    latest_usage = Usage(
                        prompt_tokens=usage.get("input_tokens", 0),
                        completion_tokens=usage.get("output_tokens", 0),
                        total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    )

            elif event_type == "message_start":
                # Initial message info (may contain usage)
                message = chunk.get("message", {})
                usage = message.get("usage")
                if usage:
                    # Initial input tokens
                    latest_usage = Usage(
                        prompt_tokens=usage.get("input_tokens", 0),
                        completion_tokens=0,
                        total_tokens=usage.get("input_tokens", 0),
                    )

        # Emit accumulated tool calls
        tool_calls = tool_accumulator.get_complete_tool_calls()
        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                )
        elif full_text:
            yield TextDoneEvent(text=full_text, usage=latest_usage)

    async def _non_stream_anthropic(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming Anthropic response."""
        from ..adapters import anthropic as anthropic_adapter

        response = await self._http.post_json(url, body, headers)
        text, tool_calls, usage = anthropic_adapter.parse_response(response)

        # Parse usage
        latest_usage = Usage()
        if usage:
            latest_usage = Usage(
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            )

        if tool_calls:
            for tc in tool_calls:
                yield ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                )
        elif text:
            yield TextDoneEvent(text=text, usage=latest_usage)

    async def close(self) -> None:
        """Close the provider and release resources."""
        await self._http.close()

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
