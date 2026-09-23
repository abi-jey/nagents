"""Hosted Responses function batches executed through the Agent's tool executor."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from ..events import Event
from ..events import ToolCallEvent
from ..events import ToolResultEvent
from ..types import ContentPart
from ..types import ImageContent
from ..types import TextContent
from ..types import ToolCall

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from ..agent import Agent
    from .controls import LiveControls


class HostedTools:
    def __init__(self, agent: Agent, controls: LiveControls, emit: Callable[[Event], Awaitable[None]]) -> None:
        self.agent = agent
        self.controls = controls
        self.emit = emit
        self.responses: dict[str, str] = {}
        self.calls: dict[str, list[dict[str, object]]] = {}
        self.seen: set[str] = set()
        self.finished: set[str] = set()
        self.revisions: dict[str, int] = {}
        self.pending: asyncio.Queue[tuple[str, str, list[dict[str, object]], int]] = asyncio.Queue(maxsize=32)

    def observe(self, envelope: dict[str, object]) -> None:
        if envelope.get("type") != "response.event":
            return
        event = envelope.get("event")
        if not isinstance(event, dict):
            raise ValueError("Malformed hosted response event")
        delegation = str(envelope.get("delegation_id", ""))
        kind = event.get("type")
        response = event.get("response", {})
        if kind == "response.created" and isinstance(response, dict):
            self.responses[delegation] = str(response["id"])
            self.revisions[str(response["id"])] = self.controls.revision
        identifier = self.responses.get(delegation, "")
        if kind in {"response.failed", "response.incomplete"}:
            abandoned = self.calls.pop(identifier, [])
            self.controls.pending_tools -= len(abandoned)
            self.finished.add(identifier)
            return
        if kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                if not identifier:
                    raise ValueError("Hosted function arrived without response.created")
                call_id = str(item["call_id"])
                if call_id not in self.seen:
                    self.seen.add(call_id)
                    self.calls.setdefault(identifier, []).append(item)
                    self.controls.pending_tools += 1
        if kind == "response.completed" and identifier not in self.finished:
            self.finished.add(identifier)
            calls = self.calls.pop(identifier, [])
            if calls:
                self.pending.put_nowait(
                    (delegation, identifier, calls, self.revisions.get(identifier, self.controls.revision))
                )

    async def run(self) -> None:
        while True:
            delegation, response, calls, revision = await self.pending.get()
            for call in calls:
                call_id, name = str(call["call_id"]), str(call["name"])
                metadata = {"source": "live_backend", "delegation_id": delegation, "response_id": response}
                try:
                    arguments = json.loads(str(call["arguments"]))
                    json.dumps(arguments, allow_nan=False)
                    if not isinstance(arguments, dict):
                        raise ValueError("Function arguments must be an object")
                    await self.emit(ToolCallEvent(id=call_id, name=name, arguments=arguments, extra=metadata))
                    if revision != self.controls.revision:
                        result = ToolResultEvent(
                            id=call_id, name=name, error="Skipped: application task was superseded"
                        )
                    else:
                        result = await self.agent.tool_executor.execute(
                            ToolCall(id=call_id, name=name, arguments=arguments)
                        )
                except (ValueError, TypeError, KeyError):
                    result = ToolResultEvent(id=call_id, name=name, error="Invalid function call")
                result.extra = {**result.extra, **metadata}
                await self.emit(result)
                await self.controls.command(
                    "response.item.create",
                    acknowledged=False,
                    item={
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"error": result.error} if result.error else result.result, default=str),
                    },
                )
                self.controls.pending_tools -= 1
            await self.controls.command("response.create", acknowledged=False)

    async def submit(self, content: list[ContentPart]) -> str:
        parts: list[dict[str, object]] = []
        for part in content:
            if isinstance(part, TextContent):
                parts.append({"type": "input_text", "text": part.text})
            elif isinstance(part, ImageContent):
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                        "detail": part.detail or "auto",
                    }
                )
            else:
                raise ValueError("Live backend input supports text and images")
        identifier = await self.controls.command(
            "response.item.create", acknowledged=False, item={"type": "message", "role": "user", "content": parts}
        )
        if not self.controls.pending_tools:
            await self.controls.command("response.create", acknowledged=False)
        return identifier
