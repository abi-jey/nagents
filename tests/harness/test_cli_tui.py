"""Headless CLI paths stay independent of Textual and share the checkout version."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nagents._version import framework_version
from nagents.cli import _parser


def cli_without_textual(*arguments: str) -> subprocess.CompletedProcess[str]:
    # Collection imports TUI tests in each xdist worker. A fresh interpreter
    # tests optional imports without depending on another test unimporting them.
    script = """
import sys
sys.path.insert(0, sys.argv.pop(1))
from nagents.cli import main
try:
    raise SystemExit(main(sys.argv[1:]))
finally:
    assert 'textual' not in sys.modules
"""
    source = Path(__file__).resolve().parents[2] / "src"
    return subprocess.run(
        [sys.executable, "-c", script, str(source), *arguments], capture_output=True, text=True, timeout=30
    )


def test_version_is_checkout_aware_and_does_not_import_textual() -> None:
    result = cli_without_textual("--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ngn (nagents {framework_version()})"


def test_nonterminal_tui_exits_with_headless_guidance(tmp_path: Path) -> None:
    result = cli_without_textual("--workspace", str(tmp_path))
    assert result.returncode == 2, result.stderr
    assert "The TUI needs a terminal" in result.stderr


def test_parser_keeps_resume_and_demo_options_available() -> None:
    args = _parser().parse_args(["--demo", "resume", "session-1"])
    assert args.demo
    assert args.session_id == "session-1"
