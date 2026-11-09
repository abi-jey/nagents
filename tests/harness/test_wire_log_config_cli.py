"""Wire logging configuration is opt-in, strict, and follows normal precedence."""

import argparse
import json
from pathlib import Path
from typing import cast

import pytest

from nagents import cli
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config


def test_wire_log_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NGN_WIRE_LOG", raising=False)
    assert HarnessConfig(workspace=tmp_path).wire_log is False
    assert load_config(tmp_path).wire_log is False
    assert not hasattr(cli._parser().parse_args([]), "wire_log")


@pytest.mark.parametrize("value", [True, False])
def test_wire_log_json_boolean(tmp_path: Path, value: bool) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"wire_log": value}))
    assert load_config(tmp_path, path).wire_log is value
    assert HarnessConfig(workspace=tmp_path, wire_log=value).wire_log is value


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_wire_log_rejects_non_booleans(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="wire_log must be a boolean"):
        HarnessConfig(workspace=tmp_path, wire_log=cast("bool", value))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"wire_log": value}))
    with pytest.raises(ValueError, match="wire_log must be a boolean"):
        load_config(tmp_path, path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("False", False), ("0", False)],
)
def test_wire_log_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("NGN_WIRE_LOG", value)
    assert load_config(tmp_path).wire_log is expected


@pytest.mark.parametrize("value", ["", "yes", "no", "2", " true "])
def test_wire_log_invalid_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("NGN_WIRE_LOG", value)
    with pytest.raises(ValueError, match="NGN_WIRE_LOG must be true/false or 1/0"):
        load_config(tmp_path)


def test_wire_log_file_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGN_WIRE_LOG", "true")
    user = tmp_path / "user-config/ngn/config.json"
    user.parent.mkdir(parents=True)
    user.write_text('{"wire_log": false}')
    assert load_config(tmp_path).wire_log is False
    project = tmp_path / ".ngn/config.json"
    project.parent.mkdir()
    project.write_text('{"wire_log": true}')
    assert load_config(tmp_path, trust_project=True).wire_log is True
    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"wire_log": false}')
    assert load_config(tmp_path, explicit, trust_project=True).wire_log is False


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--wire-log", "run", "hello"], True),
        (["run", "--no-wire-log", "hello"], False),
        (["--wire-log", "run", "--no-wire-log", "hello"], False),
        (["--no-wire-log", "run", "--wire-log", "hello"], True),
        (["--wire-log", "--no-wire-log"], False),
        (["serve", "--wire-log"], True),
    ],
)
def test_wire_log_cli_flags(flags: list[str], expected: bool) -> None:
    assert cli._parser().parse_args(flags).wire_log is expected


@pytest.mark.parametrize(
    ("environment", "configured", "flags", "expected"),
    [
        ("false", False, ["--wire-log"], True),
        ("true", True, ["--no-wire-log"], False),
        ("false", True, [], True),
        ("true", False, [], False),
    ],
)
def test_wire_log_cli_overrides_loaded_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    configured: bool,
    flags: list[str],
    expected: bool,
) -> None:
    monkeypatch.setenv("NGN_WIRE_LOG", environment)
    path = tmp_path / "config.json"
    original = json.dumps({"wire_log": configured})
    path.write_text(original)
    captured: list[HarnessConfig] = []

    class StubHarness:
        def __init__(self, config: HarnessConfig) -> None:
            captured.append(config)

    async def headless(harness: StubHarness, args: argparse.Namespace) -> int:
        return 0

    monkeypatch.setattr(cli, "Harness", StubHarness)
    monkeypatch.setattr(cli, "_headless", headless)
    assert cli.main(["-C", str(tmp_path), "--config", str(path), "doctor", *flags]) == 0
    assert captured[0].wire_log is expected
    assert path.read_text() == original
