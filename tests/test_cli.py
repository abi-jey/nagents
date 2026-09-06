"""CLI contracts, without API credentials or a real terminal."""

import json
from pathlib import Path

import pytest

from nagents.cli import _event_record
from nagents.cli import _parser
from nagents.cli import _plain
from nagents.cli import main
from nagents.events import TextChunkEvent


def test_options_work_before_and_after_subcommand() -> None:
    args = _parser().parse_args(["--demo", "--model", "first", "run", "--json", "--model", "second", "hello"])
    assert args.demo
    assert args.json
    assert args.model == "second"
    assert args.prompt == ["hello"]


def test_output_does_not_execute_terminal_controls() -> None:
    assert _plain("hello\x1b]52;c;payload\x07\nworld\x9b") == "hello]52;c;payload\nworld"


def test_json_event_schema() -> None:
    record = _event_record(TextChunkEvent(chunk="hello"))
    assert record["event"] == "text_chunk"
    assert record["schema_version"] == 1
    assert record["chunk"] == "hello"


def test_invalid_workspace_is_actionable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--workspace", str(tmp_path / "missing"), "doctor"]) == 2
    assert "Workspace is not a directory" in capsys.readouterr().err


def test_conflicting_resume_flags_across_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--continue", "run", "--resume", "other-session", "hello"]) == 2
    assert "either --continue or --resume" in capsys.readouterr().err


def test_missing_credentials_are_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("NGN_UNSET_TEST_KEY", raising=False)
    result = main(["run", "--workspace", str(tmp_path), "--api-key-env", "NGN_UNSET_TEST_KEY", "hello"])
    assert result in {1, 2}
    assert "NGN_UNSET_TEST_KEY" in capsys.readouterr().err


def test_demo_json_is_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert main(["run", "--workspace", str(tmp_path), "--demo", "--json", "hello"]) == 0
    output = capsys.readouterr()
    records = [json.loads(line) for line in output.out.splitlines()]
    assert records
    assert all(record["schema_version"] == 1 for record in records)
    assert any(record["event"] == "text_chunk" for record in records)
    assert any(record["event"] == "done" for record in records)


def test_demo_sessions_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    common = ["--workspace", str(tmp_path), "--demo"]
    assert main([*common, "run", "first prompt"]) == 0
    capsys.readouterr()
    assert main([*common, "sessions"]) == 0
    assert "first prompt" in capsys.readouterr().out
    assert main([*common, "run", "--continue", "second prompt"]) == 0
    assert "Session:" in capsys.readouterr().err
