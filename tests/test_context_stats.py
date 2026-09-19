"""Deterministic context accounting for assembled requests and idle sessions."""

import asyncio
from pathlib import Path

from nagents import COMPONENT_LABELS
from nagents import Agent
from nagents import ContextStats
from nagents import SessionManager
from nagents import estimate_context_stats
from nagents import estimate_messages_tokens
from nagents import estimate_tool_tokens
from nagents.compaction import DEFAULT_CONTEXT_LIMIT
from nagents.context_stats import HISTORY_ASSISTANT_KEY
from nagents.context_stats import HISTORY_TOOL_KEY
from nagents.context_stats import HISTORY_USER_KEY
from nagents.context_stats import MEDIA_KEY
from nagents.context_stats import SKILLS_KEY
from nagents.context_stats import SYSTEM_PROMPT_KEY
from nagents.context_stats import TOOLS_KEY
from nagents.events import TokenUsage
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.types import ToolDefinition
from tests.test_extensions import OfflineProvider

SYSTEM_KEYS = [key for key, _ in COMPONENT_LABELS]


def tokens(stats: ContextStats, key: str) -> int:
    component = stats.component(key)
    assert component is not None
    return component.tokens


def test_empty_request_reports_stable_zero_components() -> None:
    stats = estimate_context_stats()

    assert stats.total_tokens == 0
    assert stats.context_window is None
    assert stats.remaining_tokens is None
    assert [component.key for component in stats.components] == SYSTEM_KEYS
    assert [component.label for component in stats.components] == [label for _, label in COMPONENT_LABELS]
    assert all(component.tokens == 0 for component in stats.components)


def test_system_only_populates_only_the_system_component() -> None:
    stats = estimate_context_stats(system_prompt="a" * 400)

    assert tokens(stats, SYSTEM_PROMPT_KEY) == 100
    assert stats.total_tokens == 100
    for key in (TOOLS_KEY, SKILLS_KEY, HISTORY_USER_KEY, MEDIA_KEY):
        assert tokens(stats, key) == 0


def test_tools_only_populates_only_the_tool_component() -> None:
    tools = [
        ToolDefinition(name="search", description="Find things", parameters={"type": "object"}),
        ToolDefinition(name="read", description="Read a file", parameters={"type": "object"}),
    ]
    stats = estimate_context_stats(tools=tools)

    assert tokens(stats, TOOLS_KEY) == estimate_tool_tokens(tools)
    assert tokens(stats, TOOLS_KEY) > 0
    assert stats.total_tokens == tokens(stats, TOOLS_KEY)
    assert tokens(stats, SYSTEM_PROMPT_KEY) == 0


def test_history_only_splits_user_assistant_and_tool() -> None:
    messages = [
        Message(role="user", content="u" * 40),
        Message(role="assistant", content="a" * 80),
        Message(role="tool", content="t" * 120, name="search", tool_call_id="call-1"),
    ]
    stats = estimate_context_stats(messages=messages)

    assert tokens(stats, HISTORY_USER_KEY) == 10 + 4
    assert tokens(stats, HISTORY_ASSISTANT_KEY) == 20 + 4
    assert tokens(stats, HISTORY_TOOL_KEY) > 20
    history_total = (
        tokens(stats, HISTORY_USER_KEY) + tokens(stats, HISTORY_ASSISTANT_KEY) + tokens(stats, HISTORY_TOOL_KEY)
    )
    assert history_total == estimate_messages_tokens(messages)
    assert stats.total_tokens == history_total


def test_media_is_counted_separately_from_history_text() -> None:
    image = ImageContent(base64_data="A" * 400, media_type="image/png")
    message = Message(role="user", content=[TextContent(text="look " * 20), image])
    stats = estimate_context_stats(messages=[message])

    assert tokens(stats, MEDIA_KEY) == 100
    assert tokens(stats, HISTORY_USER_KEY) == max(1, len("look " * 20) // 4) + 4
    assert stats.total_tokens == estimate_messages_tokens([message])


def test_skill_tool_results_are_reported_as_skills() -> None:
    message = Message(role="tool", content="s" * 80, name="skill", tool_call_id="call-1")
    stats = estimate_context_stats(messages=[message])

    assert tokens(stats, SKILLS_KEY) > 0
    assert tokens(stats, HISTORY_TOOL_KEY) == 0


def test_component_sums_equal_total_and_observed_window_is_reported() -> None:
    stats = estimate_context_stats(
        system_prompt="s" * 40,
        instructions="i" * 40,
        skill_manifest="k" * 40,
        messages=[Message(role="user", content="hello")],
        context_window=1000,
        provider="openai_compatible",
        model="example",
        observed=TokenUsage(prompt_tokens=321, completion_tokens=12, total_tokens=333),
    )

    assert stats.total_tokens == sum(component.tokens for component in stats.components)
    assert stats.remaining_tokens == 1000 - stats.total_tokens
    assert stats.observed_prompt_tokens == 321
    assert stats.observed_completion_tokens == 12
    assert stats.provider == "openai_compatible"
    assert stats.model == "example"


def test_agent_reports_persisted_history_without_running_the_provider(tmp_path: Path) -> None:
    async def drive() -> None:
        provider = OfflineProvider()
        agent = Agent(
            provider,
            SessionManager(tmp_path / "context.db"),
            tools=[lambda city: f"sunny in {city}"],
            system_prompt="You are concise.",
            compactor=None,
        )
        assert [event async for event in agent.run("what is the weather", session_id="s")]
        calls_before = len(provider.requests)

        stats = await agent.context_stats("s")

        assert len(provider.requests) == calls_before  # read-only: no new model request
        assert tokens(stats, SYSTEM_PROMPT_KEY) > 0
        assert tokens(stats, TOOLS_KEY) > 0
        assert tokens(stats, HISTORY_USER_KEY) > 0
        assert tokens(stats, HISTORY_ASSISTANT_KEY) > 0
        assert stats.context_window == DEFAULT_CONTEXT_LIMIT
        assert stats.observed_prompt_tokens is None  # the offline provider reports no usage
        assert stats.total_tokens == sum(component.tokens for component in stats.components)
        await agent.close()

    asyncio.run(drive())


def test_agent_context_stats_rejects_unknown_session(tmp_path: Path) -> None:
    async def drive() -> None:
        agent = Agent(OfflineProvider(), SessionManager(tmp_path / "missing.db"), compactor=None)
        try:
            await agent.context_stats("absent")
        except ValueError as error:
            assert "not found" in str(error)
        else:  # pragma: no cover - the assertion above is the contract
            raise AssertionError("Expected a missing-session error")
        await agent.close()

    asyncio.run(drive())
