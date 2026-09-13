"""Bounded, ordered active-run evidence for cold-reload reconstruction."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field

from nagents.cli import _json_default

MAX_RUN_EVENTS = 2048
MAX_RUN_BYTES = 2 * 1024 * 1024


@dataclass
class RunReplay:
    records: list[dict[str, object]] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    bytes: int = 0
    truncated: bool = False

    @staticmethod
    def scope(record: dict[str, object]) -> tuple[object, ...]:
        extra = record.get("extra", {})
        extra = extra if isinstance(extra, dict) else {}
        return (
            *(
                record.get(key, extra.get(key, ""))
                for key in (
                    "event",
                    "run_id",
                    "session_id",
                    "task_id",
                    "activation",
                    "followup",
                    "stream",
                )
            ),
            record.get("call_id", record.get("id", "")),
            record.get("tool", record.get("name", "")),
        )

    def append(self, record: dict[str, object]) -> None:
        if self.truncated:
            return
        # Normalize/detach the same wire values as the WS bus. Only adjacent
        # deltas in the exact same scope can be coalesced without reordering.
        copied: dict[str, object] = json.loads(json.dumps(record, default=_json_default, ensure_ascii=True))
        event = copied.get("event")
        delta = "text" if event == "tool_output" else "chunk" if event in {"text_chunk", "reasoning_chunk"} else ""
        merge = bool(delta and self.records and self.scope(self.records[-1]) == self.scope(copied))
        if merge:
            copied[delta] = str(self.records[-1].get(delta, "")) + str(copied.get(delta, ""))
            self.bytes -= self.sizes.pop()
            self.records.pop()
        size = len(json.dumps(copied, ensure_ascii=True))
        if self.bytes + size > MAX_RUN_BYTES or len(self.records) >= MAX_RUN_EVENTS:
            # A suffix is not a full replay: replacing a client's complete live
            # evidence with it would erase earlier task/tool records. Fall back
            # to persisted history + the independently bounded current drafts.
            self.truncated = True
            self.records.clear()
            self.sizes.clear()
            self.bytes = 0
            return
        self.records.append(copied)
        self.sizes.append(size)
        self.bytes += size

    def snapshot(self) -> dict[str, object]:
        return {"events_truncated": True} if self.truncated else {"events": deepcopy(self.records)}
