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

    def observe(self, event: Event) -> None:
        if isinstance(
            event, (TextChunkEvent, TextDoneEvent, ReasoningChunkEvent, ToolCallEvent, ToolCallProgressEvent)
        ):
            event.extra = {**event.extra, "response_id": self.response_id, "response_session_id": self.session_id}
        if isinstance(event, ReasoningChunkEvent) and event.displayable:
            truncated = self.summary_truncated or len(self.summary) + len(event.chunk) > SUMMARY_LIMIT
            summary = (self.summary + event.chunk)[:SUMMARY_LIMIT]
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
