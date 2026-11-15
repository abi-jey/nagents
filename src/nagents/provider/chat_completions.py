"""ChatCompletionsBackend wire implementation; shared lifecycle belongs to Provider."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..adapters import openai as openai_adapter
from ..adapters._validation import ProtocolError
from ..adapters._validation import list_data
from ..adapters._validation import load_object
from ..adapters._validation import object_data
from ..adapters._validation import string_data
from ..adapters._validation import token_usage
from ..events import ErrorEvent
from ..events import Event
from ..events import FinishReason
from ..events import ReasoningChunkEvent
from ..events import TextChunkEvent
from ..events import TextDoneEvent
from ..events import ToolCallEvent
from ..events import Usage
from ..types import GenerationConfig
from ..types import Message
from ..types import ToolDefinition
from .backend import ProviderBackend
from .base import ProviderType

logger = logging.getLogger(__name__)


class ChatCompletionsBackend(ProviderBackend):
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        if self._provider.provider_type == ProviderType.OPENROUTER:
            generator = self._generate_openrouter
        elif self._provider.provider_type in {
            ProviderType.AZURE_OPENAI_COMPATIBLE,
            ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
        }:
            generator = self._generate_azure_openai
        else:
            generator = self._generate_openai
        async for event in generator(messages, tools, config, stream):
            yield event

    async def _generate_openai(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using OpenAI-compatible API."""
        url = f"{self._provider.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._provider.api_key}",
            "Content-Type": "application/json",
        }
        if self._provider.bearer_token_provider is not None:
            headers.update(await self._provider.auth_headers(url))
        headers.update(self._provider._session_headers())

        body: dict[str, Any] = {
            "model": self._provider.model,
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

            if config.reasoning_effort:
                body["reasoning_effort"] = config.reasoning_effort

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
        url = f"{self._provider.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._provider.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/nagents",
        }
        headers.update(self._provider._session_headers())

        body: dict[str, Any] = {
            "model": self._provider.model,
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
                body["reasoning"] = dict(config.reasoning)
            if config.reasoning_effort:
                reasoning = body.setdefault("reasoning", {})
                reasoning.pop("max_tokens", None)
                reasoning.pop("enabled", None)
                reasoning["effort"] = config.reasoning_effort

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
        if self._provider.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE:
            url = f"{self._provider.base_url}/deployments/{self._provider.model}/chat/completions?api-version={self._provider.api_version}"
            # Azure uses api-key header instead of Authorization Bearer
            headers = {
                "api-key": self._provider.api_key,
                "Content-Type": "application/json",
            }
        elif self._provider.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
            # V1 Azure OpenAI format - standard OpenAI-compatible endpoint
            # URL: https://{resource}.openai.azure.com/openai/v1/chat/completions
            # Uses Bearer token auth and model in body (like standard OpenAI)
            prefix = self._provider._api_prefix()
            url = f"{prefix}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self._provider.api_key}",
                "Content-Type": "application/json",
            }
        else:
            raise ValueError(f"Unexpected provider type for Azure generation: {self._provider.provider_type}")

        if self._provider.bearer_token_provider is not None:
            headers.update(await self._provider.auth_headers(url))
        headers.update(self._provider._session_headers())

        body: dict[str, Any] = {
            "messages": openai_adapter.format_messages(messages),
            "stream": stream,
        }

        # V1 API requires model in body (like standard OpenAI)
        if self._provider.provider_type == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
            body["model"] = self._provider.model

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

            if config.reasoning_effort:
                body["reasoning_effort"] = config.reasoning_effort

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
        tool_accumulator = openai_adapter.StreamingToolCallAccumulator()
        latest_usage = Usage()
        finish_reason = FinishReason.UNKNOWN
        extra: dict[str, Any] = {}
        completed = False
        reasoning_details: dict[int, dict[str, Any]] = {}
        terminal_reason = ""

        async for data in self._provider._http.post_stream(url, body, headers):
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
            index = choice.get("index", 0)
            if type(index) is not int or index != 0:
                raise ProtocolError("Provider returned an unexpected choice index; no tools were released.")
            delta = object_data(choice.get("delta") or {})
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
                yield ReasoningChunkEvent(chunk=string_data(reasoning), usage=latest_usage, displayable=True)
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
                for preview in tool_accumulator.previews():
                    yield preview

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
                call = ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )
                yield tool_accumulator.ready(call)
                yield call

    async def _non_stream_openai(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming OpenAI response."""
        response = await self._provider._http.post_json(url, body, headers)
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
        index = choice.get("index", 0)
        if type(index) is not int or index != 0:
            raise ProtocolError("Provider returned an unexpected choice index; no tools were released.")

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
            yield ReasoningChunkEvent(chunk=reasoning, usage=latest_usage, extra=extra, displayable=True)

        # Handle tool calls
        tool_calls = openai_adapter.parse_tool_calls(choice)
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
