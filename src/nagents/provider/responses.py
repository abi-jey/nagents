"""ResponsesBackend wire implementation; shared lifecycle belongs to Provider."""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast
from urllib.parse import urlsplit

from ..adapters._additional_tools import AdditionalToolsUnsupported
from ..adapters._additional_tools import ToolTimeline
from ..adapters._additional_tools import rejects_additional_tools
from ..adapters._reasoning import openai_summary_model
from ..adapters._validation import ProtocolError
from ..adapters._validation import load_object
from ..events import Event
from ..events import FinishReason
from ..events import TextDoneEvent
from ..types import GenerationConfig
from ..types import Message
from ..types import ToolDefinition
from .backend import ProviderBackend
from .base import ProviderType
from .cache import ConversationCacheTracker

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


logger = logging.getLogger(__name__)


class ResponsesBackend(ProviderBackend):
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        async for event in self._generate_responses(messages, tools, config, stream):
            yield event

    async def _generate_responses(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        from ..adapters.responses import ResponseAccumulator
        from ..adapters.responses import format_request

        body = format_request(self._provider.model, messages, tools, config, stream)
        cache_key = ConversationCacheTracker.key(
            f"{self._provider._api_prefix()}/responses", self._provider.model, self._provider._http.session_id
        )
        if (
            config
            and config.reasoning_summary == "auto"
            and urlsplit(self._provider.base_url).hostname == "api.openai.com"
            and openai_summary_model(self._provider.model)
            and (not config.reasoning or config.reasoning.get("enabled") is not False)
        ):
            reasoning = dict(config.responses_reasoning)
            if config.reasoning_effort:
                reasoning["effort"] = config.reasoning_effort
            reasoning.setdefault("summary", "auto")
            body["reasoning"] = reasoning
        url = f"{self._provider._api_prefix()}/responses"
        headers = {"Authorization": f"Bearer {self._provider.api_key}", "Content-Type": "application/json"}
        if self._provider.bearer_token_provider is not None:
            headers.update(await self._provider.auth_headers(url))
        if self._provider.provider_type == ProviderType.OPENROUTER:
            headers["HTTP-Referer"] = "https://github.com/nagents"
        headers.update(self._provider._session_headers())
        route = (self._provider.base_url, self._provider.model)
        key = (*route, self._provider._http.session_id)
        # Compatible endpoints and unknown models must not be probed speculatively.
        model = self._provider.model.removesuffix("-latest")
        eligible = (
            self._provider.provider_type == ProviderType.OPENAI
            and urlsplit(self._provider.base_url).hostname == "api.openai.com"
            and model in {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"}
            and route not in self._provider._responses_tools_unsupported
        )
        fallback_body = dict(body)
        if eligible:
            if key not in self._provider._responses_tool_timelines:
                # Bound optimization memory for long-lived shared providers.
                if len(self._provider._responses_tool_timelines) >= 128:
                    self._provider._responses_tool_timelines.pop(next(iter(self._provider._responses_tool_timelines)))
                self._provider._responses_tool_timelines[key] = ToolTimeline()
            self._provider._responses_tool_timelines[key].apply(body)
        for attempt in range(2):
            accumulator = ResponseAccumulator(streaming=stream)
            received = False
            try:
                if stream:
                    async with aclosing(
                        cast("AsyncGenerator[str, None]", self._provider._http.post_stream(url, body, headers))
                    ) as chunks:
                        async for data in chunks:
                            if data == "[DONE]":
                                received = True
                                break
                            frame = load_object(data)
                            # An explicit first-frame rejection is safe to replay;
                            # even a prior non-visible protocol frame is not.
                            if (
                                not received
                                and frame.get("type") == "error"
                                and rejects_additional_tools(frame.get("error", frame))
                            ):
                                raise AdditionalToolsUnsupported()
                            received = True
                            events = accumulator.add(frame)
                            if accumulator.completed:
                                break
                            for event in events:
                                yield event
                    if not accumulator.completed:
                        raise ProtocolError("Responses stream ended before completion; no tools were released.")
                else:
                    result = await self._provider._http.post_json(url, body, headers)
                    events = accumulator.finish(result)
                # Terminal events repeat usage on text and each tool; count only
                # the validated request, before the caller can stop consuming.
                for terminal in events:
                    if isinstance(terminal, TextDoneEvent):
                        if terminal.finish_reason in {FinishReason.STOP, FinishReason.TOOL_CALLS}:
                            self._provider._record_conversation_cache(cache_key, terminal.usage)
                        break
                for event in events:
                    yield event
                return
            except AdditionalToolsUnsupported:
                if received or attempt or body == fallback_body:
                    raise
                self._provider._responses_tools_unsupported.add(route)
                self._provider._responses_tool_timelines.pop(key, None)
                body = fallback_body
