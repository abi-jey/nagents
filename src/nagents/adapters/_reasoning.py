"""Display-summary request gates and Responses summary delta reconciliation."""

from __future__ import annotations

import re
from typing import cast

from ..events import ReasoningChunkEvent
from ._validation import ProtocolError
from ._validation import list_data
from ._validation import object_data
from ._validation import string_data


def openai_summary_model(model: str) -> bool:
    """Known reasoning families; unknown/non-reasoning models keep their request unchanged."""
    name = model.rsplit("/", 1)[-1].lower()
    return "-chat" not in name and bool(re.match(r"(?:o[134](?:-|$)|gpt-(?:5|6)(?:[.\-]|$))", name))


def gemini_summary_model(model: str) -> bool:
    return bool(re.match(r"gemini-(?:2\.5|3(?:\.\d+)?)(?:-|$)", model.rsplit("/", 1)[-1].lower()))


def anthropic_summary_model(model: str) -> bool:
    # Only models with adaptive thinking already on by default. Requesting
    # summaries must not enable reasoning or invent a budget on older models.
    return bool(
        re.fullmatch(
            r"claude-(?:(?:opus|sonnet|haiku|fable|mythos)-5(?:[.-]\d+)*(?:-\d{8})?|mythos-preview)",
            model.rsplit("/", 1)[-1].lower(),
        )
    )


class SummaryAccumulator:
    """Only emit new display text when a terminal summary repeats streamed deltas."""

    def __init__(self, *, streaming: bool) -> None:
        self.streaming = streaming
        self.texts: dict[tuple[int, int], str] = {}
        self.emitted: dict[tuple[int, int], str] = {}
        self.done: set[tuple[int, int]] = set()
        self.identities: dict[int, str] = {}
        self.limit = 65536

    def _key(self, event: dict[str, object]) -> tuple[int, int]:
        values = (event.get("output_index", 0), event.get("summary_index", 0))
        if any(type(value) is not int or not 0 <= value < 1024 for value in values):
            raise ProtocolError("Provider returned an invalid reasoning summary index.")
        if "item_id" in event:
            self._identity(cast("int", values[0]), event["item_id"])
        return cast("tuple[int, int]", values)

    def _identity(self, index: int, value: object) -> None:
        identity = string_data(value)
        if not identity or (index in self.identities and self.identities[index] != identity):
            raise ProtocolError("Provider returned inconsistent reasoning summary identities.")
        self.identities[index] = identity

    def _remember(self, key: tuple[int, int], text: str) -> None:
        if key not in self.texts and len(self.texts) >= 1024:
            raise ProtocolError("Provider returned too many reasoning summary parts.")
        previous = self.texts.get(key, "")
        available = self.limit - sum(len(value) for value in self.texts.values()) + len(previous)
        text = text[:available]
        # A later item/terminal snapshot can repeat an earlier prefix. Keep the
        # fuller text already observed instead of dropping a received body.
        if key in self.texts and previous.startswith(text):
            return
        if not text.startswith(previous):
            raise ProtocolError("Provider returned inconsistent reasoning summary text.")
        self.texts[key] = text

    def add(self, event: dict[str, object]) -> list[ReasoningChunkEvent]:
        key = self._key(event)
        if event.get("type") == "response.reasoning_summary_text.delta":
            if key in self.done:
                raise ProtocolError("Provider sent reasoning text after its summary finished.")
            text = self.texts.get(key, "") + string_data(event.get("delta"))
        elif event.get("type") in {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done"}:
            part = object_data(event.get("part"))
            if part.get("type") != "summary_text":
                return []
            text = string_data(part.get("text"))
            if event.get("type") == "response.reasoning_summary_part.done":
                self.done.add(key)
        else:
            text = string_data(event.get("text"))
            self.done.add(key)
        self._remember(key, text)
        return self._emit(key) if self.streaming else []

    def finish_item(self, index: int, item: dict[str, object]) -> None:
        self._key({"output_index": index})
        if "id" in item:
            self._identity(index, item["id"])
        parts = list_data(item.get("summary") or [])
        if len(parts) > 1024:
            raise ProtocolError("Provider returned too many reasoning summary parts.")
        for part_index, raw in enumerate(parts):
            part = object_data(raw)
            if part.get("type") != "summary_text":
                continue
            key = (index, part_index)
            text = string_data(part.get("text"))
            self._remember(key, text)

    def finish(self) -> list[ReasoningChunkEvent]:
        return [event for key in sorted(self.texts) for event in self._emit(key)]

    def _emit(self, key: tuple[int, int]) -> list[ReasoningChunkEvent]:
        text = self.texts[key]
        previous = self.emitted.get(key, "")
        if text == previous:
            return []
        self.emitted[key] = text
        return [ReasoningChunkEvent(chunk=text[len(previous) :], displayable=True, summary_part=key)]
