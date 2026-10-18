"""Bounded display metadata for one provider generation, independent of prompts."""

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace

from .events import Event
from .events import ReasoningChunkEvent
from .events import ResponseMetadataEvent
from .events import TextChunkEvent
from .events import TextDoneEvent
from .events import ToolCallEvent
from .events import ToolCallProgressEvent
from .events import Usage

SUMMARY_LIMIT = 16384


@dataclass
class ResponseTrace:
    response_id: str
    session_id: str
    round_number: int
    revision: int = 0
    summary: str = ""
    summary_truncated: bool = False
    status: str = "responding"
    usage: Usage = field(default_factory=Usage)
    usage_reported: bool = False
    dirty: bool = False
    _summary_parts: dict[tuple[int, int], str] = field(default_factory=dict, init=False, repr=False)

    def _summary(self, event: ReasoningChunkEvent) -> tuple[str, bool]:
        if not event.chunk:
            return self.summary, self.summary_truncated
        # The provider's displayable parts may interleave or get their remaining
        # body only at item completion. Rebuild presentation order without moving
        # this response's identity or exposing opaque protocol reasoning.
        key = event.summary_part if len(event.summary_part) == 2 else (-1, -1)
        if not self._summary_parts and self.summary:
            self._summary_parts[(-1, -1)] = self.summary
        if key not in self._summary_parts and len(self._summary_parts) >= 1024:
            return self.summary, True
        previous = self._summary_parts.get(key, "")
        truncated = self.summary_truncated or len(previous) + len(event.chunk) > SUMMARY_LIMIT
        self._summary_parts[key] = (previous + event.chunk[:SUMMARY_LIMIT])[:SUMMARY_LIMIT]
        remaining = SUMMARY_LIMIT
        parts: list[str] = []
        for index in sorted(self._summary_parts):
            text = self._summary_parts[index]
            if not text:
                continue
            separator = "\n\n" if parts else ""
            kept = text[: max(0, remaining - len(separator))]
            truncated |= len(kept) < len(text)
            self._summary_parts[index] = kept
            if kept:
                parts.append(separator + kept)
                remaining -= len(separator) + len(kept)
        return "".join(parts), truncated

    def observe(self, event: Event) -> None:
        if isinstance(
            event, (TextChunkEvent, TextDoneEvent, ReasoningChunkEvent, ToolCallEvent, ToolCallProgressEvent)
        ):
            event.extra = {**event.extra, "response_id": self.response_id, "response_session_id": self.session_id}
        if isinstance(event, ReasoningChunkEvent) and event.displayable:
            summary, truncated = self._summary(event)
            self.dirty |= (summary, truncated, "thinking") != (self.summary, self.summary_truncated, self.status)
            self.summary, self.summary_truncated, self.status = summary, truncated, "thinking"
        elif isinstance(event, (TextChunkEvent, TextDoneEvent)):
            self.dirty |= self.status != "responding"
            self.status = "responding"
        if event.usage.has_usage():
            current = replace(event.usage, session=None)
            self.dirty |= current != self.usage or not self.usage_reported
            self.usage, self.usage_reported = current, True

    def snapshot(self, *, terminal: bool = False, failed: bool = False, history_id: int = 0) -> ResponseMetadataEvent:
        self.revision += 1
        self.dirty = False
        return ResponseMetadataEvent(
            response_id=self.response_id,
            response_session_id=self.session_id,
            round_number=self.round_number,
            revision=self.revision,
            summary=self.summary,
            summary_truncated=self.summary_truncated,
            status="failed"
            if failed
            else "completed"
            if terminal
            else "thinking"
            if self.status == "thinking"
            else "responding",
            usage=replace(self.usage),
            usage_reported=self.usage_reported,
            history_id=str(history_id) if history_id else "",
        )


ResponseObserver = Callable[[object, ResponseMetadataEvent], None]
_observer: ContextVar[ResponseObserver | None] = ContextVar("ngn_response_observer", default=None)


@contextmanager
def observe_responses(observer: ResponseObserver) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def capture_response(adapter: object, event: Event) -> None:
    observer = _observer.get()
    if observer is not None and isinstance(event, ResponseMetadataEvent):
        observer(adapter, event)
