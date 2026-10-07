"""Bounded, non-executable tool previews shared by streaming protocols."""

import json
import uuid
from dataclasses import replace

from ..events import ToolCallEvent
from ..events import ToolCallProgressEvent
from ._validation import ProtocolError

ARGUMENT_PREVIEW_LIMIT = 16_384
IDENTITY_PREVIEW_LIMIT = 512


class ToolCallProgressTracker:
    """Keep only the last bounded snapshot for each provider output index."""

    def __init__(self) -> None:
        self.generation_id = uuid.uuid4().hex
        self._snapshots: dict[int, ToolCallProgressEvent] = {}

    def preview(self, index: int, call_id: str, name: str, arguments_text: str) -> list[ToolCallProgressEvent]:
        if type(index) is not int or not 0 <= index < 1024:
            raise ValueError("Tool preview index is out of bounds")
        if arguments_text.lstrip() and not arguments_text.lstrip().startswith("{"):
            raise ProtocolError("Provider returned non-object tool arguments; no tools were released.")
        snapshot = ToolCallProgressEvent(
            generation_id=self.generation_id,
            index=index,
            id=call_id[:IDENTITY_PREVIEW_LIMIT],
            name=name[:IDENTITY_PREVIEW_LIMIT],
            arguments_text=arguments_text[:ARGUMENT_PREVIEW_LIMIT],
            arguments_truncated=len(arguments_text) > ARGUMENT_PREVIEW_LIMIT,
        )
        previous = self._snapshots.get(index)
        if previous is not None and (
            previous.id,
            previous.name,
            previous.arguments_text,
            previous.arguments_truncated,
        ) == (snapshot.id, snapshot.name, snapshot.arguments_text, snapshot.arguments_truncated):
            return []
        self._snapshots[index] = snapshot
        return [replace(snapshot)]

    def ready(self, index: int, call: ToolCallEvent) -> ToolCallProgressEvent:
        self.preview(index, call.id, call.name, json.dumps(call.arguments, ensure_ascii=False, allow_nan=False))
        call.extra = {**call.extra, "generation_id": self.generation_id, "index": index}
        snapshot = replace(self._snapshots[index], status="ready")
        self._snapshots[index] = snapshot
        return replace(snapshot)

    def abandon(self) -> list[ToolCallProgressEvent]:
        abandoned = [replace(snapshot, status="abandoned") for snapshot in self._snapshots.values()]
        self._snapshots.clear()
        return abandoned
