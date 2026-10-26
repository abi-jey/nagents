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
from tests.agent.test_extensions import OfflineProvider

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
    assert stats.observed_context_tokens == 333
    assert stats.context_tokens_source == "provider"
    assert stats.remaining_tokens == 1000 - 333
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
            compaction_strategy=None,
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
        agent = Agent(OfflineProvider(), SessionManager(tmp_path / "missing.db"), compaction_strategy=None)
        try:
            await agent.context_stats("absent")
        except ValueError as error:
            assert "not found" in str(error)
        else:  # pragma: no cover - the assertion above is the contract
            raise AssertionError("Expected a missing-session error")
        await agent.close()

    asyncio.run(drive())


def test_observed_usage_is_scoped_to_live_history_and_request_setup(tmp_path: Path) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import Usage

    async def drive() -> None:
        provider = OfflineProvider([[TextDoneEvent(text="answer", usage=Usage(248_779, 12, 248_791))]])
        provider.configure_context_window(1_000_000, {})
        agent = Agent(provider, SessionManager(tmp_path / "observed.db"), compaction_strategy=None)
        try:
            assert [event async for event in agent.run("hello", session_id="s")]
            stats = await agent.context_stats("s")
            assert stats.observed_prompt_tokens == 248_779
            assert stats.observed_context_tokens == 248_791
            assert stats.remaining_tokens == 1_000_000 - 248_791
            assert stats.total_tokens == sum(component.tokens for component in stats.components)
            assert stats.context_window_source == "configured"
            assert stats.as_dict()["compaction"] == {"trigger": "off"}
            # The measured response remains scoped to its generation, and the
            # newly appended user message is estimated once on top of it.
            await agent.session.add_message("s", Message(role="user", content="continue"))
            appended = await agent.context_stats("s")
            assert appended.observed_prompt_tokens == 248_779
            assert appended.observed_context_tokens == 248_791
            assert appended.pending_context_tokens > 0
            assert appended.context_tokens == appended.observed_context_tokens + appended.pending_context_tokens
            assert appended.context_tokens_source == "mixed"
            pending_after_user = appended.pending_context_tokens
            tool_result = Message(role="tool", name="lookup", tool_call_id="call-1", content="result" * 20)
            await agent.session.add_message("s", tool_result)
            after_tool = await agent.context_stats("s")
            assert after_tool.pending_context_tokens == pending_after_user + estimate_messages_tokens([tool_result])
            assert after_tool.observed_context_tokens == 248_791
            assert after_tool.context_tokens == after_tool.observed_context_tokens + after_tool.pending_context_tokens
            agent.system_prompt = "A different request setup"
            assert (await agent.context_stats("s")).observed_prompt_tokens is None
        finally:
            await agent.close()

    asyncio.run(drive())


def test_agent_context_stats_preserves_reported_reasoning_accounting(tmp_path: Path) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import Usage

    async def drive() -> None:
        provider = OfflineProvider(
            [
                [
                    TextDoneEvent(
                        text="answer",
                        usage=Usage(
                            prompt_tokens=400,
                            completion_tokens=120,
                            total_tokens=520,
                            reasoning_tokens=80,
                            reasoning_reported=True,
                            reasoning_in_output="included",
                            reported=True,
                            prompt_reported=True,
                            completion_reported=True,
                            total_reported=True,
                        ),
                    )
                ]
            ]
        )
        provider.configure_context_window(1_000, {})
        agent = Agent(provider, SessionManager(tmp_path / "reasoning.db"), compaction_strategy=None)
        try:
            assert [event async for event in agent.run("hello", session_id="s")]
            stats = await agent.context_stats("s")
            assert stats.observed_prompt_tokens == 400
            assert stats.observed_completion_tokens == 120
            assert stats.observed_reasoning_tokens == 80
            assert stats.observed_reasoning_in_output == "included"
            assert stats.observed_context_tokens == 520
            assert stats.context_tokens_source == "provider"
            assert stats.remaining_tokens == 480
        finally:
            await agent.close()

    asyncio.run(drive())


def test_agent_context_stats_preserves_reported_zero_prompt_usage(tmp_path: Path) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import Usage

    async def drive() -> None:
        provider = OfflineProvider(
            [
                [
                    TextDoneEvent(
                        text="answer",
                        usage=Usage(
                            reported=True,
                            prompt_reported=True,
                            completion_reported=True,
                            total_reported=True,
                        ),
                    )
                ]
            ]
        )
        provider.configure_context_window(1_000, {})
        agent = Agent(provider, SessionManager(tmp_path / "reported-zero.db"), compaction_strategy=None)
        try:
            assert [event async for event in agent.run("hello", session_id="s")]
            stats = await agent.context_stats("s")
            assert stats.observed_prompt_tokens == 0
            assert stats.observed_context_tokens == 0
            assert stats.context_tokens_source == "provider"
        finally:
            await agent.close()

    asyncio.run(drive())


def test_observed_usage_is_invalidated_by_same_text_reset_or_model_switch(tmp_path: Path) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import Usage

    async def drive() -> None:
        for change in ("reset", "replace", "model", "provider"):
            provider = OfflineProvider([[TextDoneEvent(text="answer", usage=Usage(321, 12, 333))]])
            agent = Agent(provider, SessionManager(tmp_path / f"{change}.db"), compaction_strategy=None)
            try:
                assert [event async for event in agent.run("same text", session_id="s")]
                history = await agent.session.get_history("s")
                if change == "reset":
                    await agent.session.clear_session("s")
                    for message in history:
                        await agent.session.add_message("s", message)
                elif change == "replace":
                    await agent.session.replace_context("s", history)
                elif change == "model":
                    provider.model = "another-model"
                else:
                    agent.provider = OfflineProvider()
                stats = await agent.context_stats("s")
                assert stats.observed_prompt_tokens is None
                assert stats.context_window is not None
                assert stats.remaining_tokens == stats.context_window - stats.total_tokens
            finally:
                await agent.close()

    asyncio.run(drive())


def test_compaction_and_generation_without_usage_discard_old_observation(tmp_path: Path) -> None:
    from nagents import ModelCompaction
    from nagents.events import TextDoneEvent
    from nagents.events import Usage

    async def drive() -> None:
        provider = OfflineProvider(
            [
                [TextDoneEvent(text="answer", usage=Usage(321, 12, 333))],
                [TextDoneEvent(text="new answer without usage")],
                [TextDoneEvent(text="summary")],
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "fresh.db"), compaction_strategy=None)
        try:
            assert [event async for event in agent.run("first", session_id="s")]
            assert (await agent.context_stats("s")).observed_prompt_tokens == 321
            assert [event async for event in agent.run("second", session_id="s")]
            assert (await agent.context_stats("s")).observed_prompt_tokens is None
            agent.compaction_strategy = ModelCompaction(token_budget=600_001)
            await agent.compact("s")
            stats = await agent.context_stats("s")
            assert stats.observed_prompt_tokens is None
            assert stats.compaction == {"trigger": "tokens", "token_budget": 600_001, "token_threshold": 420_000}
        finally:
            await agent.close()

    asyncio.run(drive())


def test_context_metadata_wait_rechecks_history_and_setup(tmp_path: Path) -> None:
    from unittest.mock import patch

    from nagents.events import TextDoneEvent
    from nagents.events import Usage
    from nagents.provider.context import ContextWindow

    async def drive() -> None:
        for change in ("instructions", "history"):
            provider = OfflineProvider([[TextDoneEvent(text="answer", usage=Usage(321, 12, 333))]])
            provider.configure_context_window(1_000_000, {})
            agent = Agent(provider, SessionManager(tmp_path / f"metadata-{change}.db"), compaction_strategy=None)
            entered, release = asyncio.Event(), asyncio.Event()

            async def blocked_window(
                current_provider: OfflineProvider = provider,
                entered_event: asyncio.Event = entered,
                release_event: asyncio.Event = release,
            ) -> ContextWindow:
                entered_event.set()
                await release_event.wait()
                return current_provider.cached_context_window()

            try:
                assert [event async for event in agent.run("original", session_id="s")]
                with patch.object(provider, "resolve_context_window", blocked_window):
                    pending = asyncio.create_task(agent.context_stats("s"))
                    await entered.wait()
                    if change == "instructions":
                        agent.system_prompt = "new setup" * 20
                    else:
                        await agent.session.replace_context("s", [Message(role="user", content="x" * 800)])
                    release.set()
                    stats = await pending
                assert stats.observed_prompt_tokens is None
                if change == "history":
                    assert tokens(stats, HISTORY_USER_KEY) == 204
                else:
                    assert tokens(stats, SYSTEM_PROMPT_KEY) == 45
            finally:
                await agent.close()

    asyncio.run(drive())


def test_completion_only_usage_does_not_claim_zero_prompt_context(tmp_path: Path) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import Usage

    async def drive() -> None:
        provider = OfflineProvider([[TextDoneEvent(text="answer", usage=Usage(completion_tokens=12, total_tokens=12))]])
        agent = Agent(provider, SessionManager(tmp_path / "output-only.db"), compaction_strategy=None)
        try:
            assert [event async for event in agent.run("hello", session_id="s")]
            stats = await agent.context_stats("s")
            assert stats.observed_prompt_tokens is None
            assert stats.total_tokens > 0
            assert stats.context_window is not None
            assert stats.remaining_tokens == stats.context_window - stats.total_tokens
        finally:
            await agent.close()

    asyncio.run(drive())


def test_deferred_auth_usage_is_bound_after_credentials_resolve(tmp_path: Path) -> None:
    import os
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock
    from unittest.mock import patch

    from nagents import ModelCompaction
    from nagents.events import Event
    from nagents.events import TextDoneEvent
    from nagents.events import Usage
    from nagents.harness.config import HarnessConfig
    from nagents.harness.providers.factory import HarnessProvider
    from nagents.harness.providers.registry import ProviderProfile
    from nagents.provider import Provider
    from nagents.types import GenerationConfig

    async def generate(
        self: Provider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        assert self.api_key == "resolved-test-key"
        yield TextDoneEvent(text="answer", usage=Usage(321, 12, 333))

    async def drive() -> None:
        for strategy in (None, ModelCompaction(token_budget=600_000)):
            profile = ProviderProfile(
                kind="openai", auth="api-key", api_key_env="NGN_CONTEXT_TEST_KEY", context_window=1_000_000
            )
            provider = HarnessProvider(HarnessConfig(workspace=tmp_path, providers={"": profile}, model="test-model"))
            agent = Agent(
                provider, SessionManager(tmp_path / f"deferred-{strategy is None}.db"), compaction_strategy=strategy
            )
            try:
                with (
                    patch.dict(os.environ, {"NGN_CONTEXT_TEST_KEY": "resolved-test-key"}),
                    patch.object(provider, "verify_model", AsyncMock(return_value=True)),
                    patch.object(Provider, "generate", generate),
                ):
                    assert [event async for event in agent.run("hello", session_id="s")]
                    assert (await agent.context_stats("s")).observed_prompt_tokens == 321
            finally:
                await agent.close()

    asyncio.run(drive())
