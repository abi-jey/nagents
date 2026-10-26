"""Human CLI status reflects process outcomes without changing JSON or run status."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nagents import cli
from nagents.events import DoneEvent
from nagents.events import ToolResultEvent
from nagents.harness import Harness
from nagents.harness.types import ToolOutput

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.harness.types import HarnessEvent


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_reports_structured_shell_outcomes_without_reclassifying_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], json_mode: bool
) -> None:
    events = [
        ToolResultEvent(id="exit", name="shell", result={"output": "check", "exit_code": 7, "timed_out": False}),
        ToolResultEvent(id="timeout", name="shell", result={"output": "partial", "exit_code": -15, "timed_out": True}),
        ToolResultEvent(id="success", name="shell", result={"output": "ok", "exit_code": 0, "timed_out": False}),
        ToolResultEvent(id="other", name="read_file", result={"exit_code": 7, "timed_out": True}),
        ToolResultEvent(id="string", name="shell", result="{'exit_code': 7, 'timed_out': True}"),
        ToolResultEvent(id="framework", name="shell", result={"timed_out": True}, error="Framework failure"),
    ]

    async def scripted(harness: Harness, prompt: str) -> AsyncIterator[HarnessEvent]:
        yield ToolOutput("exit", "shell", "Original streamed output\n")
        for event in events:
            yield event
        yield DoneEvent(session_id=harness.session_id)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Rendering uses synthetic tool events, not POSIX workspace-file discovery.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    monkeypatch.setattr(Harness, "run", scripted)
    args = ["run", "--demo", "--workspace", str(tmp_path), *(["--json"] if json_mode else []), "fixture"]
    assert cli.main(args) == 0, "Process return codes do not become an assistant-run failure"
    captured = capsys.readouterr()
    if json_mode:
        records = [json.loads(line) for line in captured.out.splitlines()]
        results = [record for record in records if record["event"] == "tool_result"]
        assert [record["result"] for record in results] == [event.result for event in events]
        assert [record["error"] for record in results] == [event.error for event in events]
        assert "[shell:" not in captured.err
    else:
        assert "Original streamed output\n" in captured.err
        assert "[shell: exit 7 in 0 ms]" in captured.err
        assert "[shell: timed out in 0 ms]" in captured.err
        assert captured.err.count("[shell completed in 0 ms]") == 2
        assert "[read_file completed in 0 ms]" in captured.err
        assert "[shell: Framework failure]" in captured.err
