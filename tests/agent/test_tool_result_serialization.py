"""Tool context encoding preserves original results and custom representations."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents import Agent
from nagents import SessionManager
from nagents.agent import _save_and_return
from nagents.agent import _serialize_tool_result
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.types import ImageContent
from nagents.types import TextContent
from tests.agent.test_agent_reliability import ScriptedProvider

if TYPE_CHECKING:
    from pathlib import Path


class CustomResult:
    def __str__(self) -> str:
        return "custom result: ✓"


@pytest.mark.parametrize(
    "result",
    [None, True, False, 42, -2.5, [], {}, {"a": [None, True, "Zürich", 'quote"\n', {"path": "a\\b"}]}],
)
def test_json_results_round_trip_without_whitespace(result: object) -> None:
    encoded = _serialize_tool_result(result)
    assert json.loads(encoded) == result
    assert encoded == json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@pytest.mark.parametrize(
    "result",
    [
        "plain\ntext ✓",
        b"raw\x00",
        (1, 2),
        {1: "key"},
        {"nested": (1, 2)},
        float("nan"),
        {"surrogate": "\ud800"},
        CustomResult(),
        [TextContent(text="hello"), ImageContent(base64_data="AA==", media_type="image/png")],
    ],
)
def test_text_and_non_json_results_keep_existing_representation(result: object) -> None:
    assert _serialize_tool_result(result) == str(result)


def test_circular_results_keep_existing_representation() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    assert _serialize_tool_result(cyclic) == str(cyclic)


@pytest.mark.parametrize("streaming", [True, False])
def test_compound_tools_preserve_events_and_session_context(tmp_path: Path, streaming: bool) -> None:
    async def drive() -> None:
        result = {"rows": [{"message": 'Zürich "quoted"\nline', "ok": True, "missing": None}], "total": 1}

        def read_rows() -> dict[str, object]:
            """Read rows."""
            return result

        def custom() -> CustomResult:
            """Return custom output."""
            return CustomResult()

        def fail() -> str:
            """Fail explicitly."""
            raise ValueError("failed ✓")

        provider = ScriptedProvider(
            [
                [ToolCallEvent(id=name, name=name) for name in ("read_rows", "custom", "fail")],
                [TextDoneEvent(text="done")],
            ]
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "results.db"),
            tools=[read_rows, custom, fail],
            streaming=streaming,
            compactor=None,
        )
        try:
            events = [event async for event in agent.run("go", session_id="s")]
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert results[0].result is result
            assert isinstance(results[1].result, CustomResult)
            assert "failed ✓" in str(results[2].error)
            context = [message for message in provider.requests[1] if message.role == "tool"]
            assert [message.tool_call_id for message in context] == ["read_rows", "custom", "fail"]
            assert context[0].content == _serialize_tool_result(result)
            assert context[1].content == "custom result: ✓"
            assert context[2].content == f"Error: {results[2].error}"
            stored = await agent.session.get_history("s")
            assert [message.content for message in stored if message.role == "tool"] == [
                message.content for message in context
            ]
        finally:
            await agent.close()

    asyncio.run(drive())


def test_saved_results_use_same_lossless_encoding(tmp_path: Path) -> None:
    result = {"rows": ["✓", None, False], "metadata": {"count": 3}}
    event = ToolResultEvent(id="read", name="read", result=result)
    path = tmp_path / "output.txt"
    assert "saved" in _save_and_return(event, str(path), "s")
    assert json.loads(path.read_text()) == result
    assert path.read_text() == _serialize_tool_result(result)
    large = {"body": "✓" * 2000}
    failure = _save_and_return(ToolResultEvent(id="read", name="read", result=large), str(path / "bad"), "s")
    assert failure.split("\n", 1)[1] == _serialize_tool_result(large)[:1000]
