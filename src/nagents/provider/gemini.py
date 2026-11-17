"""GeminiBackend wire implementation; shared lifecycle belongs to Provider."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from ..adapters._reasoning import gemini_summary_model
from ..adapters._tool_progress import ToolCallProgressTracker
from ..adapters._validation import ProtocolError
from ..adapters._validation import load_object
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
from ..types import ToolCall
from ..types import ToolDefinition
from .backend import ProviderBackend

logger = logging.getLogger(__name__)


class GeminiBackend(ProviderBackend):
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        async for event in self._generate_gemini(messages, tools, config, stream):
            yield event

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
        url = f"{self._provider.base_url}/models/{self._provider.model}:{endpoint}?key={self._provider.api_key}"

        if stream:
            url += "&alt=sse"

        headers = {"Content-Type": "application/json"}
        headers.update(self._provider._session_headers())

        body = gemini_adapter.format_request(messages, tools, config)
        if config and config.reasoning_effort:
            thinking = dict(config.thinking_config or {})
            thinking.pop("thinkingBudget", None)
            thinking["thinkingLevel"] = config.reasoning_effort
            body.setdefault("generationConfig", {})["thinkingConfig"] = thinking
        if (
            config
            and config.reasoning_summary == "auto"
            and urlsplit(self._provider.base_url).hostname == "generativelanguage.googleapis.com"
            and gemini_summary_model(self._provider.model)
            and (
                config.reasoning_effort
                or not config.thinking_config
                or config.thinking_config.get("thinkingBudget") != 0
            )
        ):
            body.setdefault("generationConfig", {}).setdefault("thinkingConfig", {}).setdefault("includeThoughts", True)

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
        progress = ToolCallProgressTracker()
        latest_usage = Usage()
        finish_reason = FinishReason.UNKNOWN
        extra: dict[str, Any] = {}

        async for data in self._provider._http.post_stream(url, body, headers):
            chunk = load_object(data)
            text, tool_calls, usage = gemini_adapter.parse_response(chunk)
            summary = gemini_adapter.parse_summary(chunk)
            reason = gemini_adapter.parse_finish_reason(chunk)
            if finish_reason != FinishReason.UNKNOWN:
                if text or summary or tool_calls or reason not in {FinishReason.UNKNOWN, finish_reason}:
                    raise ProtocolError("Gemini sent generation data after completion; no tools were released.")
            elif reason != FinishReason.UNKNOWN:
                finish_reason = reason

            if usage is not None:
                latest_usage = token_usage(usage, "gemini")

            if summary:
                yield ReasoningChunkEvent(chunk=summary, usage=latest_usage, displayable=True)
            if text:
                full_text += text
                yield TextChunkEvent(chunk=text, usage=latest_usage, extra=extra)

            if tool_calls:
                for parsed_call in tool_calls:
                    for preview in progress.preview(
                        len(all_tool_calls),
                        parsed_call.id,
                        parsed_call.name,
                        json.dumps(parsed_call.arguments, allow_nan=False),
                    ):
                        yield preview
                    all_tool_calls.append(parsed_call)

        if finish_reason == FinishReason.UNKNOWN:
            raise ProtocolError("Gemini stream ended before completion; no tools were released.")
        if all_tool_calls and finish_reason != FinishReason.STOP:
            raise ProtocolError("Gemini returned incomplete tool calls; no tools were released.")
        yield TextDoneEvent(
            text=full_text,
            usage=latest_usage,
            finish_reason=FinishReason.TOOL_CALLS if all_tool_calls else finish_reason,
            extra=extra,
        )
        if all_tool_calls:
            for index, tc in enumerate(all_tool_calls):
                call = ToolCallEvent(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    usage=latest_usage,
                    finish_reason=FinishReason.TOOL_CALLS,
                    extra=extra,
                    metadata=tc.metadata,
                )
                yield progress.ready(index, call)
                yield call

    async def _non_stream_gemini(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Handle non-streaming Gemini response."""
        from ..adapters import gemini as gemini_adapter

        response = await self._provider._http.post_json(url, body, headers)
        text, tool_calls, usage = gemini_adapter.parse_response(response)

        latest_usage = token_usage(usage, "gemini")
        finish_reason = gemini_adapter.parse_finish_reason(response)
        if finish_reason == FinishReason.UNKNOWN:
            raise ProtocolError("Gemini response ended before completion; no tools were released.")
        if tool_calls and finish_reason != FinishReason.STOP:
            raise ProtocolError("Gemini returned incomplete tool calls; no tools were released.")

        extra: dict[str, Any] = {}
        if "model" in response:
            extra["model"] = response["model"]

        summary = gemini_adapter.parse_summary(response)
        if summary:
            yield ReasoningChunkEvent(chunk=summary, usage=latest_usage, displayable=True)
        yield TextDoneEvent(
            text=text,
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
