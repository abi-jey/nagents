"""Offline correctness checks for grading, usage accounting, and native fanout."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent

from . import telemetry
from .fixtures import grade
from .fixtures import scenarios
from .run import maximum_overlap
from .run import root_completion
from .run import trial
from .telemetry import Meter
from .telemetry import example_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import RetryConfig
    from nagents.types import ToolDefinition


def test_usage_repeated_events_not_double_counted_and_subsets_not_added() -> None:
    meter = Meter()
    record = meter.admit(0, True)
    usage = example_usage(100, 20, cached=60, reasoning=8)
    record.observe(ToolCallEvent(id="a", name="read_file", usage=usage))
    record.observe(ToolCallEvent(id="b", name="read_file", usage=usage))
    record.observe(TextDoneEvent(usage=usage))
    result = meter.summary()
    assert result["total_tokens"] == 120
    assert result["prompt_tokens"] == 100
    assert result["completion_tokens"] == 20
    assert result["uncached_prompt_tokens"] == 40
    assert result["reasoning_tokens"] == 8
    assert result["usage_complete"] is True


def test_interrupted_generation_retains_reported_usage_but_marks_incomplete() -> None:
    meter = Meter()
    record = meter.admit(1, True)
    record.observe(ToolCallEvent(id="a", name="read_file", usage=example_usage(50, 10)))
    assert meter.summary()["total_tokens"] == 60
    assert meter.summary()["usage_complete"] is False
    assert meter.summary()["generations_without_complete_usage"] == 1


def test_admission_reserves_requests_and_blocks_observed_token_overrun() -> None:
    meter = Meter(max_requests=1)
    meter.admit(0, True)
    with pytest.raises(RuntimeError, match="request_budget"):
        meter.admit(1, True)
    meter = Meter(max_observed_tokens=100)
    meter.admit(0, True).observe(TextDoneEvent(usage=example_usage(90, 20)))
    with pytest.raises(RuntimeError, match="observed_token_budget"):
        meter.admit(1, True)


def test_grader_requires_saved_correction_readback_and_no_other_mutation(tmp_path: Path) -> None:
    scenario = scenarios()[2]
    for name, text in scenario.files.items():
        (tmp_path / name).write_text(text)
    answer = json.dumps(scenario.expected)
    assert "incorrect_saved_content:deployment.json" in grade(scenario, tmp_path, answer, set())
    (tmp_path / "deployment.json").write_text(scenario.corrected_files["deployment.json"])
    assert grade(scenario, tmp_path, answer, set()) == ["missing_readback:deployment.json"]
    assert grade(scenario, tmp_path, answer, {"deployment.json"}) == []
    (tmp_path / "limits.json").write_text("{}")
    assert "unexpected_mutation:limits.json" in grade(scenario, tmp_path, answer, {"deployment.json"})
    (tmp_path / "extra.txt").write_text("unexpected")
    assert "unexpected_file:extra.txt" in grade(scenario, tmp_path, answer, {"deployment.json"})


def test_lifecycle_overlap_distinguishes_serial_and_concurrent_children() -> None:
    assert maximum_overlap([(0, 2), (2, 3)]) == 1
    assert maximum_overlap([(0, 3), (1, 4), (2, 5)]) == 3
    assert maximum_overlap([]) == 0


@pytest.mark.requires_posix
@pytest.mark.parametrize("strategy", ["direct", "delegated"])
def test_native_harness_instrumentation_includes_child_generations(
    strategy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[FakeLive] = []

    class FakeLive:
        def __init__(self, model: str, timeout: float, retry_config: RetryConfig) -> None:
            self.model = model
            self.index = len(created)
            self.requests = 0
            created.append(self)

        async def verify_model(self, force: bool = False) -> bool:
            return True

        async def close(self) -> None:
            pass

        async def generate(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            self.requests += 1
            usage = example_usage(100, 10, 20, 3)
            if self.requests == 1:
                if self.index == 0 and strategy == "delegated":
                    for number in range(2):
                        yield ToolCallEvent(
                            id=f"delegate-{number}",
                            name="delegate",
                            arguments={
                                "prompt": "Read ledgers/north.json and report its entries.",
                                "agent": "reviewer",
                            },
                            usage=usage,
                        )
                else:
                    await asyncio.sleep(0.01)
                    for number, path in enumerate(scenarios()[0].files):
                        yield ToolCallEvent(
                            id=f"read-{number}", name="read_file", arguments={"path": path}, usage=usage
                        )
                yield TextDoneEvent(usage=usage, finish_reason=FinishReason.TOOL_CALLS)
            else:
                yield TextDoneEvent(text=json.dumps(scenarios()[0].expected), usage=usage)

    monkeypatch.setattr(telemetry, "OpenAIProvider", FakeLive)
    result = asyncio.run(trial(scenarios()[0], strategy, "fake", 30))
    assert result["correct"] is True, result
    assert result["strategy_followed"] is True
    assert result["run_complete"] is True
    assert result["inspection_complete"] is True
    assert result["efficiency_eligible"] is True
    assert result["source_changed"] is False
    usage = result["usage"]
    assert isinstance(usage, dict)
    assert usage["provider_generations"] == sum(provider.requests for provider in created)
    assert usage["total_tokens"] == 110 * sum(provider.requests for provider in created)
    assert usage["usage_complete"] is True
    assert usage["child_generations"] == (4 if strategy == "delegated" else 0)


def test_only_noncompaction_root_done_qualifies() -> None:
    assert root_completion(DoneEvent(session_id="root"), "root", False)
    assert not root_completion(DoneEvent(session_id="child"), "root", False)
    assert not root_completion(DoneEvent(session_id="root"), "root", True)
    assert not root_completion(DoneEvent(session_id=None), "root", False)
