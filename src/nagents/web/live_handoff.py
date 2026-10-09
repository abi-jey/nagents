"""Native Codex handoffs carry request text independently of speech captions."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING
from typing import cast

from nagents.live.delegation import ClientDelegationRequest
from nagents.live.delegation import delegation_workers
from nagents.live.runtime import result_chunks

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.live.delegation import ClientDelegationObserver

MAX_HANDOFF_TEXT = 12000
MAX_HANDOFF_PARTS = 512
_CLARIFY = "I could not read that request. Please repeat it clearly or type it in the chat."
Payload = dict[str, object]
LoginHandoff = ClientDelegationRequest


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
    """Bounded independent execution of immutable native request items."""

    def __init__(
        self,
        backend: Callable[[LoginHandoff], Awaitable[str]],
        send: Callable[[Payload], Awaitable[None]],
        *,
        observer: ClientDelegationObserver | None = None,
        concurrency: int = 4,
    ) -> None:
        self.backend = backend
        self.send = send
        self.observer = observer
        if type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise ValueError("Live client concurrency must be between 1 and 32")
        self.concurrency = concurrency
        self.closed = False
        self.pending: asyncio.Queue[LoginHandoff] = asyncio.Queue(maxsize=32)
        self.seen: set[str] = set()
        self.fragments: deque[Payload] = deque()
        self.characters = 0

    def observe(self, event: Payload, raw: Payload) -> None:
        kind = event.get("type")
        if kind == "session.closed":
            self.closed = True
        if self.closed:
            return
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
            if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256:
                raise ValueError("Invalid Live client delegation ID")
            if identifier in self.seen:
                return
            text = _request_text(raw.get("item"))
            fragments = sorted(self.fragments, key=lambda part: cast("float", part["start_ms"]))
            transcript = json.dumps(fragments, ensure_ascii=False) if text else "[]"
            request = LoginHandoff(identifier, text, transcript, handoff_offset(raw.get("offset_ms")))
            if self.pending.full():
                raise RuntimeError("Live client delegation queue is full")
            if self.observer is not None:
                self.observer(request)
            self.pending.put_nowait(request)
            self.seen.add(identifier)

    async def run(self) -> None:
        try:
            await delegation_workers(self._work, self.concurrency, self._stop)
        finally:
            self.closed = True

    def _stop(self) -> None:
        self.closed = True

    async def _work(self) -> None:
        while True:
            request = await self.pending.get()
            if self.closed:
                return
            result = _CLARIFY
            if request.text:
                try:
                    async with asyncio.timeout(420):
                        result = await self.backend(request)
                except Exception:
                    result = "The backend could not complete this request."
            for part in result_chunks(result):
                if self.closed:
                    return
                await self.send({"delegation_id": request.identifier, "content": part})
