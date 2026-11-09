"""Exercise release provenance with synthetic distributions and workflow wiring."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/release_artifacts.py"
SHA = "a" * 40


def distributions(directory: Path, version: str = "1.2.3") -> None:
    directory.mkdir()
    metadata = f"Name: nagents\nVersion: {version}\n\n".encode()
    with zipfile.ZipFile(directory / "nagents.whl", "w") as archive:
        archive.writestr("nagents.dist-info/METADATA", metadata)
    with tarfile.open(directory / "nagents.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("nagents/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


@pytest.mark.parametrize("mutation", ["none", "sha", "version", "bytes", "extra", "wheel_version"])
def test_release_provenance_rejects_mismatches(tmp_path: Path, mutation: str) -> None:
    directory = tmp_path / "dist"
    distributions(directory)
    manifest = tmp_path / "manifest.json"
    args = ["--dist", str(directory), "--manifest", str(manifest), "--sha", SHA, "--version", "1.2.3"]
    subprocess.run([sys.executable, str(SCRIPT), "create", *args], check=True)
    assert json.loads(manifest.read_text())["sha"] == SHA
    if mutation == "sha":
        args[5] = "b" * 40
    elif mutation == "version":
        args[-1] = "1.2.4"
    elif mutation == "bytes":
        with zipfile.ZipFile(directory / "nagents.whl", "a") as archive:
            archive.writestr("changed.txt", "changed bytes")
    elif mutation == "extra":
        (directory / "unexpected.whl").touch()
    elif mutation == "wheel_version":
        with zipfile.ZipFile(directory / "nagents.whl", "w") as archive:
            archive.writestr("nagents.dist-info/METADATA", "Name: nagents\nVersion: 1.2.4\n\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "verify", *args], capture_output=True)
    assert (result.returncode == 0) == (mutation == "none")


def test_publications_verify_artifacts_before_upload_and_pin_sha() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    for name in ("publish-stable", "publish-alpha", "publish-dev"):
        steps = jobs[name]["steps"]
        verify_index = next(i for i, step in enumerate(steps) if step.get("name") == "Verify release artifacts")
        publish_index = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("pypa/"))
        assert verify_index < publish_index
        verify = steps[verify_index]
        assert verify["env"]["RELEASE_SHA"] == "${{ needs.build.outputs.sha }}"
        assert verify["env"]["RELEASE_VERSION"] == "${{ needs.build.outputs.version }}"
        assert 'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"' in verify["run"]
        assert "release_artifacts.py verify" in verify["run"]
        assert not any("python -m build" in step.get("run", "") for step in steps)
        assert any(step.get("with", {}).get("name") == "release-manifest" for step in steps)
        for step in steps:
            assert "git pull" not in step.get("run", "")
        release = next((step for step in steps if step.get("uses", "").startswith("softprops/")), None)
        if release:
            assert (
                "github.sha" in release["with"]["target_commitish"]
                or "needs.build.outputs.sha" in release["with"]["target_commitish"]
            )
    bump_checkout = jobs["bump-version"]["steps"][0]
    assert bump_checkout["with"]["ref"] == "${{ github.sha }}"
    for name in ("build", "docker-build"):
        runs = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert 'git checkout --detach "$RELEASE_SHA"' in runs
        assert "git pull" not in runs
        assert "FETCH_HEAD" not in runs


def test_stable_tag_must_equal_distribution_version() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["publish-stable"]["steps"]
    version_step = next(step for step in steps if step.get("id") == "version")
    assert "ref == f'refs/tags/v{version}'" in version_step["run"]
    assert "version == os.environ['EXPECTED_VERSION']" in version_step["run"]
    verify = next(step for step in steps if step.get("name") == "Verify release artifacts")
    assert 'test "$TAG_SHA" = "$RELEASE_SHA"' in verify["run"]


@pytest.mark.parametrize(
    "ref,expected,success",
    [
        ("refs/tags/v1.2.3", "1.2.3", True),
        ("refs/tags/v1.2.4", "1.2.3", False),
        ("refs/heads/main", "1.2.4", False),
    ],
)
def test_publication_version_guard_executes(tmp_path: Path, ref: str, expected: str, success: bool) -> None:
    import os

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(step for step in workflow["jobs"]["publish-stable"]["steps"] if step.get("id") == "version")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    result = subprocess.run(
        [sys.executable, "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "GITHUB_REF": ref, "EXPECTED_VERSION": expected, "GITHUB_OUTPUT": str(tmp_path / "output")},
    )
    assert (result.returncode == 0) == success
