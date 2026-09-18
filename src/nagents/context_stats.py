"""Estimated token accounting for the model request a session would send.

The numbers reported here are estimates from the same character/byte heuristics
the compaction module uses (roughly four characters per token for text and a
size-based approximation for base64 media). They are useful for budgeting and for
spotting which part of a request dominates, but they are not provider tokenizer
counts and will not match billed usage exactly.

The breakdown partitions the estimate so the components always sum to the
reported total. Each component has a stable key and a human-readable label so
clients can render and test them consistently.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .compaction import CHARS_PER_TOKEN
from .compaction import estimate_tokens
from .types import ContentPart
from .types import Message
from .types import TextContent
from .types import ToolDefinition

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .events import TokenUsage

SYSTEM_PROMPT_KEY = "system_prompt"
INSTRUCTIONS_KEY = "instructions"
TOOLS_KEY = "tools"
SKILLS_KEY = "skills"
HISTORY_USER_KEY = "history_user"
HISTORY_ASSISTANT_KEY = "history_assistant"
HISTORY_TOOL_KEY = "history_tool"
HISTORY_OTHER_KEY = "history_other"
MEDIA_KEY = "media"

ESTIMATE_METHOD = "chars/4 heuristic (UTF-8 text); base64 media approximated by size"

# The order here is the order clients should render. Keys and labels are stable.
COMPONENT_LABELS: tuple[tuple[str, str], ...] = (
    (SYSTEM_PROMPT_KEY, "System prompt"),
    (INSTRUCTIONS_KEY, "Request instructions"),
    (TOOLS_KEY, "Tool definitions"),
    (SKILLS_KEY, "Skills"),
    (HISTORY_USER_KEY, "Conversation history (user)"),
    (HISTORY_ASSISTANT_KEY, "Conversation history (assistant)"),
    (HISTORY_TOOL_KEY, "Conversation history (tool)"),
    (HISTORY_OTHER_KEY, "Conversation history (other)"),
    (MEDIA_KEY, "Media and attachments"),
)


@dataclass(frozen=True)
class ContextComponent:
    """One labeled part of the estimated request context."""

    key: str
    label: str
    tokens: int


@dataclass(frozen=True)
class ContextStats:
    """Estimated breakdown of what a request carries.

    Attributes:
        components: Stable, ordered components that sum to ``total_tokens``.
        total_tokens: Estimated input tokens for the assembled request.
        context_window: Provider/model context window when known, else ``None``.
        remaining_tokens: ``context_window - total_tokens`` when the window is
            known, else ``None``. It can be negative if the estimate exceeds it.
        observed_prompt_tokens: Last provider-reported input tokens, when the
            agent has seen a real usage event for the session.
        observed_completion_tokens: Last provider-reported output tokens.
        provider: Provider identifier the estimate was computed for.
        model: Model identifier the estimate was computed for.
        estimate_method: Human-readable note describing the heuristic.
    """

    components: tuple[ContextComponent, ...]
    total_tokens: int
    context_window: int | None = None
    remaining_tokens: int | None = None
    observed_prompt_tokens: int | None = None
    observed_completion_tokens: int | None = None
    provider: str = ""
    model: str = ""
    estimate_method: str = ESTIMATE_METHOD

    def component(self, key: str) -> ContextComponent | None:
        """Return the component with ``key``, or ``None`` when it is absent."""
        for component in self.components:
            if component.key == key:
                return component
        return None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable mapping for HTTP or logging surfaces."""
        return {
            "components": [asdict(component) for component in self.components],
            "total_tokens": self.total_tokens,
            "context_window": self.context_window,
            "remaining_tokens": self.remaining_tokens,
            "observed_prompt_tokens": self.observed_prompt_tokens,
            "observed_completion_tokens": self.observed_completion_tokens,
            "provider": self.provider,
            "model": self.model,
            "estimate_method": self.estimate_method,
        }


def _content_tokens(content: str | list[ContentPart] | None) -> tuple[int, int]:
    """Split content into (text tokens, media tokens), mirroring ``estimate_tokens``."""
    if content is None:
        return 0, 0
    if isinstance(content, str):
        if not content:
            return 0, 0
        return max(1, len(content) // CHARS_PER_TOKEN), 0
    text = 0
    media = 0
    for part in content:
        if isinstance(part, TextContent):
            text += max(1, len(part.text) // CHARS_PER_TOKEN)
        else:
            # base64 is ~4/3 of binary; tokens are roughly chars/4. This matches
            # compaction.estimate_tokens so the split still sums to the same value.
            media += max(100, len(part.base64_data) // (CHARS_PER_TOKEN * 4)) if part.base64_data else 10
    return text, media


def _message_overhead(message: Message) -> int:
    """Per-message overhead that ``estimate_messages_tokens`` includes."""
    total = 4
    for call in message.tool_calls:
        total += len(call.name) // CHARS_PER_TOKEN + 4
        if call.arguments:
            total += len(json.dumps(call.arguments)) // CHARS_PER_TOKEN
        if call.metadata:
            total += len(str(call.metadata)) // CHARS_PER_TOKEN
    if message.tool_call_id:
        total += len(message.tool_call_id) // CHARS_PER_TOKEN
    if message.name:
        total += len(message.name) // CHARS_PER_TOKEN
    return total


def _history_tokens(messages: Sequence[Message]) -> dict[str, int]:
    """Bucket history tokens by role, with media and skill tool results separate."""
    buckets = {
        HISTORY_USER_KEY: 0,
        HISTORY_ASSISTANT_KEY: 0,
        HISTORY_TOOL_KEY: 0,
        HISTORY_OTHER_KEY: 0,
        SKILLS_KEY: 0,
        MEDIA_KEY: 0,
    }
    for message in messages:
        text, media = _content_tokens(message.content)
        overhead = _message_overhead(message)
        if message.role == "tool" and message.name == "skill":
            # Loaded skill bodies returned through the skill(name) tool.
            buckets[SKILLS_KEY] += text + overhead
        elif message.role == "user":
            buckets[HISTORY_USER_KEY] += text + overhead
        elif message.role == "assistant":
            buckets[HISTORY_ASSISTANT_KEY] += text + overhead
        elif message.role == "tool":
            buckets[HISTORY_TOOL_KEY] += text + overhead
        else:
            buckets[HISTORY_OTHER_KEY] += text + overhead
        buckets[MEDIA_KEY] += media
    return buckets


def estimate_tool_tokens(tools: Sequence[ToolDefinition]) -> int:
    """Estimate tokens for the JSON tool schemas sent to the provider.

    The neutral ``name``/``description``/``parameters`` payload is used for every
    provider. Provider-specific wrapper keys add a constant, negligible overhead.
    """
    total = 0
    for tool in tools:
        schema = json.dumps(
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
            ensure_ascii=False,
        )
        total += estimate_tokens(schema)
    return total


def estimate_context_stats(
    *,
    system_prompt: str = "",
    instructions: str = "",
    tools: Sequence[ToolDefinition] = (),
    skill_manifest: str = "",
    skill_text: str = "",
    messages: Sequence[Message] = (),
    context_window: int | None = None,
    provider: str = "",
    model: str = "",
    observed: TokenUsage | None = None,
) -> ContextStats:
    """Estimate a request breakdown from its assembled inputs.

    Callers pass the same values they would send to the provider: the system
    prompt, request-local instructions, tool schemas, rendered skill text (the
    catalog manifest and any loaded bodies), and the conversation history. Skill
    tool results in ``messages`` are counted under ``skills`` instead of history.

    Args:
        system_prompt: The agent's base system prompt.
        instructions: Request-local instructions separable from the system prompt.
        tools: Tool definitions whose JSON schemas are sent to the provider.
        skill_manifest: Rendered available-skills catalog text.
        skill_text: Loaded skill body text included in the request.
        messages: Conversation history, excluding the base system prompt.
        context_window: Known model context window, or ``None`` when unknown.
        provider: Provider identifier for reporting.
        model: Model identifier for reporting.
        observed: Last real provider usage for comparison with the estimate.
    """
    values = {key: 0 for key, _ in COMPONENT_LABELS}
    values[SYSTEM_PROMPT_KEY] = estimate_tokens(system_prompt)
    values[INSTRUCTIONS_KEY] = estimate_tokens(instructions)
    values[TOOLS_KEY] = estimate_tool_tokens(tools)
    values[SKILLS_KEY] = estimate_tokens(skill_manifest) + estimate_tokens(skill_text)
    for key, tokens in _history_tokens(messages).items():
        values[key] += tokens

    components = tuple(ContextComponent(key, label, values[key]) for key, label in COMPONENT_LABELS)
    total = sum(component.tokens for component in components)
    return ContextStats(
        components=components,
        total_tokens=total,
        context_window=context_window,
        remaining_tokens=context_window - total if context_window is not None else None,
        observed_prompt_tokens=observed.prompt_tokens if observed is not None else None,
        observed_completion_tokens=observed.completion_tokens if observed is not None else None,
        provider=provider,
        model=model,
    )
