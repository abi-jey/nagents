"""Trusted Python extensions for the Agent text loop.

Plugins run in registration order, including notification and cleanup hooks.
They have full Agent access, not a sandbox. Hook exceptions abort the run;
tool authorization belongs in the executor, after before_tool transformations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol

from .types import Message

if TYPE_CHECKING:
    from .agent import Agent
    from .events import Event
    from .events import ToolResultEvent
    from .provider import Provider
    from .types import GenerationConfig
    from .types import ToolCall
    from .types import ToolDefinition


@dataclass
class RunContext:
    agent: Agent
    session_id: str
    user_id: str
    round_number: int = 0


@dataclass
class ModelRequest:
    """Ephemeral model input; mutations do not rewrite session history."""

    messages: list[Message]
    tools: list[ToolDefinition]
    config: GenerationConfig | None


class AgentPlugin:
    """Override any hooks needed. round_number is zero-based.

    The chain is snapshotted at run entry. on_event receives an observational
    copy before delivery to the caller. after_run runs even on failure/aclose;
    cleanup failures are collected so every plugin gets a cleanup opportunity.
    before_tool/after_tool must preserve the call's id and name.
    """

    async def before_run(self, context: RunContext, message: Message) -> Message:
        return message

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        return request

    async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
        return call

    async def after_tool(self, context: RunContext, result: ToolResultEvent) -> ToolResultEvent:
        return result

    async def on_event(self, context: RunContext, event: Event) -> None:
        pass

    async def after_run(self, context: RunContext) -> None:
        pass


@dataclass
class CompactionRequest:
    """Active persisted history only, excluding Agent.system_prompt.

    Messages are detached copies. A forced request bypasses should_compact.
    The strategy may use arbitrary Python and need not call a model.
    """

    messages: list[Message]
    session_id: str
    provider: Provider
    estimated_tokens: int
    force: bool = False


@dataclass
class CompactionResult:
    messages: list[Message]
    summary: str = ""


class CompactionStrategy(Protocol):
    async def should_compact(self, request: CompactionRequest) -> bool: ...

    async def compact(self, request: CompactionRequest) -> CompactionResult: ...


def _validate_context(messages: list[Message]) -> None:
    """Reject replacements that would leave invalid tool conversations."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("Replacement context must be a nonempty list of Message objects")
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, Message):
            raise ValueError("Replacement context must contain Message objects")
        if message.role not in {"system", "developer", "user", "assistant", "tool", "compaction_summary"}:
            raise ValueError(f"Invalid message role: {message.role}")
        if message.role == "tool":
            call_id = message.tool_call_id
            if message.tool_calls or not call_id or call_id not in pending:
                raise ValueError("Tool result has no matching pending tool call")
            if message.name is not None and message.name != pending[call_id]:
                raise ValueError("Tool result name does not match its call")
            del pending[call_id]
        else:
            if pending:
                raise ValueError("Unresolved tool calls before the next message")
            if message.tool_call_id is not None:
                raise ValueError("Only tool results may have tool_call_id")
            if message.tool_calls and message.role != "assistant":
                raise ValueError("Only assistant messages may contain tool calls")
            for call in message.tool_calls:
                if not call.id or not call.name or call.id in seen:
                    raise ValueError("Tool calls require unique nonempty IDs and nonempty names")
                seen.add(call.id)
                pending[call.id] = call.name
    if pending:
        raise ValueError("Replacement context contains unresolved tool calls")
