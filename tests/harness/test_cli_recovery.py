"""Headless exit status follows the final root outcome, retaining retry evidence."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents import cli
from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness.tools import CodingTools
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.types import HarnessEvent
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.fixture(autouse=True)
def portable_workspace_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # These cases execute the real run/cleanup loop, without workspace-file tools.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    monkeypatch.setattr(CodingTools, "discover_skills", lambda self: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("outcome", ["recovered", "fatal", "retry_then_fatal", "round_limit", "length"])
async def test_actual_harness_cli_classifies_recovery_and_terminal_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], json_mode: bool, outcome: str
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1 or outcome in {"retry_then_fatal", "round_limit"}:
            yield ErrorEvent(
                message="Observed provider error",
                recoverable=outcome != "fatal" and (outcome != "retry_then_fatal" or len(provider.requests) == 1),
            )
        else:
            yield TextDoneEvent(
                text="Final answer",
                finish_reason=FinishReason.LENGTH if outcome == "length" else FinishReason.STOP,
            )

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    if outcome == "round_limit":
        harness.agent.max_tool_rounds = 2
    args = cli._parser().parse_args(["run", *(["--json"] if json_mode else []), "fixture"])
    code = await cli._headless(harness, args)
    assert code == (0 if outcome == "recovered" else 1)
    assert len(providers[0].requests) == (1 if outcome == "fatal" else 2)
    assert harness._closed and providers[0].closed
    captured = capsys.readouterr()
    if json_mode:
        records = [json.loads(line) for line in captured.out.splitlines()]
        errors = [record for record in records if record["event"] == "error"]
        assert errors[0]["message"] == "Observed provider error"
        assert errors[0]["recoverable"] is (outcome != "fatal")
        assert records[-1]["event"] == "done" and records[-1]["session_id"] == harness.session_id
        assert records[-1]["finish_reason"] == (
            "stop" if outcome == "recovered" else "length" if outcome == "length" else "unknown"
        )
        if outcome in {"fatal", "retry_then_fatal", "round_limit"}:
            assert errors[-1]["recoverable"] is False
    else:
        assert "Error: Observed provider error" in captured.err
        if outcome == "round_limit":
            assert "Max tool rounds (2) exceeded" in captured.err
        if outcome in {"recovered", "length"}:
            assert "Final answer" in captured.out


@pytest.mark.asyncio
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("ending", ["missing", "other_session", "compaction", "compaction_then_root"])
async def test_cli_requires_successful_root_completion_outside_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_mode: bool, ending: str
) -> None:
    async def unused(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        raise AssertionError("Only the malformed-stream boundary is synthetic")
        yield TextDoneEvent()

    async def interrupted_stream(harness: Harness, prompt: str) -> AsyncIterator[HarnessEvent]:
        yield ErrorEvent(message="Retry evidence", recoverable=True)
        if ending.startswith("compaction"):
            yield CompactionStartedEvent(session_id=harness.session_id, compaction_session_id="compact")
            yield DoneEvent(session_id=harness.session_id)
            yield CompactionDoneEvent(session_id=harness.session_id, compaction_session_id="compact")
            if ending == "compaction_then_root":
                yield DoneEvent(session_id=harness.session_id)
        elif ending == "other_session":
            yield DoneEvent(session_id="another-root")

    harness, providers = setup_harness(tmp_path, monkeypatch, unused)
    monkeypatch.setattr(Harness, "run", interrupted_stream)
    args = cli._parser().parse_args(["run", *(["--json"] if json_mode else []), "fixture"])
    assert await cli._headless(harness, args) == (0 if ending == "compaction_then_root" else 1)
    assert not providers[0].requests and providers[0].closed


@pytest.mark.asyncio
async def test_cancelled_headless_run_propagates_and_joins_actual_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        started.set()
        try:
            await asyncio.Event().wait()
            yield TextDoneEvent(text="Must not complete")
        finally:
            stopped.set()

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    args = cli._parser().parse_args(["run", "--json", "fixture"])
    task = asyncio.create_task(cli._headless(harness, args))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and harness._closed and providers[0].closed


def test_cli_interrupt_retains_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(harness: Harness, args: object) -> int:
        await harness.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_headless", interrupted)
    assert cli.main(["run", "--demo", "--workspace", str(tmp_path), "fixture"]) == 130
