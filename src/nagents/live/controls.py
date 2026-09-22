"""Concrete Live commands, acknowledgment correlation and final usage state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import cast
from uuid import uuid4

from ..types import ContentPart
from ..types import ImageContent
from ..types import TextContent

Payload = dict[str, object]
Send = Callable[[Payload], Awaitable[None]]


class LiveCommandError(RuntimeError):
    """A rejected command, distinct from a failed connection."""


@dataclass
class LiveStatus:
    session_id: str = ""
    seconds: float = 0
    context_ratio: float = 0
    finalized: bool = False
    reason: str = ""
    snapshot: Payload = field(default_factory=dict)


class LiveControls:
    def __init__(self) -> None:
        self.sender: Send | None = None
        self.delegations: set[str] = set()
        self.status = LiveStatus()
        self.responses = False
        self.closing = False
        self.revision = 0
        self.pending_tools = 0
        self.input_content: list[ContentPart] = []
        self._pending: dict[str, asyncio.Future[Payload]] = {}
        self.submit_input: Callable[[list[ContentPart]], Awaitable[str]] | None = None

    async def command(self, kind: str, *, acknowledged: bool = True, **payload: object) -> str:
        if self.sender is None or self.closing:
            raise RuntimeError("No active GPT-Live connection; wait for session.started")
        event_id = uuid4().hex
        if acknowledged:
            if len(self._pending) >= 1024:
                # Completed results need not accumulate forever in a long call.
                self._pending = {key: value for key, value in self._pending.items() if not value.done()}
            if len(self._pending) >= 1024:
                raise RuntimeError("Too many pending Live commands")
            self._pending[event_id] = asyncio.get_running_loop().create_future()
        try:
            await self.sender({"type": kind, "event_id": event_id, **payload})
        except BaseException:
            self._pending.pop(event_id, None)
            raise
        return event_id

    async def append(self, kind: str, content: str, identifier: str = "") -> str:
        if kind not in {"commentary", "thinking", "instructions"}:
            raise ValueError("Unknown Live append channel")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Live updates require nonempty text (maximum 500 API tokens)")
        if identifier and identifier not in self.delegations:
            raise ValueError("Unknown client delegation ID for this Live connection")
        return await self.command(f"session.{kind}.append", content=content, delegation_id=identifier or None)

    def observe(self, event: Payload) -> None:
        session = event.get("session")
        if isinstance(session, dict):
            self.status.session_id = str(session.get("id", self.status.session_id))
            self.status.snapshot = cast("Payload", session)
        usage = event.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("seconds"), int | float):
            self.status.seconds = float(usage["seconds"])
        window = event.get("context_window")
        if isinstance(window, dict) and isinstance(window.get("usage_ratio"), int | float):
            self.status.context_ratio = float(window["usage_ratio"])
        error = event.get("error")
        identifier = str(event.get("client_event_id", ""))
        if isinstance(error, dict):
            identifier = str(error.get("client_event_id") or identifier)
        future = self._pending.get(identifier)
        if future is not None and not future.done():
            # Store error payloads, not unobserved Future exceptions.
            future.set_result(event)
        if event.get("type") == "session.closed":
            self.status.finalized = True
            self.status.reason = str(event.get("reason", ""))
            self.disconnect()

    def disconnect(self) -> None:
        self.sender = None
        for identifier, future in self._pending.items():
            if not future.done():
                future.set_result(
                    {"type": "error", "error": {"code": "connection_closed", "client_event_id": identifier}}
                )

    async def wait(self, event_id: str, timeout: float = 15) -> Payload:
        future = self._pending.get(event_id)
        if future is None:
            raise ValueError("Unknown or expired Live command ID")
        result = await asyncio.wait_for(asyncio.shield(future), timeout)
        if result.get("type") == "error":
            error = result.get("error")
            code = error.get("code", "command_rejected") if isinstance(error, dict) else "command_rejected"
            raise LiveCommandError(f"Live command rejected: {code}")
        return result

    async def mute_input(self) -> str:
        return await self.command("session.input_audio.mute")

    async def unmute_input(self) -> str:
        return await self.command("session.input_audio.unmute")

    async def close(self) -> str:
        identifier = await self.command("session.close")
        self.closing = True
        return identifier

    def invalidate_tasks(self) -> int:
        """Call when application task intent changes, not on every audio fragment."""
        self.revision += 1
        return self.revision

    async def update_backend(self, **settings: object) -> str:
        allowed = {
            "model",
            "instructions",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "max_output_tokens",
            "service_tier",
            "reasoning",
            "text",
        }
        if not self.responses or not settings or settings.keys() - allowed:
            raise ValueError("Only supported Responses backend settings can change during a Live session")
        return await self.command(
            "session.update", session={"delegation": {"type": "responses", "responses": settings}}
        )

    async def submit(self, content: list[ContentPart]) -> str:
        if self.submit_input is None:
            raise RuntimeError("Backend input is unavailable before Live startup")
        return await self.submit_input(content)

    async def submit_text(self, text: str) -> str:
        return await self.submit([TextContent(text=text)])

    async def submit_image(self, image: ImageContent, text: str = "Describe this image for the caller.") -> str:
        return await self.submit([TextContent(text=text), image])
