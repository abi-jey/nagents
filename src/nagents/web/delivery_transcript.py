"""Serialize exact streamed-call identities without leaking private event objects."""

from nagents.cli import _event_record
from nagents.events import ToolCallEvent
from nagents.harness.types import HarnessEvent
from nagents.harness.types import TranscriptAbandoned
from nagents.harness.types import TranscriptAnchor


class DeliveryTranscript:
    def __init__(self, *, capture: bool = True) -> None:
        self.capture = capture
        self.pending: dict[int, tuple[ToolCallEvent, str]] = {}
        self.serial = 0

    def record(self, event: HarnessEvent) -> dict[str, object]:
        if isinstance(event, (TranscriptAnchor, TranscriptAbandoned)):
            calls: list[dict[str, object]] = []
            for position, call in enumerate(event.calls):
                previous = self.pending.pop(id(call), None)
                if previous is None or previous[0] is not call:
                    raise RuntimeError("Missing committed web transcript call")
                calls.append({"event_id": previous[1], "call_position": position})
            return {
                "event": "transcript_anchor" if isinstance(event, TranscriptAnchor) else "transcript_abandoned",
                "history_id": str(event.message_id) if isinstance(event, TranscriptAnchor) else "",
                "calls": calls,
            }
        record = _event_record(event)
        if self.capture and isinstance(event, ToolCallEvent) and not event.extra.get("task_id"):
            self.serial += 1
            identity = str(self.serial)
            self.pending[id(event)] = (event, identity)
            record["transcript_event_id"] = identity
        return record
