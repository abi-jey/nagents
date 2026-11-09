"""Headless CLI paths stay independent of Textual and share the checkout version."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from nagents._version import framework_version
from nagents.cli import _parser
from nagents.cli import main

if TYPE_CHECKING:
    from pathlib import Path


def test_version_is_checkout_aware_and_does_not_import_textual(capsys: pytest.CaptureFixture[str]) -> None:
    sys.modules.pop("textual", None)
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"ngn (nagents {framework_version()})"
    assert "textual" not in sys.modules


def test_nonterminal_tui_exits_with_headless_guidance(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--workspace", str(tmp_path)]) == 2
    assert "The TUI needs a terminal" in capsys.readouterr().err
    assert "textual" not in sys.modules


def test_parser_keeps_resume_and_demo_options_available() -> None:
    args = _parser().parse_args(["--demo", "resume", "session-1"])
    assert args.demo
    assert args.session_id == "session-1"
