"""Offline checks for grading and variant controls; no model requests."""

import asyncio
from pathlib import Path

import pytest

from benchmarks.tool_descriptions.run import CASES
from benchmarks.tool_descriptions.run import canonical
from benchmarks.tool_descriptions.run import grade
from benchmarks.tool_descriptions.run import private_json
from benchmarks.tool_descriptions.run import shipping_tools
from benchmarks.tool_descriptions.run import variant_tools
from nagents.types import ToolCall
from nagents.types import ToolDefinition


def tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            "read_file",
            "Read text",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        )
    ]


def test_name_variant_preserves_schema_and_round_trips() -> None:
    original = tools()
    renamed = variant_tools(original, "explicit_names")
    assert original[0].name == "read_file"
    assert renamed[0].name == "read_text_lines"
    assert renamed[0].description == original[0].description
    assert renamed[0].parameters["required"] == ["file_path"]
    assert renamed[0].parameters["properties"]["line_count"] == {"type": "integer"}
    call = ToolCall("id", "read_text_lines", {"file_path": "src/app.py", "first_line_number": 21, "line_count": 10})
    assert canonical(call, "explicit_names").name == "read_file"
    assert grade(CASES[0], [call], renamed, "explicit_names") == []


def test_description_variant_does_not_change_names_or_types() -> None:
    original = tools()
    changed = variant_tools(original, "clarified")
    assert changed[0].name == original[0].name
    assert changed[0].parameters["required"] == original[0].parameters["required"]
    assert changed[0].parameters["properties"]["limit"]["type"] == "integer"
    assert changed[0].description != original[0].description


@pytest.mark.parametrize(
    "calls,expected",
    [
        ([], "expected_one_call"),
        ([ToolCall("id", "invented", {})], "unknown_tool"),
        ([ToolCall("id", "read_file", {"path": "src/app.py", "limit": "10"})], "invalid_schema"),
        ([ToolCall("id", "read_file", {"path": "src/app.py", "start_line": 21, "limit": 30})], "wrong_argument:limit"),
    ],
)
def test_grader_rejects_wrong_calls(calls: list[ToolCall], expected: str) -> None:
    assert expected in grade(CASES[0], calls, tools(), "current")


def test_grader_rejects_unknown_arguments_and_extra_calls() -> None:
    call = ToolCall("id", "read_file", {"path": "src/app.py", "start_line": 21, "limit": 10, "hash": "x"})
    assert "invalid_schema" in grade(CASES[0], [call], tools(), "current")
    assert "expected_one_call" in grade(CASES[0], [call, call], tools(), "current")


def test_task_status_requires_text_and_no_invented_call() -> None:
    case = next(case for case in CASES if case.name == "task_status")
    assert grade(case, [], tools(), "current", "No polling tool is exposed.") == []
    assert grade(case, [], tools(), "current")
    assert grade(case, [ToolCall("id", "task_status", {})], tools(), "current", "No polling tool.")


def test_runtime_limit_checked_even_when_schema_has_no_bound() -> None:
    case = next(case for case in CASES if case.name == "bounded_sample")
    assert "read_limit_out_of_bounds" in grade(
        case, [ToolCall("id", "read_file", {"path": "data.txt", "limit": 1500})], tools(), "current"
    )


@pytest.mark.requires_posix
def test_private_json_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    private_json(destination, {"ok": True})
    assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_json(destination, {"ok": False})


def test_shipping_schema_capture_does_not_execute_tools(tmp_path: Path) -> None:
    captured = asyncio.run(shipping_tools(tmp_path))
    assert {"read_file", "find", "search", "edit", "write", "shell", "delegate"} <= {tool.name for tool in captured}
    assert all(tool.func is None for tool in captured)
    assert "demo_preview" not in {tool.name for tool in captured}


def test_provider_text_done_event_is_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import AsyncIterator

    import benchmarks.tool_descriptions.run as benchmark
    from nagents.events import Event
    from nagents.events import TextDoneEvent
    from nagents.events import ToolCallEvent
    from nagents.events import Usage
    from nagents.provider.openai import OpenAIProvider
    from nagents.types import GenerationConfig
    from nagents.types import Message

    async def fake_generate(
        self: OpenAIProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        case = next(case for case in CASES if case.prompt == messages[-1].content)
        yield TextDoneEvent(
            text="No status polling tool is exposed.",
            usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12, cached_tokens=4, reasoning_tokens=1),
        )
        if case.tool:
            yield ToolCallEvent(
                id="id",
                name=case.tool,
                arguments=dict(case.expected),
                usage=Usage(
                    prompt_tokens=10, completion_tokens=2, total_tokens=12, cached_tokens=4, reasoning_tokens=1
                ),
            )

    # Explicit fake credentials bypass all saved-auth discovery. No HTTP occurs.
    def fake_provider(**kwargs: object) -> OpenAIProvider:
        return OpenAIProvider(api_key="test-key", model="offline")

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)
    monkeypatch.setattr(benchmark, "OpenAIProvider", fake_provider)
    result = asyncio.run(benchmark.run("offline", 2, tmp_path / "results", 1, "current"))
    assert result["passed"] == len(CASES) - 1
    assert result["structural_passed"] == len(CASES)
    assert result["manual_review_cases"] == 1
    assert result["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 24,
        "total_tokens": 144,
        "cached_tokens": 48,
        "reasoning_tokens": 12,
        "audio_tokens": 0,
    }
    assert result["cases_with_usage"] == 12
    assert result["usage_complete"] is True
    assert result["uncached_prompt_tokens"] == 72
    assert result["provider_error_cases"] == 0
    assert result["rubric_failure_cases"] == 0


def test_provider_failure_is_not_a_cheap_completed_evaluation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import AsyncIterator

    import benchmarks.tool_descriptions.run as benchmark
    from nagents.events import ErrorEvent
    from nagents.events import Event
    from nagents.provider.openai import OpenAIProvider

    async def failed_generate(self: OpenAIProvider, *args: object, **kwargs: object) -> AsyncIterator[Event]:
        yield ErrorEvent(message="Rate limited", code="CODEX_HTTP_429")

    def fake_provider(**kwargs: object) -> OpenAIProvider:
        return OpenAIProvider(api_key="test-key", model="offline")

    monkeypatch.setattr(OpenAIProvider, "generate", failed_generate)
    monkeypatch.setattr(benchmark, "OpenAIProvider", fake_provider)
    result = asyncio.run(benchmark.run("offline", 2, tmp_path / "results", 1, "current"))
    assert result["passed"] == 0
    assert result["cases_with_usage"] == 0
    assert result["usage_complete"] is False
    assert result["provider_error_cases"] == len(CASES)
    assert result["rubric_failure_cases"] == 0


def test_equivalent_recursive_glob_is_accepted() -> None:
    case = next(case for case in CASES if case.name == "filename_glob")
    tool = ToolDefinition(
        "find",
        "Find paths",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}},
        },
    )
    assert (
        grade(case, [ToolCall("id", "find", {"pattern": "*.py", "path": "src", "limit": 20})], [tool], "current") == []
    )


def test_renamed_descriptions_use_corresponding_argument_names() -> None:
    original = tools()
    original[0].description = "read_file: start_line is first line; limit is count."
    changed = variant_tools(original, "explicit_names")
    assert changed[0].description == "read_text_lines: first_line_number is first line; line_count is count."
    assert original[0].description.startswith("read_file:")


@pytest.mark.parametrize(
    "name",
    [
        "challenge_numbered_crlf",
        "challenge_duplicate_edit",
        "challenge_replace_existing",
        "challenge_delete_exact_line",
    ],
)
def test_challenge_edit_grades_result_not_specific_match_width(name: str) -> None:
    from benchmarks.tool_descriptions.challenges import EDIT_FIXTURES
    from benchmarks.tool_descriptions.challenges import get_cases
    from benchmarks.tool_descriptions.challenges import grade_challenge
    from benchmarks.tool_descriptions.run import Case

    case = next(case for case in get_cases(Case) if case.name == name)
    before, after = EDIT_FIXTURES[name]
    assert grade_challenge(case, ToolCall("id", "edit", {"old": before, "new": after})) == []
    assert grade_challenge(case, ToolCall("id", "edit", {"old": before, "new": "wrong"})) == ["edit_wrong_result"]
    assert grade_challenge(case, ToolCall("id", "edit", {"old": "", "new": after})) == ["invalid_literal_edit"]


def test_challenge_rejects_nonunique_literal() -> None:
    from benchmarks.tool_descriptions.challenges import get_cases
    from benchmarks.tool_descriptions.challenges import grade_challenge
    from benchmarks.tool_descriptions.run import Case

    case = next(case for case in get_cases(Case) if case.name == "challenge_duplicate_edit")
    assert grade_challenge(case, ToolCall("id", "edit", {"old": "enabled=false", "new": "enabled=true"})) == [
        "edit_match_not_unique"
    ]


@pytest.mark.parametrize("bad_old", ["host=localhost\nport=8080", "2: host=localhost\r\n3: port=8080"])
def test_challenge_rejects_lost_crlf_and_display_prefixes(bad_old: str) -> None:
    from benchmarks.tool_descriptions.challenges import get_cases
    from benchmarks.tool_descriptions.challenges import grade_challenge
    from benchmarks.tool_descriptions.run import Case

    case = next(case for case in get_cases(Case) if case.name == "challenge_numbered_crlf")
    assert grade_challenge(case, ToolCall("id", "edit", {"old": bad_old, "new": "replacement"})) == [
        "edit_match_not_unique"
    ]


def test_suite_selection_and_manual_denominators() -> None:
    from benchmarks.tool_descriptions.run import manual_case
    from benchmarks.tool_descriptions.run import suite_cases

    assert len(suite_cases("basic")) == 12
    assert len(suite_cases("challenge")) == 11
    assert len(suite_cases("all")) == 23
    assert sum(not manual_case(case) for case in suite_cases("challenge")) == 9
    assert sum(manual_case(case) for case in suite_cases("all")) == 3


@pytest.mark.parametrize("variant", ["current", "clarified", "tool_names", "arg_names", "explicit_names"])
def test_observed_history_matches_exposed_tool_and_argument_names(variant: str) -> None:
    import ast
    import hashlib

    from benchmarks.tool_descriptions.challenges import EDIT_FIXTURES
    from benchmarks.tool_descriptions.run import observed_messages
    from benchmarks.tool_descriptions.run import suite_cases

    for case in suite_cases("observed"):
        history = observed_messages(case, variant)
        call = history[1].tool_calls[0]
        assert canonical(call, variant).name == "read_file"
        assert canonical(call, variant).arguments == {"path": case.expected["path"]}
        assert history[2].name == call.name and history[2].tool_call_id == call.id
        assert isinstance(history[2].content, str)
        result = ast.literal_eval(history[2].content)
        assert result["sha256"] == hashlib.sha256(EDIT_FIXTURES[case.name][0].encode()).hexdigest()
        assert result["newline_style"] == "CRLF"
