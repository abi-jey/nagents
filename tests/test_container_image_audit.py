"""The published Trivy baseline must not hide new or unscanned findings."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/report_container_audit.py"
BASELINE = "ghcr.io/example/image@sha256:" + "1" * 64


def layout_with_index(path: Path) -> tuple[str, str]:
    blobs = path / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def blob(data: object) -> str:
        raw = json.dumps(data).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        (blobs / checksum).write_bytes(raw)
        return f"sha256:{checksum}"

    config = blob({"os": "linux", "architecture": "amd64"})
    image = blob({"config": {"digest": config}})
    index = blob(
        {
            "manifests": [
                {"digest": image, "platform": {"os": "linux", "architecture": "amd64"}},
                {"digest": "attestation", "platform": {"os": "unknown", "architecture": "unknown"}},
            ]
        }
    )
    (path / "index.json").write_text(json.dumps({"manifests": [{"digest": index}]}), encoding="utf-8")
    return index, config


def finding(severity: str, version: str = "1.0", advisory: str = "CVE-test") -> dict[str, str]:
    return {
        "Severity": severity,
        "PkgName": "example",
        "InstalledVersion": version,
        "VulnerabilityID": advisory,
        "FixedVersion": "2.0",
    }


def scan_report(
    artifact: str, image_id: str, findings: list[dict[str, str]], *, npm_packages: int = 1
) -> dict[str, object]:
    return {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "ArtifactName": artifact,
        "Metadata": {"ImageID": image_id, "OS": {"Family": "debian"}},
        "Results": [
            {"Type": "debian", "Class": "os-pkgs", "Packages": [{}], "Vulnerabilities": findings},
            {"Type": "python-pkg", "Class": "lang-pkgs", "Packages": [{}]},
            {"Type": "node-pkg", "Class": "lang-pkgs", "Packages": [{}] * npm_packages},
        ],
    }


def run_audit(
    tmp_path: Path, baseline_findings: list[dict[str, str]], candidate_findings: list[dict[str, str]], error: str = ""
) -> subprocess.CompletedProcess[str]:
    layout = tmp_path / "layout"
    image_digest, image_id = layout_with_index(layout)
    baseline, candidate = tmp_path / "baseline.json", tmp_path / "candidate.json"
    baseline.write_text(json.dumps(scan_report(BASELINE, image_id, baseline_findings)), encoding="utf-8")
    candidate.write_text(
        json.dumps(
            scan_report(
                str(layout),
                "sha256:" + "2" * 64 if error == "wrong image" else image_id,
                candidate_findings,
                npm_packages=int(error != "missing inventory"),
            )
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(baseline), BASELINE, str(candidate), str(layout), image_digest],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_existing_findings_remain_visible_without_blocking_the_image(tmp_path: Path) -> None:
    result = run_audit(tmp_path, [finding("CRITICAL")], [finding("CRITICAL", version="1.1")])
    assert result.returncode == 0
    assert "| CRITICAL | 1 | 1 | 0 |" in result.stdout
    assert "No new Trivy findings" in result.stdout


@pytest.mark.parametrize("same_advisory", [True, False])
def test_new_low_advisory_or_an_additional_copy_blocks_the_image(tmp_path: Path, same_advisory: bool) -> None:
    existing = finding("LOW")
    new = finding("LOW", advisory="CVE-test" if same_advisory else "CVE-new")
    result = run_audit(tmp_path, [existing], [existing, new])
    assert result.returncode == 1
    assert "| LOW | 1 | 2 | 1 |" in result.stdout
    assert f"| LOW | debian | example | 1.0 | {new['VulnerabilityID']} | 2.0 |" in result.stdout


@pytest.mark.parametrize("error", ["wrong image", "missing inventory"])
def test_incomplete_or_mismatched_scan_fails_closed(tmp_path: Path, error: str) -> None:
    result = run_audit(tmp_path, [], [], error)
    assert result.returncode == 1
    assert "Trivy audit error" in result.stdout
