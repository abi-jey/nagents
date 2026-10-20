"""Provider-owned context accounting uses normalized, synthetic usage only."""

import asyncio

from nagents import ModelCompaction
from nagents.events import TokenUsage
from nagents.extensions import CompactionRequest
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message


def provider(kind: ProviderType = ProviderType.OPENAI_COMPATIBLE) -> Provider:
    result = Provider(kind, "offline-test-key", "offline-model")
    result.configure_context_window(1_000, {})
    return result


def test_openai_reasoning_included_in_completion_is_not_added_twice() -> None:
    stats = provider().context_stats(
        observed=TokenUsage(
            prompt_tokens=400,
            completion_tokens=120,
            total_tokens=520,
            prompt_reported=True,
            completion_reported=True,
            total_reported=True,
            reasoning_tokens=80,
            reasoning_reported=True,
            reasoning_in_output="included",
        )
    )

    assert stats.observed_reasoning_tokens == 80
    assert stats.observed_reasoning_in_output == "included"
    assert stats.observed_context_tokens == 520
    assert stats.context_tokens == 520
    assert stats.remaining_tokens == 480
    assert stats.context_tokens_source == "provider"


def test_separate_reasoning_is_counted_in_addition_to_completion() -> None:
    stats = provider(ProviderType.GEMINI_NATIVE).context_stats(
        observed=TokenUsage(
            prompt_tokens=400,
            completion_tokens=40,
            total_tokens=520,
            prompt_reported=True,
            completion_reported=True,
            total_reported=True,
            reasoning_tokens=80,
            reasoning_reported=True,
            reasoning_in_output="separate",
        )
    )

    assert stats.observed_context_tokens == 520
    assert stats.remaining_tokens == 480
    assert stats.context_tokens_source == "provider"


def test_reported_total_covers_unknown_reasoning_inclusion_without_double_add() -> None:
    stats = provider().context_stats(
        observed=TokenUsage(
            prompt_tokens=400,
            completion_tokens=40,
            total_tokens=520,
            prompt_reported=True,
            completion_reported=True,
            total_reported=True,
            reasoning_tokens=80,
            reasoning_reported=True,
            reasoning_in_output="unknown",
        )
    )

    assert stats.observed_context_tokens == 520
    assert stats.context_tokens_source == "provider"


def test_missing_total_and_unknown_reasoning_are_honestly_labeled_mixed() -> None:
    stats = provider().context_stats(
        observed=TokenUsage(
            prompt_tokens=400,
            completion_tokens=40,
            prompt_reported=True,
            completion_reported=True,
            reasoning_tokens=80,
            reasoning_reported=True,
            reasoning_in_output="unknown",
        )
    )

    assert stats.observed_context_tokens == 440
    assert stats.context_tokens_source == "mixed"
    assert stats.remaining_tokens == 560


def test_reported_zero_usage_is_distinct_from_missing_usage() -> None:
    zero = provider().context_stats(
        observed=TokenUsage(prompt_reported=True, completion_reported=True, total_reported=True)
    )
    missing = provider().context_stats()

    assert zero.observed_context_tokens == 0
    assert zero.context_tokens == 0
    assert zero.context_tokens_source == "provider"
    assert missing.observed_context_tokens is None
    assert missing.context_tokens_source == "estimated"


def test_appended_messages_are_estimated_once_after_observed_response() -> None:
    appended = [Message(role="tool", name="lookup", content="result" * 40)]
    stats = provider().context_stats(
        observed=TokenUsage(
            prompt_tokens=400,
            completion_tokens=120,
            total_tokens=520,
            prompt_reported=True,
            completion_reported=True,
            total_reported=True,
            reasoning_tokens=80,
            reasoning_reported=True,
            reasoning_in_output="included",
        ),
        additional_messages=appended,
    )

    assert stats.observed_context_tokens == 520
    assert stats.pending_context_tokens > 0
    assert stats.context_tokens == 520 + stats.pending_context_tokens
    assert stats.remaining_tokens == 1_000 - stats.context_tokens


def test_model_compaction_threshold_uses_provider_context_snapshot() -> None:
    selected_provider = provider()
    stats = selected_provider.context_stats(
        observed=TokenUsage(
            prompt_tokens=400,
            completion_tokens=120,
            total_tokens=520,
            prompt_reported=True,
            completion_reported=True,
            total_reported=True,
            reasoning_tokens=80,
            reasoning_reported=True,
            reasoning_in_output="included",
        )
    )
    request = CompactionRequest(
        messages=[Message(role="user", content="short")],
        session_id="s",
        provider=selected_provider,
        estimated_tokens=100,
        actual_input_tokens=400,
        context_stats=stats,
    )

    async def check() -> None:
        assert await ModelCompaction(token_threshold=520).should_compact(request)
        assert not await ModelCompaction(token_threshold=521).should_compact(request)

    asyncio.run(check())
