"""Generic OpenAI Responses API format, independent of ChatGPT/Codex OAuth."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..events import FinishReason
from ..events import ReasoningChunkEvent
from ..events import TextChunkEvent
from ..events import TextDoneEvent
from ..events import ToolCallEvent
from ..types import COMPACTION_SUMMARY_PREFIX
from ..types import ImageContent
from ..types import TextContent
from ._validation import ProtocolError
from ._validation import list_data
from ._validation import object_data
from ._validation import string_data
from ._validation import token_usage
from ._validation import tool_arguments

if TYPE_CHECKING:
    from ..events import Event
    from ..types import GenerationConfig
    from ..types import Message
    from ..types import ToolDefinition


def format_request(
    model: str,
    messages: list[Message],
    tools: list[ToolDefinition] | None,
    config: GenerationConfig | None,
    stream: bool,
) -> dict[str, object]:
    inputs: list[dict[str, object]] = []
    for message in messages:
        role = "user" if message.role == "compaction_summary" else message.role
        if role == "tool":
            if not message.tool_call_id or message.tool_calls:
                raise ProtocolError("Responses tool results require a call ID and no tool calls.")
            if message.content is None or isinstance(message.content, str):
                output = message.content or ""
            elif all(isinstance(part, TextContent) for part in message.content):
                output = "\n".join(part.text for part in message.content if isinstance(part, TextContent))
            else:
                raise ProtocolError("Responses tool results currently support text only.")
            inputs.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": output})
            continue
        content: list[dict[str, object]] = []
        parts = [TextContent(message.content)] if isinstance(message.content, str) else message.content or []
        for part in parts:
            if isinstance(part, TextContent):
                text = COMPACTION_SUMMARY_PREFIX + part.text if message.role == "compaction_summary" else part.text
                content.append({"type": "output_text" if role == "assistant" else "input_text", "text": text})
            elif isinstance(part, ImageContent) and role == "user":
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                        "detail": part.detail or "auto",
                    }
                )
            else:
                raise ProtocolError("Responses supports text and user images, not audio or document attachments.")
        if content:
            inputs.append({"role": role, "content": content})
        for call in message.tool_calls:
            if role != "assistant" or not call.id or not call.name:
                raise ProtocolError("Responses history contains an invalid assistant tool call.")
            # Reasoning items are replayed with the first call of the turn. This
            # survives session persistence through existing ToolCall.metadata.
            if "responses_reasoning" in call.metadata:
                for item in list_data(json.loads(call.metadata["responses_reasoning"])):
                    reasoning = object_data(item)
                    if reasoning.get("type") != "reasoning":
                        raise ProtocolError("Responses history contains invalid reasoning metadata.")
                    inputs.append(reasoning)
            inputs.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, allow_nan=False),
                }
            )
    body: dict[str, object] = {
        "model": model,
        "input": inputs,
        "stream": stream,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            }
            for tool in tools
        ]
    if config:
        if config.stop or config.thinking_config or config.reasoning:
            raise ProtocolError(
                "Responses does not support stop or provider-specific thinking/reasoning settings in GenerationConfig."
            )
        for name, value in (
            ("temperature", config.temperature),
            ("top_p", config.top_p),
            ("max_output_tokens", config.max_tokens),
        ):
            if value is not None:
                body[name] = value
    return body


def _index(value: object) -> int:
    if type(value) is not int or not 0 <= value < 1024:
        raise ProtocolError("Responses returned an invalid output index.")
    return value


class ResponseAccumulator:
    """Validate the entire response before releasing any executable tool calls."""

    def __init__(self) -> None:
        self.items: dict[int, dict[str, object]] = {}
        self.finished: set[int] = set()
        self.arguments: dict[int, str] = {}
        self.arguments_done: set[int] = set()
        self.texts: dict[tuple[int, int], str] = {}
        self.texts_done: set[tuple[int, int]] = set()
        self.completed = False

    def _merge(self, index: int, item: dict[str, object]) -> None:
        previous = self.items.get(index, {})
        for key in ("id", "call_id", "name", "type"):
            if previous.get(key) and item.get(key) and previous[key] != item[key]:
                raise ProtocolError("Responses returned inconsistent output identities.")
        if (
            previous.get("arguments")
            and item.get("arguments")
            and tool_arguments(previous["arguments"]) != tool_arguments(item["arguments"])
        ):
            raise ProtocolError("Responses returned inconsistent completed tool arguments.")
        self.items[index] = {**previous, **item}

    def add(self, event: dict[str, object]) -> list[Event]:
        kind = string_data(event.get("type"))
        if kind in {"error", "response.failed", "response.cancelled"} or event.get("error"):
            raise ProtocolError("Responses generation failed; no tools were released.")
        if kind in {"response.output_item.added", "response.output_item.done"}:
            index = _index(event.get("output_index"))
            self._merge(index, object_data(event.get("item")))
            if kind.endswith(".done"):
                self.finished.add(index)
        elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            index = _index(event.get("output_index"))
            item = self.items.setdefault(index, {})
            if event.get("item_id"):
                self._merge(index, {"id": event["item_id"]})
                item = self.items[index]
            if index in self.finished or index in self.arguments_done:
                raise ProtocolError("Responses sent arguments after an output item finished.")
            if kind.endswith(".delta"):
                self.arguments[index] = self.arguments.get(index, "") + string_data(event.get("delta"))
            else:
                item["arguments"] = string_data(event.get("arguments"))
                self.arguments_done.add(index)
        elif kind in {
            "response.output_text.delta",
            "response.output_text.done",
            "response.refusal.delta",
            "response.refusal.done",
        }:
            index = _index(event.get("output_index"))
            key = (index, _index(event.get("content_index")))
            if event.get("item_id"):
                self._merge(index, {"id": event["item_id"]})
            if key in self.texts_done or index in self.finished:
                raise ProtocolError("Responses sent text after a content part finished.")
            if kind.endswith(".delta"):
                delta = string_data(event.get("delta"))
                self.texts[key] = self.texts.get(key, "") + delta
                return [TextChunkEvent(chunk=delta)]
            text = string_data(event.get("refusal" if "refusal" in kind else "text"))
            if key in self.texts and self.texts[key] != text:
                raise ProtocolError("Responses returned inconsistent completed text.")
            self.texts[key] = text
            self.texts_done.add(key)
        elif kind == "response.reasoning_summary_text.delta":
            return [ReasoningChunkEvent(chunk=string_data(event.get("delta")))]
        elif kind in {"response.completed", "response.incomplete"}:
            result = object_data(event.get("response"))
            if result.get("status") != kind.removeprefix("response."):
                raise ProtocolError("Responses returned an inconsistent terminal status.")
            return self.finish(result)
        return []

    def finish(self, result: dict[str, object]) -> list[Event]:
        status = result.get("status")
        if result.get("error") or status not in {"completed", "incomplete"}:
            raise ProtocolError("Responses generation failed or did not complete; no tools were released.")
        output = list_data(result.get("output"))
        if len(output) > 1024:
            raise ProtocolError("Responses returned too many output items.")
        observed = set(self.items) | set(self.arguments) | {index for index, _ in self.texts}
        if output:
            if observed - set(range(len(output))):
                raise ProtocolError("Responses terminal output omitted streamed items.")
            for index, raw in enumerate(output):
                self._merge(index, object_data(raw))
                self.finished.add(index)
        elif observed - self.finished:
            raise ProtocolError("Responses ended before streamed items completed.")
        usage = token_usage(result.get("usage"), "responses")
        extra: dict[str, object] = {key: result[key] for key in ("id", "model", "created_at") if key in result}
        calls: list[ToolCallEvent] = []
        call_ids: set[str] = set()
        reasoning: list[dict[str, object]] = []
        texts: dict[tuple[int, int], str] = {}
        reason = FinishReason.STOP
        if status == "incomplete":
            incomplete = object_data(result.get("incomplete_details") or {}).get("reason")
            if incomplete not in {"max_output_tokens", "content_filter"}:
                raise ProtocolError("Responses generation was incomplete; no tools were released.")
            reason = FinishReason.LENGTH if incomplete == "max_output_tokens" else FinishReason.CONTENT_FILTER
        for index, item in sorted(self.items.items()):
            kind = item.get("type")
            if kind == "function_call":
                call_id, name = string_data(item.get("call_id")), string_data(item.get("name"))
                if (
                    status != "completed"
                    or item.get("status") not in {None, "completed"}
                    or not call_id
                    or not name
                    or call_id in call_ids
                ):
                    raise ProtocolError(
                        "Responses returned an invalid or incomplete tool call; no tools were released."
                    )
                args = tool_arguments(item.get("arguments", self.arguments.get(index, "")))
                if index in self.arguments and tool_arguments(self.arguments[index]) != args:
                    raise ProtocolError("Responses returned inconsistent streamed tool arguments.")
                calls.append(ToolCallEvent(id=call_id, name=name, arguments=args, usage=usage, extra=extra))
                call_ids.add(call_id)
            elif index in self.arguments:
                raise ProtocolError("Responses returned arguments without a function call.")
            elif kind == "message":
                if status == "completed" and item.get("status") not in {None, "completed"}:
                    raise ProtocolError("Responses returned an unfinished message.")
                for part_index, raw_part in enumerate(list_data(item.get("content"))):
                    part = object_data(raw_part)
                    if part.get("type") == "output_text":
                        texts[index, part_index] = string_data(part.get("text"))
                    elif part.get("type") == "refusal":
                        texts[index, part_index] = string_data(part.get("refusal"))
                    else:
                        raise ProtocolError("Responses returned unsupported message content.")
            elif kind == "reasoning":
                reasoning.append(
                    {key: item[key] for key in ("type", "id", "summary", "encrypted_content") if key in item}
                )
            else:
                raise ProtocolError("Responses returned an unsupported output item.")
        for key, text in self.texts.items():
            if texts.get(key) != text:
                raise ProtocolError("Responses terminal output did not match streamed text.")
        if calls and reasoning:
            calls[0].metadata["responses_reasoning"] = json.dumps(reasoning, allow_nan=False)
        self.completed = True
        return [
            TextDoneEvent(
                text="".join(text for _, text in sorted(texts.items())),
                usage=usage,
                finish_reason=FinishReason.TOOL_CALLS if calls else reason,
                extra=extra,
            ),
            *calls,
        ]
