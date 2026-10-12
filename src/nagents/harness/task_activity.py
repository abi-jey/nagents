"""Bounded public evidence and accepted runtime configuration for child tasks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite

from nagents.compaction import ModelCompaction
from nagents.compaction import describe_compaction
from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.events import ToolExecutionStartedEvent
from nagents.events import ToolResultEvent
from nagents.extensions import AgentPlugin

from .types import Notice
from .types import ToolOutput

if TYPE_CHECKING:
    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext

    from .runtime import Harness
    from .subagents import SubagentManager
    from .subagents import TaskInfo
    from .types import HarnessEvent

MAX_ACTIVITY_TEXT = 32768


@dataclass
class ChildHistoryCommitted(Notice):
    """Internal ordered marker consumed by the owning task stream only."""

    text: str = ""
    history_id: int = 0


def bounded(value: object) -> object:
    """Copy JSON evidence without retaining opaque provider objects or large bodies."""
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "[Unsupported activity value]"
    if len(encoded) > MAX_ACTIVITY_TEXT:
        return {"text": encoded[:MAX_ACTIVITY_TEXT], "truncated": True}
    result: object = json.loads(encoded)
    return result


def public_payload(event: HarnessEvent) -> dict[str, object]:
    """An allowlist: no reasoning, diagnostics, provider extras, or root Done events."""
    if isinstance(event, ChildHistoryCommitted):
        return {"type": "history_committed", "history_id": event.history_id}
    if isinstance(event, TextChunkEvent):
        return {"type": "text_chunk", "chunk": event.chunk[:MAX_ACTIVITY_TEXT]}
    if isinstance(event, TextDoneEvent):
        return {
            "type": "text_done",
            "text": event.text[:MAX_ACTIVITY_TEXT],
            "truncated": len(event.text) > MAX_ACTIVITY_TEXT,
        }
    if isinstance(event, ToolCallProgressEvent):
        return {
            "type": "tool_call_progress",
            "id": event.id,
            "name": event.name,
            "generation_id": event.generation_id,
            "index": event.index,
            "arguments_text": event.arguments_text[:MAX_ACTIVITY_TEXT],
            "arguments_truncated": event.arguments_truncated or len(event.arguments_text) > MAX_ACTIVITY_TEXT,
            "status": event.status,
        }
    if isinstance(event, ToolCallEvent):
        return {
            "type": "tool_call",
            "id": event.id,
            "name": event.name,
            "arguments": bounded(event.arguments),
            **_tool_identity(event),
        }
    if isinstance(event, ToolExecutionStartedEvent):
        return {"type": "tool_execution_started", "id": event.id, "name": event.name, **_tool_identity(event)}
    if isinstance(event, ToolOutput):
        return {
            "type": "tool_output",
            "id": event.call_id,
            "name": event.tool,
            "text": event.text[:MAX_ACTIVITY_TEXT],
            **_tool_identity(event),
        }
    if isinstance(event, ToolResultEvent):
        return {
            "type": "tool_result",
            "id": event.id,
            "name": event.name,
            "result": bounded(event.result),
            "error": event.error[:MAX_ACTIVITY_TEXT] if event.error else "",
            "duration_ms": event.duration_ms,
            **_tool_identity(event),
        }
    if isinstance(event, (CompactionStartedEvent, CompactionDoneEvent)):
        return {"type": "status", "phase": "compacting" if isinstance(event, CompactionStartedEvent) else "running"}
    if isinstance(event, ErrorEvent):
        return {
            "type": "status",
            "phase": "running" if event.recoverable else "failed",
            "detail": "Provider or tool processing encountered an error.",
        }
    return {}


def _tool_identity(
    event: ToolCallEvent | ToolExecutionStartedEvent | ToolOutput | ToolResultEvent,
) -> dict[str, object]:
    """Only bounded invocation correlation may cross from event extras."""
    generation, index = event.extra.get("generation_id"), event.extra.get("index")
    if isinstance(generation, str) and 0 < len(generation) <= 200 and type(index) is int and 0 <= index < 1024:
        return {"generation_id": generation, "index": index}
    return {}


def compaction_snapshot(harness: Harness) -> dict[str, object]:
    return dict(describe_compaction(harness.agent.compaction_strategy, harness.agent.provider))


@dataclass(frozen=True)
class TaskOptions:
    provider: str = ""
    model: str = ""
    compact_trigger: str = ""
    compact_tokens: int = 200000
    compact_messages: int = 100

    def validate(self, harness: Harness) -> None:
        if not isinstance(self.provider, str) or (self.provider and self.provider not in harness.providers.providers):
            raise ValueError("Delegation provider must name a configured provider connection")
        if (
            not isinstance(self.model, str)
            or len(self.model) > 200
            or (self.model and (not self.model.strip() or not self.model.isprintable()))
        ):
            raise ValueError("Delegation model must be a bounded model identifier")
        if self.compact_trigger not in {"", "auto", "tokens", "messages", "off"}:
            raise ValueError("Unknown delegation compaction trigger")
        if type(self.compact_tokens) is not int or not 1024 <= self.compact_tokens <= 10000000:
            raise ValueError("Delegation context budget must be between 1024 and 10000000 tokens")
        if type(self.compact_messages) is not int or not 1 <= self.compact_messages <= 10000:
            raise ValueError("Delegation message limit must be between 1 and 10000")

    def apply_compaction(self, child: Harness, parent: Harness) -> None:
        if not self.compact_trigger:
            # Strategies may own provider clients; inherit the accepted strategy,
            # never clone or reconfigure its client as part of an inspection.
            child.agent.compaction_strategy = parent.agent.compaction_strategy
        elif self.compact_trigger == "off":
            child.agent.compaction_strategy = None
        elif self.compact_trigger == "tokens":
            child.agent.compaction_strategy = ModelCompaction(token_budget=self.compact_tokens)
        elif self.compact_trigger == "messages":
            child.agent.compaction_strategy = ModelCompaction(message_threshold=self.compact_messages)
        else:
            child.agent.compaction_strategy = ModelCompaction()


class ChildActivity(AgentPlugin):
    """Publish a committed human input only when the child enters model processing."""

    def __init__(self, manager: SubagentManager, info: TaskInfo, child: Harness) -> None:
        self.manager, self.info, self.child = manager, info, child
        self.seen_turns: set[str] = set()

    async def _history_committed(self) -> None:
        # Capture this watermark in the child's producer, before later writes
        # can race a delayed root observer. Viewers can combine only this saved
        # prefix with the activity that follows its ordered marker.
        async with aiosqlite.connect(self.child.agent.session.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM v2_messages WHERE session_id = ?",
                (self.info.child_session_id,),
            )
            row = await cursor.fetchone()
        await self.child.emit(ChildHistoryCommitted(history_id=int(row[0]) if row else 0))

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        await self._history_committed()
        turn = self.child.followups.current
        if turn.id not in self.seen_turns:
            self.seen_turns.add(turn.id)
            await self.manager._input_started(self.info, self.child, turn)
        self.info.context = (await self.child.agent.context_stats(self.info.child_session_id)).as_dict()
        self.info.compaction = compaction_snapshot(self.child)
        await self.manager._emit_activity(
            self.info,
            {"type": "status", "phase": "running", "context": self.info.context, "compaction": self.info.compaction},
        )
        return request

    async def after_run(self, context: RunContext) -> None:
        current = asyncio.current_task()
        if current is not None and not current.cancelling():
            await self._history_committed()
            self.info.context = (await self.child.agent.context_stats(self.info.child_session_id)).as_dict()
            self.info.compaction = compaction_snapshot(self.child)
            await self.manager._emit_activity(
                self.info,
                {
                    "type": "status",
                    "phase": self.info.phase,
                    "context": self.info.context,
                    "compaction": self.info.compaction,
                },
            )
