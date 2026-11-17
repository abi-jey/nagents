"""Version, provenance, packaging and extracted-install checks for Rust releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
NGN = ROOT / "ngn"


def version_check(tag: str = "") -> str:
    version = tomllib.loads((NGN / "Cargo.toml").read_text())["workspace"]["package"]["version"]
    assert isinstance(version, str), "Rust workspace version must be text"
    if re.fullmatch(r"0\.100\.\d+", version) is None:
        raise ValueError("Rust releases must use the reserved 0.100.x series")
    if tag and tag != f"v{version}":
        raise ValueError("Release tag does not match Rust version")
    adapter = tomllib.loads((NGN / "integrations/nagents/pyproject.toml").read_text())
    assert adapter["project"]["version"] == version, "Adapter version mismatch"
    assert json.loads((NGN / "package.json").read_text())["version"] == version, "Browser version mismatch"
    assert f'__version__ = "{version}"' in (NGN / "integrations/nagents/src/ngn_nagents/__init__.py").read_text()
    lock = tomllib.loads((NGN / "Cargo.lock").read_text())
    for package in lock["package"]:
        if package["name"] in {"ngn", "ngn-core", "ngn-protocol"}:
            assert package["version"] == version, "Cargo lock version mismatch"
    return version


def commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def binary_archive(destination: Path, binary: Path, source: Path) -> None:
    """Bundle only checked-in application docs/examples, never local configuration."""
    with tarfile.open(destination, "w:gz") as target:
        target.add(binary, arcname="ngn")
        for name in (
            "LICENSE",
            "README.md",
            "CHANGELOG.md",
            "config.example.json",
            "config.chatgpt.example.json",
            "docs",
        ):
            target.add(source / name, arcname=name)


def package(output: Path, binary: Path) -> None:
    version = version_check()
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip(), (
        "Commit the complete candidate before packaging"
    )
    assert platform.system() == "Linux" and platform.machine() == "x86_64", "Only Linux x86_64 binaries are validated"
    assert subprocess.check_output([str(binary), "--version"], text=True).strip() == f"ngn {version}"
    output.mkdir(parents=True, exist_ok=True)
    assert not list(output.iterdir()), "Use a new artifact directory; never overwrite an existing release"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(output),
            str(NGN / "integrations/nagents"),
        ],
        check=True,
    )
    archive = output / f"ngn-v{version}-linux-x86_64.tar.gz"
    binary_archive(archive, binary, NGN)
    subprocess.run(
        [
            "git",
            "archive",
            "HEAD",
            "--format=tar.gz",
            f"--prefix=nagents-ngn-{version}/",
            "-o",
            str(output / f"ngn-v{version}-source.tar.gz"),
        ],
        cwd=ROOT,
        check=True,
    )
    manifest = {
        "schema_version": 1,
        "commit": commit(),
        "ngn_version": version,
        "nagents_version": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
        "runtime_protocol": 1,
        "platform": "linux-x86_64",
        "artifacts": {path.name: digest(path) for path in sorted(output.iterdir())},
    }
    (output / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in sorted(output.iterdir())))
    verify(output)


def verify(output: Path) -> None:
    version = version_check()
    manifest = json.loads((output / "release-manifest.json").read_text())
    assert manifest["schema_version"] == 1 and manifest["commit"] == commit(), (
        "Artifacts do not belong to this exact tested commit"
    )
    assert manifest["ngn_version"] == version and manifest["runtime_protocol"] == 1
    assert manifest["nagents_version"] == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], (
        "Python core provenance mismatch"
    )
    assert manifest["platform"] == "linux-x86_64", "Unsupported release platform"
    expected = {
        f"ngn-v{version}-linux-x86_64.tar.gz",
        f"ngn-v{version}-source.tar.gz",
        f"ngn_nagents-{version}-py3-none-any.whl",
    }
    assert set(manifest["artifacts"]) == expected, "Missing or unexpected release assets"
    for name, checksum in manifest["artifacts"].items():
        assert Path(name).name == name and digest(output / name) == checksum, f"Corrupt artifact: {name}"
    checksums = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in (output / "SHA256SUMS").read_text().splitlines()
    }
    assert set(checksums) == expected | {"release-manifest.json"}
    for name, checksum in checksums.items():
        assert digest(output / name) == checksum, f"Checksum failed: {name}"
    assert {path.name for path in output.iterdir()} == expected | {"release-manifest.json", "SHA256SUMS"}
    with ZipFile(output / f"ngn_nagents-{version}-py3-none-any.whl") as wheel:
        for source in (NGN / "integrations/nagents/src/ngn_nagents").glob("*.py"):
            assert wheel.read(f"ngn_nagents/{source.name}") == source.read_bytes(), (
                "Packaged adapter is not the tested source"
            )
    with tarfile.open(output / f"ngn-v{version}-source.tar.gz") as source_archive:
        prefix = f"nagents-ngn-{version}/"
        for path, expected_version, component in [
            ("pyproject.toml", manifest["nagents_version"], "project"),
            ("ngn/Cargo.toml", version, "workspace"),
        ]:
            member = source_archive.extractfile(prefix + path)
            assert member is not None, "Missing component source"
            metadata = tomllib.loads(member.read().decode())
            actual = (
                metadata["project"]["version"]
                if component == "project"
                else metadata["workspace"]["package"]["version"]
            )
            assert actual == expected_version, "Source archive component version mismatch"
    with tempfile.TemporaryDirectory(prefix="ngn-install-", dir=output.parent) as temporary:
        extracted = Path(temporary)
        with tarfile.open(output / f"ngn-v{version}-linux-x86_64.tar.gz") as archive:
            archive.extractall(extracted, filter="data")
        for name in ("config.example.json", "config.chatgpt.example.json"):
            assert (extracted / name).read_bytes() == (NGN / name).read_bytes(), (
                "Packaged configuration example is not the tested source"
            )
        binary = extracted / "ngn"
        assert subprocess.check_output([str(binary), "--version"], text=True).strip() == f"ngn {version}"
        run = subprocess.run(
            [
                str(binary),
                "--demo",
                "-C",
                str(extracted),
                "--data-dir",
                str(extracted / "state"),
                "run",
                "--json",
                "demo subagents",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": str(extracted)},
        )
        events = [json.loads(line) for line in run.stdout.splitlines()]
        assert events[-1]["status"] == "completed"
        assert (
            sum(event["event"] == "task_finished" and event["task"]["status"] == "completed" for event in events) == 2
        )
    print(
        f"PASS: ngn {version} artifacts bound to {manifest['commit']}; checksums and extracted Python-free install verified"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "package", "verify"))
    parser.add_argument("--tag", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--binary", type=Path, default=NGN / "target/release/ngn")
    arguments = parser.parse_args()
    if arguments.action == "check":
        print("PASS: consistent Rust component versions:", version_check(arguments.tag))
    elif arguments.action == "package":
        package(arguments.output.resolve(), arguments.binary.resolve())
    else:
        verify(arguments.output.resolve())


if __name__ == "__main__":
    main()
