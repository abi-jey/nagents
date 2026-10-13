"""Offline provider summary and reported-usage contracts."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.adapters import anthropic
from nagents.adapters._reasoning import SummaryAccumulator
from nagents.adapters._validation import ProtocolError
from nagents.adapters._validation import list_data
from nagents.adapters._validation import token_usage
from nagents.adapters.responses import ResponseAccumulator
from nagents.events import ErrorEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.openai import _request_body
from nagents.provider.openai import _usage
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolCall
from tests.providers.test_gateway_provider import payload
from tests.providers.test_provider_reliability import CALL
from tests.providers.test_provider_reliability import candidate
from tests.providers.test_provider_reliability import gemini_response
from tests.providers.test_tool_stream_boundaries import generate

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("api", ["responses", "chat_completions", "gemini", "messages", "codex"])
@pytest.mark.parametrize("count", [0, 7])
def test_reasoning_counts_report_zero_without_double_counting(api: str, count: int) -> None:
    if api == "gemini":
        wire: dict[str, object] = {"promptTokenCount": 10, "candidatesTokenCount": 8, "thoughtsTokenCount": count}
    elif api == "messages":
        wire = {"input_tokens": 10, "output_tokens": 8, "output_tokens_details": {"thinking_tokens": count}}
    elif api == "chat_completions":
        wire = {"prompt_tokens": 10, "completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": count}}
    else:
        wire = {"input_tokens": 10, "output_tokens": 8, "output_tokens_details": {"reasoning_tokens": count}}
    usage = _usage(wire) if api == "codex" else token_usage(wire, api)
    assert usage.reasoning_tokens == count and usage.reasoning_reported and usage.reported
    assert usage.reasoning_in_output == ("separate" if api == "gemini" else "included")
    assert usage.total_tokens == 18 + (count if api == "gemini" else 0)
    assert usage.prompt_reported and usage.completion_reported and not usage.total_reported
    assert asdict(usage)["reasoning_reported"] is True


@pytest.mark.parametrize("api", ["responses", "chat_completions", "gemini", "messages", "codex"])
def test_missing_usage_is_not_a_reported_zero(api: str) -> None:
    empty_frames: tuple[object, ...] = (None, {})
    for wire in empty_frames:
        usage = _usage(wire) if api == "codex" else token_usage(wire, api)
        assert not usage.reported and not usage.reasoning_reported and not usage.has_usage()
        assert usage.reasoning_in_output == "unknown"
        assert not usage.prompt_reported and not usage.completion_reported and not usage.total_reported
    key = "promptTokenCount" if api == "gemini" else "prompt_tokens" if api == "chat_completions" else "input_tokens"
    zero = _usage({key: 0}) if api == "codex" else token_usage({key: 0}, api)
    assert zero.reported and zero.has_usage() and not zero.reasoning_reported
    assert zero.prompt_reported and not zero.completion_reported and not zero.total_reported


@pytest.mark.parametrize("api", ["responses", "chat_completions", "gemini", "messages", "codex"])
@pytest.mark.parametrize("count", [0, 7])
def test_reasoning_only_snapshot_does_not_fabricate_reported_input_or_output(api: str, count: int) -> None:
    if api == "gemini":
        wire: dict[str, object] = {"thoughtsTokenCount": count}
    elif api == "messages":
        wire = {"output_tokens_details": {"thinking_tokens": count}}
    else:
        wire = {
            f"{'completion' if api == 'chat_completions' else 'output'}_tokens_details": {"reasoning_tokens": count}
        }
    usage = _usage(wire) if api == "codex" else token_usage(wire, api)
    assert usage.reported and usage.reasoning_reported and usage.reasoning_tokens == count
    assert not usage.prompt_reported and not usage.completion_reported and not usage.total_reported
    assert usage.prompt_tokens == usage.completion_tokens == 0
    assert usage.total_tokens == (count if api == "gemini" else 0)


@pytest.mark.parametrize("api", ["responses", "chat_completions", "gemini", "messages", "codex"])
def test_each_output_and_total_counter_has_independent_zero_provenance(api: str) -> None:
    output_key = (
        "candidatesTokenCount"
        if api == "gemini"
        else "completion_tokens"
        if api == "chat_completions"
        else "output_tokens"
    )
    output = _usage({output_key: 0}) if api == "codex" else token_usage({output_key: 0}, api)
    assert output.reported and output.completion_reported
    assert not output.prompt_reported and not output.total_reported
    if api == "messages":
        cached = token_usage({"cache_read_input_tokens": 0}, api)
        assert cached.prompt_reported and not cached.completion_reported and not cached.total_reported
    else:
        total_key = "totalTokenCount" if api == "gemini" else "total_tokens"
        total = _usage({total_key: 0}) if api == "codex" else token_usage({total_key: 0}, api)
        assert total.reported and total.total_reported
        assert not total.prompt_reported and not total.completion_reported


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_gemini_thought_parts_never_become_answer_or_tool_prose(stream: bool) -> None:
    result = candidate(
        [{"text": "Display summary", "thought": True, "thoughtSignature": "opaque-text"}, {"text": "Answer"}, CALL]
    )
    result["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 8, "thoughtsTokenCount": 7}
    async with gemini_response([result]) as provider:
        events = [event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)]
    assert "".join(event.chunk for event in events if isinstance(event, ReasoningChunkEvent)) == "Display summary"
    done = next(event for event in events if isinstance(event, TextDoneEvent))
    assert done.text == "Answer" and done.usage.total_tokens == 25
    assert all(event.displayable for event in events if isinstance(event, ReasoningChunkEvent))
    call = next(event for event in events if isinstance(event, ToolCallEvent))
    assert call.metadata == {"thoughtSignature": "test-signature"}


@pytest.mark.parametrize("api", ["responses", "codex"])
def test_nullable_total_uses_reported_input_and_output(api: str) -> None:
    wire = {"input_tokens": 10, "output_tokens": 8, "total_tokens": None}
    usage = _usage(wire) if api == "codex" else token_usage(wire, api)
    assert usage.reported and usage.total_tokens == 18
    assert usage.prompt_reported and usage.completion_reported and not usage.total_reported
    assert not usage.reasoning_reported


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_summaries_signatures_and_final_usage_remain_separate(stream: bool) -> None:
    blocks: list[dict[str, object]] = [
        {"type": "thinking", "thinking": "Display summary", "signature": "opaque-signature"},
        {"type": "redacted_thinking", "data": "opaque-redacted"},
        {"type": "text", "text": "Answer"},
        {"type": "tool_use", "id": "call", "name": "work", "input": {}},
    ]
    usage: dict[str, object] = {"output_tokens": 8, "output_tokens_details": {"thinking_tokens": 7}}
    if stream:
        wire: list[dict[str, object] | str] = [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}}]
        for index, block in enumerate(blocks):
            wire.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {**block, "thinking": "", "signature": ""} if index == 0 else block,
                }
            )
            if index == 0:
                wire.extend(
                    [
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "thinking_delta", "thinking": "Display summary"},
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "signature_delta", "signature": "opaque-signature"},
                        },
                    ]
                )
            wire.append({"type": "content_block_stop", "index": index})
        wire.extend(
            [{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": usage}, {"type": "message_stop"}]
        )
        events = await generate("messages", wire)
    else:
        events = await generate(
            "messages",
            {"type": "message", "content": blocks, "stop_reason": "tool_use", "usage": {"input_tokens": 10, **usage}},
        )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert "".join(event.chunk for event in events if isinstance(event, ReasoningChunkEvent)) == "Display summary"
    done = next(event for event in events if isinstance(event, TextDoneEvent))
    assert done.text == "Answer" and done.usage.reasoning_tokens == 7 and done.usage.total_tokens == 18
    assert all(event.displayable for event in events if isinstance(event, ReasoningChunkEvent))
    call = next(event for event in events if isinstance(event, ToolCallEvent))
    _, continuation = anthropic.format_messages(
        [
            Message(
                role="assistant",
                content=done.text,
                tool_calls=[ToolCall(call.id, call.name, call.arguments, call.metadata)],
            )
        ]
    )
    assert continuation[0]["content"][:2] == blocks[:2]
    assert continuation[0]["content"][2] == {"type": "text", "text": "Answer"}


@pytest.mark.parametrize("stream", [False, True])
def test_responses_summary_delta_done_and_final_snapshot_emit_once(stream: bool) -> None:
    accumulator = ResponseAccumulator(streaming=stream)
    emitted = accumulator.add(
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "Display "}
    )
    emitted += accumulator.add(
        {
            "type": "response.reasoning_summary_text.done",
            "output_index": 0,
            "summary_index": 0,
            "text": "Display summary",
        }
    )
    result = payload("responses", "Answer")
    result["output"] = [
        {
            "type": "reasoning",
            "id": "reason",
            "summary": [{"type": "summary_text", "text": "Display summary"}],
            "encrypted_content": "opaque",
        },
        *list_data(result["output"]),
    ]
    emitted += accumulator.finish(result)
    assert "".join(event.chunk for event in emitted if isinstance(event, ReasoningChunkEvent)) == "Display summary"
    assert next(event for event in emitted if isinstance(event, TextDoneEvent)).text == "Answer"


def test_summary_bounds_and_conflicting_snapshots() -> None:
    accumulator = SummaryAccumulator(streaming=True)
    events = accumulator.add({"type": "response.reasoning_summary_text.delta", "delta": "x" * 100000})
    accumulator.finish_item(0, {"summary": [{"type": "summary_text", "text": "x" * 100000}]})
    assert sum(len(event.chunk) for event in events) == 65536 and not accumulator.finish()
    with pytest.raises(ProtocolError, match="inconsistent"):
        accumulator.add({"type": "response.reasoning_summary_text.done", "text": "different"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api,kind,model,host,expected",
    [
        ("responses", ProviderType.OPENAI_COMPATIBLE, "gpt-6.1-sol", "https://api.openai.com/v1", True),
        ("responses", ProviderType.OPENAI_COMPATIBLE, "gpt-4o", "https://api.openai.com/v1", False),
        ("responses", ProviderType.OPENAI_COMPATIBLE, "gpt-5-chat-latest", "https://api.openai.com/v1", False),
        ("responses", ProviderType.OPENAI_COMPATIBLE, "gpt-5.1-chat-latest", "https://api.openai.com/v1", False),
        ("responses", ProviderType.OPENAI_COMPATIBLE, "gpt-6-chat-latest", "https://api.openai.com/v1", False),
        ("responses", ProviderType.OPENAI_COMPATIBLE, "gpt-6.1-sol", "https://gateway.example/v1", False),
        ("messages", ProviderType.ANTHROPIC, "claude-sonnet-5", "https://api.anthropic.com/v1", True),
        ("messages", ProviderType.ANTHROPIC, "claude-sonnet-4-6", "https://api.anthropic.com/v1", False),
        ("messages", ProviderType.ANTHROPIC, "claude-haiku-4-5", "https://api.anthropic.com/v1", False),
        ("messages", ProviderType.ANTHROPIC, "claude-sonnet-4-6", "https://gateway.example/v1", False),
        (
            "auto",
            ProviderType.GEMINI_NATIVE,
            "gemini-3.8-flash",
            "https://generativelanguage.googleapis.com/v1beta",
            True,
        ),
        (
            "auto",
            ProviderType.GEMINI_NATIVE,
            "gemini-2.0-flash",
            "https://generativelanguage.googleapis.com/v1beta",
            False,
        ),
    ],
)
async def test_summary_opt_in_only_changes_supported_native_requests(
    api: str, kind: ProviderType, model: str, host: str, expected: bool
) -> None:
    provider = Provider(kind, "offline", model, base_url=host, api=api)
    response = candidate([{"text": "Answer"}]) if api == "auto" else payload(api)
    try:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=response)) as request:
            events = [
                event
                async for event in provider.generate(
                    [Message(role="user", content="Hello")],
                    config=GenerationConfig(reasoning_summary="auto"),
                    stream=False,
                )
            ]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        body = request.call_args.args[1]
        key = "generationConfig" if api == "auto" else "thinking" if api == "messages" else "reasoning"
        assert (key in body) is expected
        if expected:
            assert body[key] == (
                {"thinkingConfig": {"includeThoughts": True}}
                if api == "auto"
                else {"type": "adaptive", "display": "summarized"}
                if api == "messages"
                else {"summary": "auto"}
            )
    finally:
        await provider.close()


def test_codex_explicit_effort_and_summary_precedence() -> None:
    config = GenerationConfig(reasoning_summary="auto", responses_reasoning={"effort": "low", "summary": "concise"})
    body = _request_body("gpt-6.1-sol", [Message(role="user", content="Hello")], None, config)
    assert body["reasoning"] == {"effort": "low", "summary": "concise"}
    assert "reasoning" not in _request_body("gpt-6.1-sol", [], None, GenerationConfig())
    assert "reasoning" not in _request_body(
        "gpt-6.1-sol", [], None, GenerationConfig(reasoning_summary="auto", reasoning={"enabled": False})
    )


@pytest.mark.asyncio
async def test_explicit_gemini_opt_out_and_responses_gateway_summary_are_preserved() -> None:
    provider = Provider(ProviderType.GEMINI_NATIVE, "offline", "gemini-3.8-flash")
    config = GenerationConfig(
        reasoning_summary="auto", thinking_config={"includeThoughts": False, "thinkingBudget": 2048}
    )
    try:
        with patch.object(
            provider._http, "post_json", AsyncMock(return_value=candidate([{"text": "Answer"}]))
        ) as request:
            _ = [
                event
                async for event in provider.generate(
                    [Message(role="user", content="Hello")], config=config, stream=False
                )
            ]
        assert request.call_args.args[1]["generationConfig"]["thinkingConfig"] == {
            "includeThoughts": False,
            "thinkingBudget": 2048,
        }
    finally:
        await provider.close()
    provider = Provider(
        ProviderType.LITELLM, "offline", "custom-model", base_url="https://gateway.example", api="responses"
    )
    config = GenerationConfig(reasoning_summary="auto", responses_reasoning={"effort": "low", "summary": "concise"})
    try:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))) as request:
            _ = [
                event
                async for event in provider.generate(
                    [Message(role="user", content="Hello")], config=config, stream=False
                )
            ]
        assert request.call_args.args[1]["reasoning"] == {"effort": "low", "summary": "concise"}
    finally:
        await provider.close()


def test_explicit_thinking_start_reports_phase_without_inventing_text_or_usage() -> None:
    events = ResponseAccumulator().add(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs-1", "summary": []},
        }
    )
    assert len(events) == 1 and isinstance(events[0], ReasoningChunkEvent)
    assert events[0].chunk == "" and not events[0].usage.has_usage()


def test_harness_summary_default_opt_out_and_json(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
    harness = Harness(config)
    assert harness.generation_config() == GenerationConfig(reasoning_summary="auto")
    config.reasoning_summaries = False
    assert harness.generation_config() is None
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"reasoning_summaries": False}))
    assert load_config(tmp_path, path).reasoning_summaries is False
    path.write_text(json.dumps({"reasoning_summaries": "false"}))
    with pytest.raises(ValueError, match="reasoning_summaries"):
        load_config(tmp_path, path)
