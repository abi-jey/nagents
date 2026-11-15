"""AnthropicBackend wire implementation; shared lifecycle belongs to Provider."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from ..adapters._reasoning import anthropic_summary_model
from ..adapters._validation import ProtocolError
from ..adapters._validation import load_object
from ..adapters._validation import object_data
from ..adapters._validation import string_data
from ..adapters._validation import token_usage
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


class AnthropicBackend(ProviderBackend):
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        async for event in self._generate_anthropic(messages, tools, config, stream):
            yield event

    async def _generate_anthropic(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Generate using Anthropic Claude API."""
        from ..adapters import anthropic as anthropic_adapter

        url = f"{self._provider.base_url}/messages"
        headers = {
            "x-api-key": self._provider.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if self._provider.provider_type != ProviderType.ANTHROPIC:
            prefix = (
                self._provider.base_url if self._provider.base_url.endswith("/v1") else f"{self._provider.base_url}/v1"
            )
            url = f"{prefix}/messages"
            headers.pop("x-api-key")
            headers["Authorization"] = f"Bearer {self._provider.api_key}"
        headers.update(self._provider._session_headers())

        # Format messages (extracts system prompt separately)
        system_prompt, formatted_messages = anthropic_adapter.format_messages(messages)

        body: dict[str, Any] = {
            "model": self._provider.model,
            "messages": formatted_messages,
            "max_tokens": config.max_tokens if config and config.max_tokens else 4096,
            "stream": stream,
        }

        if self._provider.provider_type == ProviderType.ANTHROPIC:
            # Automatic prefix caching uses Anthropic's default five-minute TTL.
            body["cache_control"] = {"type": "ephemeral"}

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
            if config.reasoning_effort:
                body["output_config"] = {"effort": config.reasoning_effort}
            if (
                config.reasoning_summary == "auto"
                and urlsplit(self._provider.base_url).hostname == "api.anthropic.com"
                and anthropic_summary_model(self._provider.model)
            ):
                body["thinking"] = {"type": "adaptive", "display": "summarized"}

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
        open_blocks: dict[int, str] = {}
        seen_blocks: set[int] = set()
        thinking_blocks: dict[int, dict[str, object]] = {}

        async for data in self._provider._http.post_stream(url, body, headers):
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
                seen_blocks.add(index)
                content_block = object_data(chunk.get("content_block", {}))
                block_type = string_data(content_block.get("type", ""))
                open_blocks[index] = block_type

                if block_type == "tool_use":
                    # Start of a tool call
                    tool_accumulator.start_tool_call(
                        index=current_block_index,
                        tool_id=string_data(content_block.get("id", "")),
                        name=string_data(content_block.get("name", "")),
                        initial_input=content_block.get("input"),
                    )
                    for preview in tool_accumulator.previews():
                        yield preview
                elif block_type == "text" and content_block.get("text"):
                    text = string_data(content_block["text"])
                    full_text += text
                    yield TextChunkEvent(chunk=text, usage=latest_usage)
                elif block_type in {"thinking", "redacted_thinking"}:
                    thinking_blocks[index] = dict(content_block)
                    if block_type == "thinking":
                        text = string_data(content_block.get("thinking", ""))
                        # Even an empty explicit thinking block reports a real
                        # phase; it supplies no fabricated summary or count.
                        yield ReasoningChunkEvent(chunk=text, usage=latest_usage, displayable=True)

            elif event_type == "content_block_stop":
                index = chunk.get("index")
                if type(index) is not int or index not in open_blocks:
                    raise ProtocolError("Messages stopped an unknown content block.")
                del open_blocks[index]

            elif event_type == "content_block_delta":
                index = chunk.get("index", 0)
                if type(index) is not int or index not in open_blocks:
                    raise ProtocolError("Messages returned a delta outside an open content block.")
                current_block_index = index
                delta = object_data(chunk.get("delta", {}))
                delta_type = string_data(delta.get("type", ""))
                expected_block = {
                    "text_delta": "text",
                    "input_json_delta": "tool_use",
                    "thinking_delta": "thinking",
                    "signature_delta": "thinking",
                    "citations_delta": "text",
                }.get(delta_type)
                if (expected_block is not None and open_blocks[index] != expected_block) or (
                    open_blocks[index] == "tool_use" and delta_type != "input_json_delta"
                ):
                    raise ProtocolError(
                        "Messages returned a delta for the wrong content block; no tools were released."
                    )

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
                    for preview in tool_accumulator.previews():
                        yield preview
                elif delta_type in {"thinking_delta", "signature_delta"}:
                    field_name = "thinking" if delta_type == "thinking_delta" else "signature"
                    value = string_data(delta.get(field_name, ""))
                    block = thinking_blocks[index]
                    block[field_name] = string_data(block.get(field_name, "")) + value
                    if field_name == "thinking" and value:
                        yield ReasoningChunkEvent(chunk=value, usage=latest_usage, displayable=True)

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
        if tool_calls and thinking_blocks:
            tool_calls[0].metadata["anthropic_thinking"] = json.dumps(
                [block for _, block in sorted(thinking_blocks.items())], allow_nan=False
            )
        yield TextDoneEvent(text=full_text, usage=latest_usage, finish_reason=finish_reason)
        if tool_calls:
            for tc in tool_calls:
                call = ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    metadata=tc.metadata,
                )
                yield tool_accumulator.ready(call)
                yield call

    async def _non_stream_anthropic(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming Anthropic response."""
        from ..adapters import anthropic as anthropic_adapter

        response = await self._provider._http.post_json(url, body, headers)
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

        for summary in anthropic_adapter.parse_summaries(response):
            yield ReasoningChunkEvent(chunk=summary, usage=latest_usage, displayable=True)
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
