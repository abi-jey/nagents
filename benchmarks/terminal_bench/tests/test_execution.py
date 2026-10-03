"""Runner outcomes use the shipping Harness with scripted native provider events."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from benchmarks.terminal_bench import runner
from benchmarks.terminal_bench.summarize import summarize_events

from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.provider.openai import OpenAIProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import HarnessConfig
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition

pytestmark = pytest.mark.requires_posix


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["recovered", "fatal", "retry_then_fatal", "round_limit", "length", "cancelled", "timeout"]
)
async def test_runner_preserves_error_evidence_without_misclassifying_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(runner, "PRIVATE_ROOT", tmp_path / "private")
    monkeypatch.setattr(runner, "container_gate", lambda: None)  # No approvals/tools are requested in this test.
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "synthetic-access", "account_id": "fixture", "residency": ""}))
    credentials.chmod(0o600)
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("Exercise the native loop without a network request.")
    args = argparse.Namespace(
        output=str(tmp_path / "output"),
        credentials=str(credentials),
        instruction=str(instruction),
        model="gpt-6-astra",
        timeout=1 if outcome == "timeout" else 5,
    )
    configure = runner.configuration

    def configuration(path: Path, model: str, private: Path) -> HarnessConfig:
        config = configure(path, model, private)
        if outcome == "round_limit":
            config.max_tool_rounds = 2
        return config

    monkeypatch.setattr(runner, "configuration", configuration)
    entered = asyncio.Event()
    calls = 0
    provider_instances: list[OpenAIProvider] = []

    async def verify(provider: OpenAIProvider, force: bool = False) -> bool:
        return True

    async def generate(
        provider: OpenAIProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        nonlocal calls
        calls += 1
        provider_instances.append(provider)
        entered.set()
        if outcome in {"cancelled", "timeout"}:
            await asyncio.Event().wait()
        elif calls == 1 or outcome in {"retry_then_fatal", "round_limit"}:
            yield ErrorEvent(
                message="Synthetic provider observation",
                recoverable=outcome != "fatal" and (outcome != "retry_then_fatal" or calls == 1),
            )
        else:
            yield TextDoneEvent(
                text="Recovered answer", finish_reason=FinishReason.LENGTH if outcome == "length" else FinishReason.STOP
            )

    monkeypatch.setattr(OpenAIProvider, "verify_model", verify)
    monkeypatch.setattr(OpenAIProvider, "generate", generate)
    task = asyncio.create_task(runner.run(args))
    if outcome == "cancelled":
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == (0 if outcome == "recovered" else 1)
    summary = summarize_events(tmp_path / "output/events.jsonl")
    expected = {
        "recovered": "completed",
        "length": "incomplete",
        "cancelled": "cancelled",
        "timeout": "agent_timeout",
    }.get(outcome, "harness_error")
    assert summary["status"] == expected
    assert not credentials.exists()
    assert provider_instances and all(type(provider) is OpenAIProvider for provider in provider_instances)
    counts = summary["counts"]
    assert isinstance(counts, dict)
    if outcome == "recovered":
        assert calls == 2 and counts["error"] == 1
        assert counts["recoverable_errors"] == 1
        recovered = summary["recoverable_errors"]
        assert isinstance(recovered, list) and isinstance(recovered[0], dict)
        event = recovered[0]["event"]
        assert isinstance(event, dict) and event["recoverable"] is True
        assert not summary["failures"]
    elif outcome in {"fatal", "retry_then_fatal", "round_limit"}:
        assert counts["terminal_errors"] == 1
        assert summary["failures"]
    assert summary["completion_is_verifier_success"] is False
