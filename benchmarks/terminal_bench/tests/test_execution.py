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
    "outcome",
    [
        "recovered",
        "fatal",
        "retry_then_fatal",
        "round_limit",
        "length",
        "cancelled",
        "timeout",
        "recovered_close_failure",
        "fatal_close_failure",
        "cancelled_close_failure",
        "credentials_cleanup_failure",
    ],
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
        timeout=30 if outcome == "timeout" else 5,
    )
    configure = runner.configuration

    def configuration(path: Path, model: str, private: Path) -> HarnessConfig:
        config = configure(path, model, private)
        if outcome == "round_limit":
            config.max_tool_rounds = 2
        return config

    monkeypatch.setattr(runner, "configuration", configuration)
    if outcome.endswith("_close_failure"):
        close_provider = OpenAIProvider.close

        async def fail_close(provider: OpenAIProvider) -> None:
            await close_provider(provider)
            raise RuntimeError("Synthetic cleanup failed: synthetic-access")

        monkeypatch.setattr(OpenAIProvider, "close", fail_close)
    original_unlink = type(credentials).unlink
    if outcome == "credentials_cleanup_failure":

        def fail_unlink(path: Path, missing_ok: bool = False) -> None:
            if path == credentials:
                raise PermissionError("Synthetic credential cleanup failure")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(type(credentials), "unlink", fail_unlink)
    entered = asyncio.Event()
    calls = 0
    provider_instances: list[OpenAIProvider] = []
    deadlines: list[asyncio.Timeout] = []
    native_timeout = asyncio.timeout

    def record_timeout(seconds: float) -> asyncio.Timeout:
        deadline = native_timeout(seconds)
        deadlines.append(deadline)
        return deadline

    if outcome == "timeout":
        monkeypatch.setattr(asyncio, "timeout", record_timeout)

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
        if outcome == "timeout":
            # Expire the real runner deadline only after generation starts.
            # Local initialization must not race a one-second wall-clock budget.
            deadlines[0].reschedule(asyncio.get_running_loop().time())
        if outcome in {"cancelled", "cancelled_close_failure", "timeout"}:
            await asyncio.Event().wait()
        elif calls == 1 or outcome in {"retry_then_fatal", "round_limit"}:
            yield ErrorEvent(
                message="Synthetic provider observation",
                recoverable=outcome not in {"fatal", "fatal_close_failure"}
                and (outcome != "retry_then_fatal" or calls == 1),
            )
        else:
            yield TextDoneEvent(
                text="Recovered answer", finish_reason=FinishReason.LENGTH if outcome == "length" else FinishReason.STOP
            )

    monkeypatch.setattr(OpenAIProvider, "verify_model", verify)
    monkeypatch.setattr(OpenAIProvider, "generate", generate)
    task = asyncio.create_task(runner.run(args))
    if outcome in {"cancelled", "cancelled_close_failure"}:
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
        "cancelled_close_failure": "cancelled",
        "recovered_close_failure": "cleanup_error",
        "credentials_cleanup_failure": "cleanup_error",
        "timeout": "agent_timeout",
    }.get(outcome, "harness_error")
    assert summary["status"] == expected
    if outcome == "credentials_cleanup_failure":
        assert credentials.is_file()
        original_unlink(credentials)
    else:
        assert not credentials.exists()
    if "failure" in outcome:
        failures = summary["failures"]
        assert isinstance(failures, list)
        assert any(
            item["kind"] == "benchmark_exception" and item["event"].get("phase") == "cleanup" for item in failures
        )
        assert "synthetic-access" not in (tmp_path / "output/events.jsonl").read_text()
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
