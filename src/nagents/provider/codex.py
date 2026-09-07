"""ChatGPT subscription OAuth transport for Codex's Responses API.

This is not an API-key provider. Credentials only go to the fixed Codex endpoint;
base Provider HTTP logging, endpoint configuration, and retries are not used.
Tools are released only after a validated completed response, never from a
partially received stream. GenerationConfig.reasoning.enabled requests an auto
reasoning summary; sampling settings and token budgets are unsupported here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from typing import TYPE_CHECKING
from typing import cast

import aiohttp

from ..events import ErrorEvent
from ..events import FinishReason
from ..events import ReasoningChunkEvent
from ..events import TextChunkEvent
from ..events import TextDoneEvent
from ..events import ToolCallEvent
from ..events import Usage
from ..types import COMPACTION_SUMMARY_PREFIX
from ..types import ImageContent
from ..types import TextContent
from .base import Provider
from .base import ProviderType

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from ..events import Event
    from ..types import GenerationConfig
    from ..types import Message
    from ..types import ToolDefinition

DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
try:
    USER_AGENT = f"ngn/{version('nagents')}"
except PackageNotFoundError:
    USER_AGENT = "ngn/unknown"

_MAX_EVENT_BYTES = 4 * 1024 * 1024
_MAX_STREAM_BYTES = 32 * 1024 * 1024
_MAX_ITEMS = 1024


@dataclass
class CodexCredentials:
    access_token: str = field(repr=False)
    account_id: str = field(default="", repr=False)
    residency: str = field(default="", repr=False)


class _ProtocolError(ValueError):
    """Only constant, safe messages belong in this exception."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _ProtocolError("Codex returned malformed response data; retry the request.")
    return cast("dict[str, object]", value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise _ProtocolError("Codex returned malformed response text; retry the request.")
    return value


def _text(message: Message) -> str:
    if message.content is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    if not all(isinstance(part, TextContent) for part in message.content):
        raise _ProtocolError("Codex supports images only in user messages; audio and documents are unsupported.")
    return "\n".join(part.text for part in message.content if isinstance(part, TextContent))


def _request_body(
    model: str, messages: list[Message], tools: list[ToolDefinition] | None, config: GenerationConfig | None
) -> dict[str, object]:
    instructions: list[str] = []
    inputs: list[dict[str, object]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            instructions.append(_text(message))
        elif message.role == "tool":
            if not message.tool_call_id:
                raise _ProtocolError("A tool result is missing its call ID; repair the session history.")
            inputs.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": _text(message)})
        else:
            role = "user" if message.role == "compaction_summary" else message.role
            if role not in {"user", "assistant"}:
                raise _ProtocolError("Codex received an unsupported message role.")
            content: list[dict[str, object]] = []
            if message.role == "compaction_summary":
                content.append({"type": "input_text", "text": COMPACTION_SUMMARY_PREFIX + _text(message)})
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, TextContent):
                        content.append(
                            {"type": "output_text" if role == "assistant" else "input_text", "text": part.text}
                        )
                    elif isinstance(part, ImageContent) and role == "user":
                        if part.media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                            raise _ProtocolError("Codex supports PNG, JPEG, WebP, and GIF images only.")
                        content.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                                "detail": part.detail or "auto",
                            }
                        )
                    else:
                        raise _ProtocolError("Codex supports user images and text, not audio or document attachments.")
            elif message.content is not None:
                content.append(
                    {"type": "output_text" if role == "assistant" else "input_text", "text": message.content}
                )
            if content:
                inputs.append({"role": role, "content": content})
            for call in message.tool_calls:
                if role != "assistant" or not call.id or not call.name:
                    raise _ProtocolError("Invalid assistant tool call in session history.")
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
        "instructions": "\n\n".join(instructions),
        "input": inputs,
        "store": False,
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            }
            for tool in tools or []
        ],
    }
    if config and config.reasoning and config.reasoning.get("enabled"):
        body["reasoning"] = {"summary": "auto"}
    return body


async def _sse(response: aiohttp.ClientResponse) -> AsyncIterator[dict[str, object]]:
    buffer = b""
    data: list[bytes] = []
    frame_size = 0
    total = 0
    async for chunk in response.content.iter_chunked(8192):
        total += len(chunk)
        if total > _MAX_STREAM_BYTES:
            raise _ProtocolError("Codex response exceeded the safe stream size limit.")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            frame_size += len(line)
            if frame_size > _MAX_EVENT_BYTES:
                raise _ProtocolError("Codex response exceeded the safe event size limit.")
            if not line:
                if data:
                    payload = b"\n".join(data)
                    if payload == b"[DONE]":
                        return
                    try:
                        parsed: object = json.loads(payload)
                    except (ValueError, UnicodeError, RecursionError):
                        raise _ProtocolError("Codex returned malformed stream JSON; retry the request.") from None
                    yield _object(parsed)
                data = []
                frame_size = 0
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
        if len(buffer) + frame_size > _MAX_EVENT_BYTES:
            raise _ProtocolError("Codex response exceeded the safe event size limit.")
    if data or buffer.strip():
        raise _ProtocolError("Codex stream ended inside an event; retry the request.")


def _index(value: object) -> int:
    if type(value) is not int or not 0 <= value < _MAX_ITEMS:
        raise _ProtocolError("Codex returned an invalid output index.")
    return value


def _usage(value: object) -> Usage:
    usage = _object(value) if value is not None else {}

    def count(data: dict[str, object], name: str) -> int:
        result = data.get(name, 0)
        if type(result) is not int or result < 0:
            raise _ProtocolError("Codex returned invalid token usage.")
        return result

    return Usage(
        prompt_tokens=count(usage, "input_tokens"),
        completion_tokens=count(usage, "output_tokens"),
        total_tokens=count(usage, "total_tokens")
        if "total_tokens" in usage
        else count(usage, "input_tokens") + count(usage, "output_tokens"),
        cached_tokens=count(_object(usage.get("input_tokens_details") or {}), "cached_tokens"),
        reasoning_tokens=count(_object(usage.get("output_tokens_details") or {}), "reasoning_tokens"),
    )


class CodexProvider(Provider):
    """Responses streaming with per-request OAuth credentials, no model discovery.

    verify_model does not assert account entitlement; the Responses service does.
    No automatic retry/replay follows authentication, rate-limit, or stream errors.
    """

    def __init__(
        self,
        credentials: Callable[[], Awaitable[CodexCredentials]],
        model: str = DEFAULT_CODEX_MODEL,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "oauth-not-an-api-key", model, timeout=timeout)
        self.base_url = CODEX_ENDPOINT
        self._credentials = credentials
        self._timeout = timeout

    async def verify_model(self, force: bool = False) -> bool:
        self._model_verified = True
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncGenerator[Event, None]:
        try:
            body = _request_body(self.model, messages, tools, config)
        except (TypeError, ValueError) as error:
            message = str(error) if isinstance(error, _ProtocolError) else "Codex request data is invalid."
            yield ErrorEvent(message=message, code="CODEX_REQUEST_INVALID")
            return
        try:
            credentials = await self._credentials()
            if not credentials.access_token or not re.fullmatch(r"[\x21-\x7e]{1,65536}", credentials.access_token):
                raise ValueError
            if credentials.account_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", credentials.account_id):
                raise ValueError
            if credentials.residency and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", credentials.residency):
                raise ValueError
        except Exception:
            yield ErrorEvent(
                message="ChatGPT credentials are unavailable; sign in again with /login.", code="CODEX_AUTH"
            )
            return
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "Accept": "text/event-stream",
            "originator": "ngn",
            "User-Agent": USER_AGENT,
        }
        if credentials.account_id:
            headers["ChatGPT-Account-Id"] = credentials.account_id
        if credentials.residency and credentials.residency != "no_constraint":
            headers["x-openai-internal-codex-residency"] = credentials.residency
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                ) as client,
                client.post(CODEX_ENDPOINT, json=body, headers=headers, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    messages_by_status = {
                        401: "ChatGPT login expired or was rejected; sign in again with /login.",
                        403: "Codex access denied; check your ChatGPT plan, workspace permissions, and model access.",
                        429: "ChatGPT Codex usage limit reached; wait before retrying or check your plan limits.",
                    }
                    yield ErrorEvent(
                        message=messages_by_status.get(
                            response.status,
                            f"Codex request failed (HTTP {response.status}); check service availability and model access.",
                        ),
                        code=f"CODEX_HTTP_{response.status}",
                    )
                    return
                # Some Codex responses omit Content-Type; _sse still validates framing
                # and no calls are released without a completed, validated response.
                if response.headers.get("Content-Type") and response.content_type != "text/event-stream":
                    raise _ProtocolError("Codex did not return an event stream; retry the request.")
                items: dict[int, dict[str, object]] = {}
                finished: set[int] = set()
                arguments: dict[int, str] = {}
                texts: dict[tuple[int, int], str] = {}
                completed = False
                usage = Usage()
                async for event in _sse(response):
                    kind = _string(event.get("type"))
                    if kind in {"error", "response.failed", "response.incomplete"}:
                        raise _ProtocolError(
                            "Codex response failed or was incomplete; no tool calls were released. Retry the request."
                        )
                    if kind in {"response.output_item.added", "response.output_item.done"}:
                        index = _index(event.get("output_index"))
                        item = _object(event.get("item"))
                        previous = items.get(index, {})
                        for key in ("id", "call_id", "name", "type"):
                            if key in previous and key in item and previous[key] != item[key]:
                                raise _ProtocolError("Codex returned inconsistent output item identities.")
                        items[index] = {**previous, **item}
                        if kind == "response.output_item.done":
                            finished.add(index)
                    elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                        index = _index(event.get("output_index"))
                        item = items.setdefault(index, {})
                        if "item_id" in event and "id" in item and event["item_id"] != item["id"]:
                            raise _ProtocolError("Codex returned inconsistent argument item identities.")
                        if kind.endswith(".delta"):
                            arguments[index] = arguments.get(index, "") + _string(event.get("delta"))
                        else:
                            item["arguments"] = _string(event.get("arguments"))
                    elif kind in {"response.output_text.delta", "response.output_text.done"}:
                        text_key = (_index(event.get("output_index", 0)), _index(event.get("content_index", 0)))
                        if kind.endswith(".delta"):
                            delta = _string(event.get("delta"))
                            texts[text_key] = texts.get(text_key, "") + delta
                            if stream:
                                yield TextChunkEvent(chunk=delta)
                        else:
                            texts[text_key] = _string(event.get("text"))
                    elif kind == "response.reasoning_summary_text.delta":
                        delta = _string(event.get("delta"))
                        if stream:
                            yield ReasoningChunkEvent(chunk=delta)
                    elif kind == "response.completed":
                        result = _object(event.get("response"))
                        if result.get("status", "completed") != "completed" or result.get("error"):
                            raise _ProtocolError("Codex response was not completed; no tool calls were released.")
                        output = result.get("output")
                        if not isinstance(output, list) or len(output) > _MAX_ITEMS:
                            raise _ProtocolError("Codex completed response has malformed output.")
                        observed = set(items) | set(arguments) | {index for index, _ in texts}
                        # Codex may send output only in output_item.done, leaving the
                        # terminal response's output array empty to avoid duplication.
                        if not output and observed - finished:
                            raise _ProtocolError(
                                "Codex completed before streamed output finished; no tool calls were released."
                            )
                        if output and observed - set(range(len(output))):
                            raise _ProtocolError(
                                "Codex completed response omitted streamed output; no tool calls were released."
                            )
                        for index, raw in enumerate(output):
                            item = _object(raw)
                            previous = items.get(index, {})
                            for identity in ("id", "call_id", "name", "type"):
                                if identity in previous and identity in item and previous[identity] != item[identity]:
                                    raise _ProtocolError("Codex returned inconsistent completed output identities.")
                            items[index] = {**previous, **item}
                            finished.add(index)
                        usage = _usage(result.get("usage"))
                        completed = True
                        break
                if not completed:
                    raise _ProtocolError("Codex stream ended before response.completed; no tool calls were released.")
                calls: list[ToolCallEvent] = []
                call_ids: set[str] = set()
                for index, item in sorted(items.items()):
                    if item.get("type") == "function_call":
                        call_id, name = _string(item.get("call_id")), _string(item.get("name"))
                        if (
                            not call_id
                            or not name
                            or call_id in call_ids
                            or index not in finished
                            or item.get("status") not in (None, "completed")
                        ):
                            raise _ProtocolError("Codex returned an invalid or unfinished tool call.")
                        raw_arguments = _string(item.get("arguments", arguments.get(index, "")))
                        try:
                            parsed_args = _object(json.loads(raw_arguments))
                            # JSON serialization rejects non-finite values accepted by Python's decoder.
                            json.dumps(parsed_args, allow_nan=False)
                            if arguments.get(index) and _object(json.loads(arguments[index])) != parsed_args:
                                raise _ProtocolError("Codex returned inconsistent tool arguments.")
                        except (ValueError, UnicodeError, RecursionError):
                            raise _ProtocolError(
                                "Codex returned malformed tool arguments; no tool calls were released."
                            ) from None
                        call_ids.add(call_id)
                        calls.append(ToolCallEvent(id=call_id, name=name, arguments=parsed_args, usage=usage))
                    elif index in arguments:
                        raise _ProtocolError("Codex returned tool arguments without a complete tool call.")
                    elif item.get("type") == "message":
                        content = item.get("content")
                        if not isinstance(content, list):
                            raise _ProtocolError("Codex returned malformed message content.")
                        for part_index, raw_part in enumerate(content):
                            part = _object(raw_part)
                            if part.get("type") == "output_text":
                                texts[(index, part_index)] = _string(part.get("text"))
                            elif part.get("type") == "refusal":
                                texts[(index, part_index)] = _string(part.get("refusal"))
            # Finish network cleanup before releasing executable calls.
            yield TextDoneEvent(
                text="".join(text for _, text in sorted(texts.items())),
                usage=usage,
                finish_reason=FinishReason.TOOL_CALLS if calls else FinishReason.STOP,
            )
            for call in calls:
                yield call
        except _ProtocolError as error:
            yield ErrorEvent(message=str(error), code="CODEX_STREAM_INVALID")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            yield ErrorEvent(
                message="Codex connection failed or timed out; retry the request.", code="CODEX_CONNECTION"
            )
