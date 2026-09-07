"""A trusted Python extension that changes the harness, not the terminal UI.

This deliberately lossy, no-LLM compactor demonstrates algorithm replacement.
Use a summarizing or retrieval-based strategy when old task details must survive.
Load explicitly with: ngn --plugin examples/harness/custom_behavior.py:setup
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from nagents import AgentPlugin
from nagents import CompactionResult
from nagents import Message

if TYPE_CHECKING:
    from nagents import CompactionRequest
    from nagents import ModelRequest
    from nagents import RunContext
    from nagents.harness import Harness


class RecentTurns:
    """Keep four complete user turns after ten accumulate, without an LLM call."""

    async def should_compact(self, request: CompactionRequest) -> bool:
        return sum(message.role == "user" for message in request.messages) >= 10

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        starts = [index for index, message in enumerate(request.messages) if message.role == "user"]
        # Cutting at a user-turn boundary keeps assistant tool calls and their
        # results together. The full original transcript remains in SQLite.
        start = starts[-4] if len(starts) >= 4 else 0
        return CompactionResult(
            messages=request.messages[start:],
            summary="Retained the latest four complete user turns. Earlier details were not summarized.",
        )


class VerificationContext(AgentPlugin):
    """Add request-local instructions without appending them to the transcript."""

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        instruction = Message(
            role="system",
            content="In the final response, distinguish validations actually run from those only recommended.",
        )
        return replace(request, messages=[instruction, *request.messages])


def utc_time() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()


def setup(harness: Harness) -> AgentPlugin:
    harness.agent.compaction_strategy = RecentTurns()
    harness.agent.tool_registry.register(utc_time)
    return VerificationContext()
