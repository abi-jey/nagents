"""Native Codex handoffs carry request text independently of speech captions."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast

from nagents.live.runtime import result_chunks

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

MAX_HANDOFF_TEXT = 12000
MAX_HANDOFF_PARTS = 512
_CLARIFY = "I could not read that request. Please repeat it clearly or type it in the chat."
Payload = dict[str, object]


@dataclass(frozen=True)
class LoginHandoff:
    identifier: str
    text: str
    transcript: str = "[]"
    offset_ms: float | None = None


def handoff_offset(value: object) -> float | None:
    """Keep observed chronology without treating it as transcript completion."""
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1e12:
        return float(value)
    return None


def _request_text(item: object) -> str:
    # Codex's frameless-bidi parser joins input_text content, keyed by item.id.
    # This is a native contract; public Live delegation notices have no text.
    if not isinstance(item, dict) or item.get("type") != "delegation":
        return ""
    content = item.get("content")
    if not isinstance(content, list) or len(content) > MAX_HANDOFF_PARTS:
        return ""
    parts: list[str] = []
    length = 0
    for part in content:
        if not isinstance(part, dict):
            return ""
        if part.get("type") != "input_text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            return ""
        length += len(text)
        if length > MAX_HANDOFF_TEXT:
            return ""
        parts.append(text)
    text = "".join(parts)
    return text if text.strip() else ""


class LoginDelegations:
    """One bounded native lane, with immutable context captured at each notice."""

    def __init__(
        self, backend: Callable[[LoginHandoff], Awaitable[str]], send: Callable[[Payload], Awaitable[None]]
    ) -> None:
        self.backend = backend
        self.send = send
        self.pending: asyncio.Queue[LoginHandoff] = asyncio.Queue(maxsize=32)
        self.seen: set[str] = set()
        self.fragments: deque[Payload] = deque()
        self.characters = 0

    def observe(self, event: Payload, raw: Payload) -> None:
        kind = event.get("type")
        if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            text = event.get("delta")
            if not isinstance(text, str):
                return
            self.fragments.append(
                {
                    "speaker": "user" if kind == "session.input_transcript.delta" else "assistant",
                    "text": text,
                    "start_ms": event["start_ms"],
                    "end_ms": event["end_ms"],
                }
            )
            self.characters += len(text)
            while self.characters > MAX_HANDOFF_TEXT or len(self.fragments) > MAX_HANDOFF_PARTS:
                self.characters -= len(str(self.fragments.popleft()["text"]))
        elif kind == "session.delegation.created":
            delegation = event.get("delegation")
            if not isinstance(delegation, dict) or delegation.get("target") != "client":
                return
            identifier = delegation.get("id")
            if not isinstance(identifier, str) or identifier in self.seen:
                return
            self.seen.add(identifier)
            text = _request_text(raw.get("item"))
            fragments = sorted(self.fragments, key=lambda part: cast("float", part["start_ms"]))
            transcript = json.dumps(fragments, ensure_ascii=False) if text else "[]"
            try:
                self.pending.put_nowait(
                    LoginHandoff(identifier, text, transcript, handoff_offset(raw.get("offset_ms")))
                )
            except asyncio.QueueFull:
                raise RuntimeError("Live client delegation queue is full") from None

    async def run(self) -> None:
        while True:
            request = await self.pending.get()
            result = _CLARIFY
            if request.text:
                try:
                    async with asyncio.timeout(420):
                        result = await self.backend(request)
                except Exception:
                    result = "The backend could not complete this request."
            for part in result_chunks(result):
                await self.send({"delegation_id": request.identifier, "content": part})
