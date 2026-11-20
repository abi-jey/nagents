"""Exercise release provenance with synthetic distributions and workflow wiring."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
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
    for name in ("build", "docker-build"):
        runs = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert "git pull" not in runs
        assert "FETCH_HEAD" not in runs


def test_build_runs_alongside_tests_but_every_publication_waits_for_them() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert "needs" not in jobs["build"]
    for name in ("docker-build", "publish-stable", "publish-alpha", "publish-dev"):
        assert set(jobs[name]["needs"]) == {"lint", "test", "build"}, name
        assert "always()" not in jobs[name]["if"] and "cancelled()" not in jobs[name]["if"], name


def test_stable_tag_must_equal_distribution_version() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["publish-stable"]["steps"]
    version_step = next(step for step in steps if step.get("id") == "version")
    assert "ref == f'refs/tags/v{version}'" in version_step["run"]
    assert "version == os.environ['EXPECTED_VERSION']" in version_step["run"]
    verify = next(step for step in steps if step.get("name") == "Verify release artifacts")
    assert 'test "$TAG_SHA" = "$RELEASE_SHA"' in verify["run"]


@pytest.mark.parametrize("name", ["publish-stable", "publish-alpha", "publish-dev"])
def test_core_publish_jobs_are_pypi_only_and_cannot_create_separate_github_releases(name: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"][name]
    assert job["permissions"] == {"id-token": "write", "contents": "read"}
    assert any(step.get("uses", "").startswith("pypa/") for step in job["steps"])
    for step in job["steps"]:
        text = json.dumps(step).lower()
        assert all(
            token not in text for token in ("softprops/", "action-gh-release", "gh release", "gh api", "/releases")
        )


def test_shared_version_tag_publishes_pypi_before_the_single_version_only_github_release() -> None:
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    native = yaml.load((ROOT / ".github/workflows/ngn-release.yml").read_text(), Loader=yaml.BaseLoader)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert re.fullmatch(r"0\.100\.\d+", version)
    assert tomllib.loads((ROOT / "ngn/Cargo.toml").read_text())["workspace"]["package"]["version"] == version
    adapter = tomllib.loads((ROOT / "ngn/integrations/nagents/pyproject.toml").read_text())["project"]
    assert adapter["version"] == version and adapter["dependencies"] == [f"nagents>={version},<0.101"]
    assert json.loads((ROOT / "ngn/package.json").read_text())["version"] == version
    assert json.loads((ROOT / "ngn/runtime-lock.json").read_text())["core"]["version"] == version
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert set(native["on"]) == {"workflow_call"}
    caller = workflow["jobs"]["publish-ngn"]
    assert caller["needs"] == ["publish-stable"]
    assert "!cancelled()" in caller["if"] and "needs.publish-stable.result == 'success'" in caller["if"]
    assert "startsWith(github.ref, 'refs/tags/v0.100.')" in caller["if"]
    assert caller["uses"] == "./.github/workflows/ngn-release.yml" and caller["secrets"] == "inherit"
    assert caller["permissions"] == {"contents": "write"}
    candidate = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("id") == "candidate")
    assert "ngn/scripts/release.py', 'check', '--tag'" in candidate["run"]
    assert "bump-version" not in workflow["jobs"]
    build_runs = "\n".join(step.get("run", "") for step in native["jobs"]["build"]["steps"])
    for gate in (
        "git merge-base --is-ancestor HEAD origin/main",
        "pre-commit run --all-files",
        "git diff --exit-code",
        "--bundle --published-core",
        "--require-bundle",
        "install_smoke.py",
        "browser_smoke.py",
        "playwright install --with-deps chromium",
    ):
        assert gate in build_runs
    macos = native["jobs"]["build-macos"]
    assert {entry["platform"] for entry in macos["strategy"]["matrix"]["include"]} == {"macos-arm64", "macos-x86_64"}
    macos_runs = "\n".join(step.get("run", "") for step in macos["steps"])
    for gate in (
        "git merge-base --is-ancestor HEAD origin/main",
        "git diff --exit-code",
        "--bundle --published-core",
        "--require-bundle",
        "install_smoke.py",
        "cargo test",
    ):
        assert gate in macos_runs
    publish = native["jobs"]["publish"]
    assert publish["needs"] == ["build", "build-macos"]
    publish_run = next(step["run"] for step in publish["steps"] if "gh release create" in step.get("run", ""))
    assert publish_run.index("release.py merge") < publish_run.index("--require-bundle")
    for platform in ("linux-x86_64", "macos-arm64", "macos-x86_64"):
        assert f"--source platforms/ngn-release-{platform}" in publish_run
    assert '--title "$RELEASE_TAG"' in publish_run and '--title "ngn ' not in publish_run
    assert publish_run.count("gh release create") == 1
    assert (
        publish_run.index("--require-bundle")
        < publish_run.index("gh release view")
        < publish_run.index("gh release create")
    )
    assert "Refusing to overwrite an existing release" in publish_run
    assert "--verify-tag" in publish_run and "--draft\n" in publish_run and "--draft=false --latest" in publish_run


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
