"""Provider-backed compaction strategy behavior."""

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from nagents import Agent
from nagents import ModelCompaction
from nagents import SessionManager
from nagents.compaction import COMPACTION_CONTINUATION_PROMPT
from nagents.compaction import DEFAULT_COMPACTION_PROMPT
from nagents.compaction import with_compaction_continuation
from nagents.events import CompactionDoneEvent
from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import CompactionRequest
from nagents.types import Message
from nagents.types import ToolCall
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
    ("messages", "continued"),
    [
        ([], False),
        ([Message(role="system", content="rules")], False),
        ([Message(role="assistant", content="ordinary prefill")], False),
        ([Message(role="user", content="go")], False),
        ([Message(role="compaction_summary", content="facts"), Message(role="user", content="next")], False),
        (
            [
                Message(role="compaction_summary", content="facts"),
                Message(role="developer", content="rules"),
                Message(role="user", content="next"),
            ],
            False,
        ),
        ([Message(role="compaction_summary", content="facts")], True),
        ([Message(role="system", content="rules"), Message(role="compaction_summary", content="facts")], True),
        (
            [Message(role="compaction_summary", content="first"), Message(role="compaction_summary", content="second")],
            True,
        ),
        (
            [
                Message(role="compaction_summary", content="facts"),
                Message(role="assistant", content="done"),
                Message(role="user", content="next"),
            ],
            True,
        ),
        (
            [
                Message(role="compaction_summary", content="facts"),
                Message(role="assistant", tool_calls=[ToolCall("a", "work")]),
                Message(role="tool", tool_call_id="a", name="work", content="completed"),
            ],
            True,
        ),
    ],
)
def test_compaction_continuation_is_idempotent_preserves_history_and_only_fills_missing_user_boundary(
    messages: list[Message], continued: bool
) -> None:
    original = deepcopy(messages)
    prepared = with_compaction_continuation(messages)
    assert messages == original
    continuation = Message(role="user", content=COMPACTION_CONTINUATION_PROMPT)
    assert prepared.count(continuation) == int(continued)
    assert [message for message in prepared if message != continuation] == original
    assert with_compaction_continuation(prepared) is prepared
    if not continued:
        assert prepared is messages


def test_summary_only_continuation_formats_for_every_native_contract() -> None:
    from nagents.adapters import anthropic
    from nagents.adapters import gemini
    from nagents.adapters import openai
    from nagents.adapters import responses

    messages = with_compaction_continuation([Message(role="compaction_summary", content="Completed work")])
    assert anthropic.format_messages(messages)[1][0]["role"] == "user"
    assert gemini.format_request(messages)["contents"][0]["role"] == "user"
    assert openai.format_messages(messages)[-1]["role"] == "user"
    assert responses.format_request("fixture", messages, [], None, True)["input"]


def test_repeated_mid_turn_compactions_rebuild_one_ephemeral_continuation(tmp_path: Path) -> None:
    async def drive() -> None:
        provider = OfflineProvider(
            [
                [ToolCallEvent(id="first", name="work", arguments={"value": "first"})],
                [TextDoneEvent(text="First action completed")],
                [ToolCallEvent(id="second", name="work", arguments={"value": "second"})],
                [TextDoneEvent(text="Both actions completed")],
                [TextDoneEvent(text="Finished")],
            ]
        )
        performed: list[str] = []

        def work(value: str) -> str:
            """Perform a local action once."""
            performed.append(value)
            return value

        session = SessionManager(tmp_path / "repeated.db")
        agent = Agent(provider, session, tools=[work], compaction_strategy=ModelCompaction(message_threshold=3))
        try:
            events = [event async for event in agent.run("Do both actions", session_id="s")]
            assert performed == ["first", "second"]
            assert sum(isinstance(event, CompactionDoneEvent) for event in events) == 2
            continuation = Message(role="user", content=COMPACTION_CONTINUATION_PROMPT)
            assert provider.requests[2].messages == [
                Message(role="compaction_summary", content="First action completed"),
                continuation,
            ]
            assert provider.requests[4].messages == [
                Message(role="compaction_summary", content="Both actions completed"),
                continuation,
            ]
            assert await SessionManager(session.db_path).get_history("s") == [
                Message(role="compaction_summary", content="Both actions completed"),
                Message(role="assistant", content="Finished"),
            ]
        finally:
            await agent.close()

    asyncio.run(drive())


def test_manual_compaction_with_new_user_turn_does_not_invent_a_continuation(tmp_path: Path) -> None:
    async def drive() -> None:
        provider = OfflineProvider([[TextDoneEvent(text="Saved facts")], [TextDoneEvent(text="Answer")]])
        session = SessionManager(tmp_path / "manual.db")
        await session.get_or_create_session("s", "u")
        await session.add_message("s", Message(role="user", content="Old request"))
        agent = Agent(provider, session, compaction_strategy=ModelCompaction(message_threshold=10))
        try:
            await agent.compact("s")
            _ = [event async for event in agent.run("New request", session_id="s")]
            assert provider.requests[-1].messages == [
                Message(role="compaction_summary", content="Saved facts"),
                Message(role="user", content="New request"),
            ]
        finally:
            await agent.close()

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
