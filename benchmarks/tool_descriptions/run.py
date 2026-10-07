"""One-turn schema comprehension benchmark; never executes generated tool calls."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness.providers import ProviderProfile
from nagents.harness.tools import _validate
from nagents.provider.openai import OpenAIProvider
from nagents.types import Message
from nagents.types import RetryConfig
from nagents.types import ToolCall

if TYPE_CHECKING:
    from nagents.types import JsonValue
    from nagents.types import ToolDefinition


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    tool: str
    expected: dict[str, JsonValue] = field(default_factory=dict)
    bounded_read: bool = False


CASES = (
    Case(
        "read_lines",
        "Read exactly lines 21 through 30 inclusive of src/app.py.",
        "read_file",
        {"path": "src/app.py", "start_line": 21, "limit": 10},
    ),
    Case(
        "literal_search",
        "Find at most 7 matching lines containing the literal text a+b[0] in src/.",
        "search",
        {"query": "a+b[0]", "path": "src", "limit": 7},
    ),
    Case(
        "filename_glob",
        "Find Python file paths recursively below src/, at most 20 paths.",
        "find",
        {"pattern": "**/*.py", "path": "src", "limit": 20},
    ),
    Case(
        "edit_existing",
        "You have already read config.txt in this turn, and it has not changed. Its unique literal line is enabled=false. Replace just enabled=false with enabled=true.",
        "edit",
        {"path": "config.txt", "old": "enabled=false", "new": "enabled=true"},
    ),
    Case(
        "new_file",
        "Create the nonexistent notes.txt in the existing workspace root, containing exactly hello followed by one newline.",
        "write",
        {"path": "notes.txt", "content": "hello\n"},
    ),
    Case(
        "read_before_edit",
        "Existing config.txt has not been read in this session. Change its setting enabled=false to enabled=true. Take the first appropriate step.",
        "read_file",
        {"path": "config.txt"},
    ),
    Case(
        "bounded_sample",
        "Read the first 1500 lines of data.txt (a 20 KB UTF-8 file). If a single read cannot cover this, take the largest valid first chunk.",
        "read_file",
        {"path": "data.txt", "start_line": 1, "limit": 1000},
        True,
    ),
    Case(
        "shell_default",
        "Run exactly python -m pytest using the configured default shell timeout. Do not override the timeout with another duration.",
        "shell",
        {"command": "python -m pytest", "timeout": 0},
    ),
    Case(
        "shell_explicit",
        "Run exactly python -m pytest with a 15-second timeout.",
        "shell",
        {"command": "python -m pytest", "timeout": 15},
    ),
    Case(
        "delegate",
        "Start a background child using the default assistant profile. Give it exactly this self-contained task: Inspect src/parser.py for incorrect handling of empty strings and report findings; do not modify files.",
        "delegate",
        {
            "prompt": "Inspect src/parser.py for incorrect handling of empty strings and report findings; do not modify files.",
            "agent": "assistant",
        },
    ),
    Case(
        "task_status",
        "A previously delegated child task id is task-123. Which callable tool can poll its status? Call it if available; if none is exposed, explain that limitation without invoking a different tool.",
        "",
    ),
    Case(
        "search_file",
        "Find up to 3 lines containing the literal text ERROR.* in logs/app.log; do not interpret the text as a regular expression.",
        "search",
        {"query": "ERROR.*", "path": "logs/app.log", "limit": 3},
    ),
)

CLARIFIED = {
    "read_file": "Read existing UTF-8 text with numbered lines and an edit snapshot. start_line is 1-based; limit is a line COUNT from 1 through 1000 (default 200), not an ending line. Reading a chunk still requires the entire file to fit the configured byte limit.",
    "find": "Find FILE PATHS by glob relative to path, not file contents. Use **/*.py for recursive Python files. limit is a result count from 1 through 1000.",
    "search": "Search file CONTENTS for case-sensitive LITERAL text, never regex. path is a file or directory. query is 1..1000 characters and limit is 1..1000 matching lines.",
    "edit": "Edit an EXISTING file after reading it in this session. Replace exactly one nonempty literal old string with new. Requires an unchanged read snapshot and approval; never use write to overwrite.",
    "write": "CREATE a nonexistent UTF-8 file in an existing directory after approval. Cannot overwrite existing files; use read_file then edit for those.",
    "shell": "Run a POSIX shell command after approval. timeout is seconds: omit it or use 0 to inherit the configured default (60 seconds here); a positive override must not exceed that default. Not sandboxed.",
    "delegate": "Start a background child and return task_id immediately. prompt must be self-contained because children do not receive parent history. Omit agent for the assistant profile; never invent profiles or pass an empty name. Results arrive automatically; this tool does not poll status.",
}
NAME_MAP = {
    "read_file": "read_text_lines",
    "find": "find_paths_by_glob",
    "search": "search_literal_text",
    "edit": "replace_exact_text",
    "write": "create_new_text_file",
    "shell": "run_shell_command",
    "delegate": "start_background_task",
}
ARG_MAP = {
    "read_file": {"path": "file_path", "start_line": "first_line_number", "limit": "line_count"},
    "find": {"pattern": "path_glob", "path": "directory_path", "limit": "max_paths"},
    "search": {"query": "literal_text", "path": "file_or_directory", "limit": "max_matching_lines"},
    "edit": {"path": "file_path", "old": "exact_old_text", "new": "replacement_text"},
    "write": {"path": "new_file_path", "content": "utf8_content"},
    "shell": {"command": "shell_command", "timeout": "timeout_seconds"},
    "delegate": {"prompt": "self_contained_task", "agent": "agent_profile"},
}
ARG_CLARIFIED = {
    "read_file": {
        "path": "Existing workspace file.",
        "start_line": "1-based first line, default 1.",
        "limit": "Line count, not ending line; 1..1000, default 200.",
    },
    "find": {
        "pattern": "Path glob, not content text.",
        "path": "Directory to search.",
        "limit": "Maximum returned paths, 1..1000.",
    },
    "search": {
        "query": "Literal text, not regex.",
        "path": "File or directory to search.",
        "limit": "Maximum matching lines, 1..1000.",
    },
    "edit": {
        "path": "Already-read existing workspace file.",
        "old": "Exact unique literal without displayed line numbers; retain original line endings.",
        "new": "Literal replacement; empty deletes the match.",
    },
    "shell": {
        "command": "POSIX shell command.",
        "timeout": "Seconds: omit or 0 for configured default; positive override cannot exceed default.",
    },
    "delegate": {
        "prompt": "Self-contained task; no inherited conversation.",
        "agent": "Omit for assistant; only an exact configured profile is accepted.",
    },
}
DEFAULTS: dict[str, JsonValue] = {"start_line": 1, "limit": 200, "timeout": 0, "agent": "assistant", "path": "."}


def rename_words(text: str, names: dict[str, str]) -> str:
    if not names:
        return text
    return re.sub(
        r"\b(?:" + "|".join(re.escape(key) for key in names) + r")\b", lambda match: names[match.group()], text
    )


def variant_tools(tools: list[ToolDefinition], variant: str) -> list[ToolDefinition]:
    result = copy.deepcopy(tools)
    for tool in result:
        tool.func = None
        if variant == "clarified" and tool.name in CLARIFIED:
            tool.description += "\n" + CLARIFIED[tool.name]
            for name, description in ARG_CLARIFIED.get(tool.name, {}).items():
                schema = tool.parameters.get("properties", {}).get(name)
                if schema is not None:
                    schema["description"] = schema.get("description", "") + " " + description
        tool_names = NAME_MAP if variant in {"explicit_names", "tool_names"} else {}
        argument_names = ARG_MAP.get(tool.name, {}) if variant in {"explicit_names", "arg_names"} else {}
        replacements = {**tool_names, **argument_names}
        tool.description = rename_words(tool.description, replacements)
        for schema in tool.parameters.get("properties", {}).values():
            if "description" in schema:
                schema["description"] = rename_words(schema["description"], replacements)
        tool.parameters["properties"] = {
            argument_names.get(k, k): v for k, v in tool.parameters.get("properties", {}).items()
        }
        tool.parameters["required"] = [argument_names.get(k, k) for k in tool.parameters.get("required", [])]
        tool.name = tool_names.get(tool.name, tool.name)
    return result


def canonical(call: ToolCall, variant: str) -> ToolCall:
    tool_name = (
        next((k for k, v in NAME_MAP.items() if v == call.name), call.name)
        if variant in {"explicit_names", "tool_names"}
        else call.name
    )
    reverse = (
        {v: k for k, v in ARG_MAP.get(tool_name, {}).items()} if variant in {"explicit_names", "arg_names"} else {}
    )
    return ToolCall(call.id, tool_name, {reverse.get(k, k): v for k, v in call.arguments.items()})


def grade(case: Case, calls: list[ToolCall], tools: list[ToolDefinition], variant: str, text: str = "") -> list[str]:
    failures: list[str] = []
    schemas = {tool.name: tool for tool in tools}
    for call in calls:
        if call.name not in schemas:
            failures.append("unknown_tool")
        else:
            try:
                _validate(call.arguments, schemas[call.name].parameters)
            except ValueError:
                failures.append("invalid_schema")
    if not case.tool:
        if calls or not text.strip():
            failures.append("expected_text_without_tools")
        return failures
    if len(calls) != 1:
        return [*failures, "expected_one_call"]
    call = canonical(calls[0], variant)
    if call.name != case.tool:
        return [*failures, "wrong_tool"]
    for key, expected in case.expected.items():
        value = call.arguments.get(key, DEFAULTS.get(key))
        if key == "path" and isinstance(value, str):
            value = value.removeprefix("./").rstrip("/")
        if case.name == "filename_glob" and key == "pattern" and value == "*.py":
            continue
        if value != expected:
            failures.append(f"wrong_argument:{key}")
    if case.bounded_read:
        limit = call.arguments.get("limit", 200)
        if type(limit) is not int or not 1 <= limit <= 1000:
            failures.append("read_limit_out_of_bounds")
    if case.name.startswith("challenge_"):
        from benchmarks.tool_descriptions.challenges import grade_challenge

        failures.extend(grade_challenge(case, call))
    return failures


def manual_case(case: Case) -> bool:
    if not case.tool:
        return True
    if case.name.startswith("challenge_"):
        from benchmarks.tool_descriptions.challenges import is_manual

        return is_manual(case)
    return False


def suite_cases(suite: str) -> tuple[Case, ...]:
    if suite == "basic":
        return CASES
    from benchmarks.tool_descriptions.challenges import get_cases

    challenges = get_cases(Case)
    if suite == "observed":
        prompts = {
            "challenge_numbered_crlf": "Change the host to 127.0.0.1 and port to 9090 together, preserving line endings and all other text. The file has not changed since the read.",
            "challenge_delete_exact_line": "Remove the entire allow=write line including its line terminator; leave no blank line and preserve the other lines. The file has not changed since the read.",
        }
        return tuple(replace(case, prompt=prompts[case.name]) for case in challenges if case.name in prompts)
    return (*CASES, *challenges) if suite == "all" else challenges


def observed_messages(case: Case, variant: str) -> list[Message]:
    from benchmarks.tool_descriptions.challenges import EDIT_FIXTURES

    before, _ = EDIT_FIXTURES[case.name]
    path = str(case.expected["path"])
    name = NAME_MAP["read_file"] if variant in {"tool_names", "explicit_names"} else "read_file"
    path_argument = ARG_MAP["read_file"]["path"] if variant in {"arg_names", "explicit_names"} else "path"
    lines = before.splitlines()
    result = {
        "path": path,
        "sha256": hashlib.sha256(before.encode()).hexdigest(),
        "content": "\n".join(f"{index}: {line}" for index, line in enumerate(lines, 1)),
        "newline_style": "CRLF",
        "newline_counts": {"LF": 0, "CRLF": before.count("\r\n"), "CR": 0},
        "total_lines": len(lines),
        "truncated": False,
        "instructions": "",
    }
    return [
        Message("user", f"Read {path} before I request a change."),
        Message("assistant", tool_calls=[ToolCall("observed-read", name, {path_argument: path})]),
        Message("tool", str(result), tool_call_id="observed-read", name=name),
    ]


_SCHEMA_CAPTURE_LOCK = asyncio.Lock()


async def shipping_tools(root: Path) -> list[ToolDefinition]:
    async with _SCHEMA_CAPTURE_LOCK:
        return await _shipping_tools(root)


async def _shipping_tools(root: Path) -> list[ToolDefinition]:
    previous = {key: os.environ.get(key) for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    os.environ["XDG_DATA_HOME"] = str(root / "data")
    harness = Harness(
        HarnessConfig(
            workspace=root, data_dir=root / "state", providers={"": ProviderProfile(kind="openai", auth="api-key")}
        )
    )
    try:
        await harness.initialize()
        # Detach bound functions before copying to avoid copying runtime resources.
        from nagents.types import ToolDefinition

        return [
            ToolDefinition(tool.name, tool.description, copy.deepcopy(tool.parameters))
            for tool in harness.agent.tool_registry.get_all()
        ]
    finally:
        await harness.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def private_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


async def run(
    model: str, concurrency: int, output: Path, repeats: int, variant: str, suite: str = "basic"
) -> dict[str, object]:
    cases = suite_cases(suite)
    suite_sha256 = hashlib.sha256(json.dumps([asdict(case) for case in cases], sort_keys=True).encode()).hexdigest()
    source = Path(__file__).read_bytes() + Path(__file__).with_name("challenges.py").read_bytes()
    runner_sha256 = hashlib.sha256(source).hexdigest()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="ngn-tool-eval-") as temporary:
        tools = variant_tools(await shipping_tools(Path(temporary)), variant)
    schema = [{"name": tool.name, "description": tool.description, "parameters": tool.parameters} for tool in tools]
    digest = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    private_json(
        output / "schemas.json",
        {
            "sha256": digest,
            "variant": variant,
            "tools": schema,
            "tool_names": NAME_MAP if variant in {"explicit_names", "tool_names"} else {},
            "argument_names": ARG_MAP if variant in {"explicit_names", "arg_names"} else {},
        },
    )
    schema_chars = len(json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    usage_records: list[dict[str, int]] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(case: Case, repeat: int) -> dict[str, object]:
        async with semaphore:
            started = time.monotonic()
            calls: list[ToolCall] = []
            response_text = ""
            errors: list[str] = []
            done = False
            usage = {
                name: 0
                for name in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                    "audio_tokens",
                )
            }
            usage_reported = False
            try:
                async with (
                    asyncio.timeout(150),
                    OpenAIProvider(model=model, timeout=120, retry_config=RetryConfig(max_retries=0)) as provider,
                ):
                    messages = [
                        Message(
                            "system",
                            "Choose the next appropriate action using the available tools. All paths refer to a fictional workspace. Only the assistant agent profile exists. The configured shell timeout is 60 seconds. Do not invent tools or arguments. Return at most one tool call, or explain if no appropriate tool exists.",
                        ),
                        *(observed_messages(case, variant) if suite == "observed" else []),
                        Message("user", case.prompt),
                    ]
                    async for event in provider.generate(messages, tools=tools):
                        if event.usage.has_usage():
                            usage_reported = True
                            for name in usage:
                                # Providers attach the same cumulative usage to multiple events.
                                usage[name] = max(usage[name], int(getattr(event.usage, name)))
                        if isinstance(event, ToolCallEvent):
                            calls.append(ToolCall(event.id, event.name, event.arguments))
                        elif isinstance(event, TextDoneEvent):
                            done = event.finish_reason in {FinishReason.STOP, FinishReason.TOOL_CALLS}
                            response_text = event.text
                        elif isinstance(event, ErrorEvent):
                            errors.append(event.code or "provider_error")
            except Exception as error:
                errors.append(type(error).__name__)
            failures = grade(case, calls, tools, variant, response_text)
            if not done:
                errors.append("missing_done")
            usage_records.append(usage)
            result = {
                "usage": usage,
                "usage_reported": usage_reported,
                "schema_chars": schema_chars,
                "case": case.name,
                "repeat": repeat,
                "prompt": case.prompt,
                "seconds": round(time.monotonic() - started, 3),
                "passed": (not failures and not errors) if not manual_case(case) else None,
                "structural_pass": not failures and not errors,
                "manual_review": manual_case(case),
                "failures": failures,
                "errors": errors,
                "calls": [{"name": c.name, "arguments": c.arguments} for c in calls],
                "text": response_text,
            }
            private_json(output / f"{case.name}-{repeat}.json", result)
            return result

    started = time.monotonic()
    results = await asyncio.gather(
        *(evaluate(case, repeat) for repeat in range(repeats) for case in suite_cases(suite))
    )
    summary: dict[str, object] = {
        "model": model,
        "variant": variant,
        "suite": suite,
        "suite_sha256": suite_sha256,
        "runner_sha256": runner_sha256,
        "schema_sha256": digest,
        "schema_chars": schema_chars,
        "schema_source": "initialized non-demo Harness; isolated stores; deferred api-key provider with no generation",
        "usage": {name: sum(record[name] for record in usage_records) for name in usage_records[0]}
        if usage_records
        else {},
        "cases_with_usage": sum(result["usage_reported"] is True for result in results),
        "concurrency": concurrency,
        "repeats": repeats,
        "cases": len(results),
        "automated_cases": sum(result["manual_review"] is False for result in results),
        "structural_passed": sum(result["structural_pass"] is True for result in results),
        "manual_review_cases": sum(result["manual_review"] is True for result in results),
        "passed": sum(result["passed"] is True for result in results),
        "seconds": round(time.monotonic() - started, 3),
        "results": results,
        "limitations": "One-turn fictional scenarios; tools are never executed. Exact-choice rubric is intentionally narrow. Does not measure task completion, retry recovery, real filesystem effects, or causal superiority without repeated paired runs.",
    }
    private_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--suite", choices=("basic", "challenge", "all", "observed"), default="basic")
    parser.add_argument(
        "--variant", choices=("current", "clarified", "explicit_names", "tool_names", "arg_names"), default="current"
    )
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16 or not 1 <= args.repeats <= 20:
        parser.error("concurrency must be 1..16 and repeats 1..20")
    summary = asyncio.run(run(args.model, args.concurrency, args.output, args.repeats, args.variant, args.suite))
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, indent=2))


if __name__ == "__main__":
    main()
