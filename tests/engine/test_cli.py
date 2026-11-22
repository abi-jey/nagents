"""Launch overrides and the read-only native launcher discovery probe."""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING

import pytest

from nagents.engine import cli
from nagents.engine import lifecycle

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.config import HarnessConfig

pytestmark = pytest.mark.requires_posix


def _expected(base: Path, workspace: Path) -> bytes:
    key = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()
    return os.fsencode(base / "engine" / "workspaces" / key) + b"\n"


def test_probe_uses_trusted_config_without_importing_server_or_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    workspace = tmp_path / "shared-workspace"
    project_dir = workspace / ".ngn"
    project_dir.mkdir(parents=True)
    (project_dir / "config.json").write_text('{"data_dir":"project-state"}', encoding="utf-8")
    explicit = tmp_path / "config.json"
    explicit.write_text('{"data_dir":"explicit-state"}', encoding="utf-8")
    environment = tmp_path / "environment-state"
    monkeypatch.setenv("NGN_DATA_DIR", str(environment))
    monkeypatch.setitem(sys.modules, "nagents.engine.lifecycle", None)
    argv = ["engine", "-C", str(workspace), "--print-state-dir", "--read-only", "--model", "example", "--no-wire-log"]

    with pytest.warns(UserWarning, match="Ignoring untrusted project config"):
        assert cli.main(argv) == 0
    assert capsysbinary.readouterr().out == _expected(environment, workspace)
    assert cli.main([*argv, "--trust-project"]) == 0
    assert capsysbinary.readouterr().out == _expected(project_dir / "project-state", workspace)
    assert cli.main([*argv, "--trust-project", "--config", str(explicit)]) == 0
    assert capsysbinary.readouterr().out == _expected(tmp_path / "explicit-state", workspace)
    selected = tmp_path / os.fsdecode(b"launcher-state-\xff ")
    assert cli.main([*argv, "--trust-project", "--config", str(explicit), "--state-dir", str(selected)]) == 0
    assert capsysbinary.readouterr().out == os.fsencode(selected) + b"\n"
    assert not environment.exists()
    assert not (project_dir / "project-state").exists()
    assert not (tmp_path / "explicit-state").exists()
    assert not selected.exists()
    assert not (tmp_path / "user-config").exists()


@pytest.mark.parametrize(
    ("environment", "settings", "flags", "expected"),
    [
        ("false", "false", ["--wire-log"], True),
        ("true", "true", ["--no-wire-log"], False),
        ("false", "true", [], True),
        ("true", "false", [], False),
    ],
)
def test_wire_log_overrides_existing_harness_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    settings: str,
    flags: list[str],
    expected: bool,
) -> None:
    monkeypatch.setenv("NGN_WIRE_LOG", environment)
    monkeypatch.setenv("NGN_MODEL", "environment-model")
    argv = ["engine", "-C", str(tmp_path)]
    path = tmp_path / "config.json"
    contents = f'{{"wire_log":{settings},"model":"file-model","read_only":true}}'
    path.write_text(contents, encoding="utf-8")
    captured: list[HarnessConfig] = []

    async def serve(config: HarnessConfig, *, port: int, lease: lifecycle.StdioLease | None) -> None:
        captured.append(config)

    monkeypatch.setattr(lifecycle, "serve_engine", serve)
    assert cli.main([*argv, "--config", str(path), *flags]) == 0
    assert captured[0].wire_log is expected and captured[0].read_only
    assert captured[0].model == "file-model"
    assert path.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize(
    "args",
    [
        ["--wire-log", "--no-wire-log"],
        ["--print-state-dir", "--stdio"],
    ],
)
def test_engine_flags_are_exclusive(args: list[str]) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(["engine", *args])
    assert result.value.code == 2


@pytest.mark.parametrize(
    ("args", "diagnostic"),
    [
        (["--provider-id", "missing"], b"Unknown provider connection"),
        (["--model", " "], b"model must not be empty"),
        (["--model", "x\n"], b"Model id must be a nonempty, bounded identifier"),
        (["--state-dir", "two\nlines"], b"cannot contain a line break"),
    ],
)
def test_invalid_probe_emits_no_discovery_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
    args: list[str],
    diagnostic: bytes,
) -> None:
    monkeypatch.setenv("NGN_DATA_DIR", str(tmp_path / "state"))
    assert cli.main(["engine", "-C", str(tmp_path), "--print-state-dir", *args]) == 2
    output = capsysbinary.readouterr()
    assert not output.out and diagnostic in output.err
    assert not (tmp_path / "state").exists()


def test_probe_refuses_unsafe_existing_directory_without_touching_it(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    public = tmp_path / "public-state"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    assert cli.main(["engine", "-C", str(tmp_path), "--state-dir", str(public), "--print-state-dir"]) == 2
    output = capsysbinary.readouterr()
    assert not output.out and b"unsafe" in output.err
    assert not list(public.iterdir())
