"""The audit must report coverage and reject incomplete or vulnerable results."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/report_dependency_audit.py"


def report(tmp_path: Path, tool: str, data: object, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), tool, "test input", str(exit_code), str(path)],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


@pytest.mark.parametrize(
    ("tool", "data", "message"),
    [
        ("osv", {"results": [{"packages": []}]}, "No package entries audited"),
        (
            "pip-audit",
            {"dependencies": [{"name": "example", "skip_reason": "not found"}]},
            "Incomplete PyPI coverage (1 skipped)",
        ),
        ("npm", {"error": {"code": "ECONNRESET"}}, "npm registry audit error"),
        ("cargo-audit", {"database": {"advisory-count": 0}}, "No RustSec advisory database"),
    ],
)
def test_rejects_incomplete_coverage(tmp_path: Path, tool: str, data: object, message: str) -> None:
    result = report(tmp_path, tool, data)
    assert result.returncode == 1
    assert "ERROR" in result.stdout and message in result.stdout


@pytest.mark.parametrize(("exit_code", "status"), [(0, "PASS"), (124, "ERROR")])
def test_report_keeps_counts_and_exit_status(tmp_path: Path, exit_code: int, status: str) -> None:
    result = report(
        tmp_path,
        "npm",
        {
            "vulnerabilities": {},
            "metadata": {
                "dependencies": {"total": 42},
                "vulnerabilities": dict.fromkeys(("critical", "high", "moderate", "low", "info", "total"), 0),
            },
        },
        exit_code,
    )
    assert result.returncode == int(bool(exit_code))
    assert f"{status} · 42 package entries · 0 findings · exit {exit_code}" in result.stdout
    assert "critical: 0, high: 0" in result.stdout


def test_advisory_ids_and_patches_are_printed_and_block_even_with_zero_exit(tmp_path: Path) -> None:
    result = report(
        tmp_path,
        "pip-audit",
        {
            "dependencies": [
                {
                    "name": "example",
                    "version": "1.0",
                    "vulns": [{"id": "PYSEC-test", "aliases": ["CVE-test"], "fix_versions": ["1.1"]}],
                }
            ]
        },
    )
    assert result.returncode == 1
    assert "FINDINGS · 1 package entries · 1 findings · exit 0" in result.stdout
    assert "example==1.0: CVE-test, PYSEC-test; patched: 1.1" in result.stdout
