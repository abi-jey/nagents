"""Headless CLI paths stay independent of Textual and share the checkout version."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nagents._version import framework_version
from nagents.cli import _parser
from nagents.cli import main


def test_version_is_checkout_aware_and_does_not_import_textual(capsys: pytest.CaptureFixture[str]) -> None:
    sys.modules.pop("textual", None)
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"ngn (nagents {framework_version()})"
    assert "textual" not in sys.modules


def test_nonterminal_tui_exits_with_headless_guidance(tmp_path: Path) -> None:
    # Other tests may already have loaded Textual in this xdist worker. A fresh
    # process tests lazy imports without depending on collection/execution order.
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from nagents.cli import main; "
            "code = main(['--workspace', sys.argv[2]]); "
            "assert 'textual' not in sys.modules; sys.exit(code)",
            str(source),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stderr
    assert "The TUI needs a terminal" in result.stderr


def test_parser_keeps_resume_and_demo_options_available() -> None:
    args = _parser().parse_args(["--demo", "resume", "session-1"])
    assert args.demo
    assert args.session_id == "session-1"
