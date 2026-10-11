"""Provider-backed compaction strategy behavior."""

import asyncio
from pathlib import Path
from typing import cast

import pytest

from nagents import Agent
from nagents import ModelCompaction
from nagents import SessionManager
from nagents.compaction import DEFAULT_COMPACTION_PROMPT
from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import CompactionRequest
from nagents.types import Message
from tests.agent.test_extensions import OfflineProvider


def test_model_compaction_thresholds_prefer_observed_input_tokens() -> None:
    async def drive() -> None:
        provider = OfflineProvider()
        request = CompactionRequest(
            messages=[Message(role="user", content="small")],
            session_id="s",
            provider=provider,
            estimated_tokens=100,
            actual_input_tokens=900,
        )
        assert await ModelCompaction(token_threshold=900).should_compact(request)
        assert not await ModelCompaction(token_threshold=901).should_compact(request)
        assert await ModelCompaction(message_threshold=1).should_compact(request)
        assert not await ModelCompaction(message_threshold=2).should_compact(request)
        # When usage is unavailable, use the estimate.
        request.actual_input_tokens = 0
        assert await ModelCompaction(token_threshold=100).should_compact(request)

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"token_threshold": 0}, "token_threshold"),
        ({"message_threshold": -1}, "message_threshold"),
        ({"token_threshold": 1, "message_threshold": 1}, "mutually exclusive"),
    ],
)
def test_model_compaction_rejects_invalid_thresholds(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ModelCompaction(
            token_threshold=kwargs.get("token_threshold"), message_threshold=kwargs.get("message_threshold")
        )


@pytest.mark.parametrize("value", [True, 1.5, float("nan"), float("inf")])
def test_model_compaction_requires_integer_thresholds(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ModelCompaction(token_threshold=cast("int", value))
    with pytest.raises(ValueError, match="positive integer"):
        ModelCompaction(message_threshold=cast("int", value))


def test_model_compaction_rejects_blank_prompt() -> None:
    with pytest.raises(ValueError, match="nonempty text"):
        ModelCompaction(prompt=" \n ")


def test_model_compaction_uses_configured_prompt_and_provider() -> None:
    async def drive() -> None:
        summarizer = OfflineProvider()
        request = CompactionRequest(
            messages=[Message(role="user", content="keep this fact")],
            session_id="s",
            provider=OfflineProvider(),
            estimated_tokens=20,
        )
        strategy = ModelCompaction(prompt="Keep the key fact.", provider=summarizer)
        result = await strategy.compact(request)
        assert summarizer.requests[0].messages[0] == Message(role="system", content="Keep the key fact.")
        assert "keep this fact" in str(summarizer.requests[0].messages[1].content)
        assert result.messages == [Message(role="compaction_summary", content="answer")]
        assert result.summary == "answer"

    asyncio.run(drive())


def test_model_compaction_defaults_to_continuity_prompt_and_agent_provider() -> None:
    async def drive() -> None:
        provider = OfflineProvider()
        request = CompactionRequest(
            messages=[Message(role="user", content="context")],
            session_id="s",
            provider=provider,
            estimated_tokens=10,
        )
        await ModelCompaction().compact(request)
        assert provider.requests[0].messages[0] == Message(role="system", content=DEFAULT_COMPACTION_PROMPT)

    asyncio.run(drive())


@pytest.mark.parametrize(
    "events",
    [
        [ErrorEvent(message="summary failed")],
        [ToolCallEvent(id="call-1", name="tool")],
        [TextDoneEvent(text="  ")],
        [TextDoneEvent(text="partial", finish_reason=FinishReason.LENGTH)],
    ],
)
def test_failed_model_compaction_does_not_replace_active_history(tmp_path: Path, events: list[Event]) -> None:
    async def drive() -> None:
        provider = OfflineProvider([events])
        sessions = SessionManager(tmp_path / "failed_compaction.db")
        await sessions.get_or_create_session("s", "u")
        original = [Message(role="user", content="keep this history")]
        await sessions.replace_context("s", original)
        agent = Agent(provider, sessions, compaction_strategy=ModelCompaction(message_threshold=1))
        with pytest.raises(ValueError):
            await agent.compact("s")
        assert await sessions.get_history("s") == original
        assert await sessions.get_message_count("s") == 1
        await agent.close()

    asyncio.run(drive())
