"""Version, provenance, packaging and extracted-install checks for Rust releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path
from typing import cast
from zipfile import ZipFile

import bundle

ROOT = Path(__file__).resolve().parents[2]
NGN = ROOT / "ngn"


def version_check(tag: str = "") -> str:
    version = tomllib.loads((NGN / "Cargo.toml").read_text())["workspace"]["package"]["version"]
    assert isinstance(version, str), "Rust workspace version must be text"
    if re.fullmatch(r"0\.100\.\d+", version) is None:
        raise ValueError("Coordinated releases must use the 0.100.x series")
    if tag and tag != f"v{version}":
        raise ValueError("Release tag does not match Rust version")
    core = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert core == version, "Core, native application, adapter and browser must share the same version"
    runtime_lock = json.loads((NGN / "runtime-lock.json").read_text())["core"]
    assert (
        runtime_lock["version"] == version
        and runtime_lock["official_metadata"] == f"https://pypi.org/pypi/nagents/{version}/json"
    ), "Runtime lock must bind the shared core version to fixed public PyPI metadata"
    adapter = tomllib.loads((NGN / "integrations/nagents/pyproject.toml").read_text())
    assert adapter["project"]["version"] == version, "Adapter version mismatch"
    assert f"nagents>={version},<0.101" in adapter["project"]["dependencies"], (
        "Bundle requires the public-auth core dependency"
    )
    assert json.loads((NGN / "package.json").read_text())["version"] == version, "Browser version mismatch"
    assert f'__version__ = "{version}"' in (NGN / "integrations/nagents/src/ngn_nagents/__init__.py").read_text()
    bootstrap = re.search(r"(?m)^NGN_BOOTSTRAP_VERSION=([0-9.]+)$", (NGN / "install.sh").read_text())
    assert bootstrap is not None and bootstrap[1] == version, "Installer bootstrap version mismatch"
    assert f'<span id="version">{version}</span>' in (NGN / "web/index.html").read_text(), "Web client version mismatch"
    lock = tomllib.loads((NGN / "Cargo.lock").read_text())
    for package in lock["package"]:
        if package["name"] in {"ngn", "ngn-core", "ngn-protocol"}:
            assert package["version"] == version, "Cargo lock version mismatch"
    return version


def bump(version: str, notes: str, root: Path = ROOT) -> None:
    """Move every coordinated component to one new version and record its release notes."""
    if re.fullmatch(r"0\.100\.\d+", version) is None:
        raise ValueError("Coordinated releases must use the 0.100.x series")
    if not notes.strip() or "\n" in notes.strip():
        raise ValueError("Release notes must be one non-empty line")
    ngn = root / "ngn"
    current = tomllib.loads((ngn / "Cargo.toml").read_text())["workspace"]["package"]["version"]
    if tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
        raise ValueError(f"New version must be greater than {current}")
    old = re.escape(current)
    replacements: list[tuple[Path, str, str]] = [
        (root / "pyproject.toml", rf'(?m)^version = "{old}"$', f'version = "{version}"'),
        (ngn / "Cargo.toml", rf'(?m)^version = "{old}"$', f'version = "{version}"'),
        (ngn / "package.json", rf'"version": "{old}"', f'"version": "{version}"'),
        (ngn / "integrations/nagents/pyproject.toml", rf'(?m)^version = "{old}"$', f'version = "{version}"'),
        (ngn / "integrations/nagents/pyproject.toml", rf'"nagents>={old},<0\.101"', f'"nagents>={version},<0.101"'),
        (
            ngn / "integrations/nagents/src/ngn_nagents/__init__.py",
            rf'__version__ = "{old}"',
            f'__version__ = "{version}"',
        ),
        (ngn / "install.sh", rf"(?m)^NGN_BOOTSTRAP_VERSION={old}$", f"NGN_BOOTSTRAP_VERSION={version}"),
        (ngn / "runtime-lock.json", rf'"version": "{old}"', f'"version": "{version}"'),
        (ngn / "runtime-lock.json", rf"pypi/nagents/{old}/json", f"pypi/nagents/{version}/json"),
        (ngn / "web/index.html", rf'<span id="version">{old}</span>', f'<span id="version">{version}</span>'),
    ]
    for crate in ("ngn", "ngn-core", "ngn-protocol"):
        replacements.append(
            (ngn / "Cargo.lock", rf'(?m)^(name = "{crate}"\n)version = "{old}"$', rf'\g<1>version = "{version}"')
        )
    for path, pattern, replacement in replacements:
        text, count = re.subn(pattern, replacement, path.read_text())
        if count != 1:
            raise ValueError(f"Expected one version field in {path.relative_to(root)}, found {count}")
        path.write_text(text)
    notes_path = ngn / "docs/releases" / f"{version}.md"
    if notes_path.exists():
        raise ValueError(f"{notes_path.relative_to(root)} already exists")
    notes_path.write_text(f"# v{version}\n\n{notes.strip()}\n")
    changelog = ngn / "CHANGELOG.md"
    text = changelog.read_text()
    heading = "# Changelog\n\n"
    if not text.startswith(heading):
        raise ValueError("CHANGELOG.md must start with '# Changelog'")
    changelog.write_text(f"{heading}## {version}\n\n- {notes.strip()}\n\n{text[len(heading) :]}")


def commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


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


def core_artifacts(destination: Path, *, published: bool) -> bundle.CoreArtifacts:
    """Keep both exact core distributions; never build a downloaded source archive."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert isinstance(version, str)
    if published:
        return bundle.download_published_core(destination, version=version, source_commit=commit())
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(destination), str(ROOT)],
        cwd=destination,
        check=True,
    )
    artifacts = bundle.CoreArtifacts(
        destination / f"nagents-{version}-py3-none-any.whl", destination / f"nagents-{version}.tar.gz"
    )
    assert {path.name for path in destination.iterdir()} == {artifacts.wheel.name, artifacts.sdist.name}, (
        "Core distributions mismatch"
    )
    bundle.verify_core_artifacts(artifacts, version=version, source_commit=commit())
    return artifacts


def package(output: Path, binary: Path, *, bundled: bool = False, published_core: bool = False) -> None:
    version = version_check()
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip(), (
        "Commit the complete candidate before packaging"
    )
    assert platform.system() == "Linux" and platform.machine() == "x86_64", "Only Linux x86_64 binaries are validated"
    assert subprocess.check_output([str(binary), "--version"], text=True).strip() == f"ngn {version}"
    assert not published_core or bundled, "--published-core requires --bundle"
    if bundled:
        bundle.prepare_output(output)
    else:
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
    if bundled:
        with tempfile.TemporaryDirectory(prefix="ngn-bundle-core-", dir=output.parent) as temporary:
            core = core_artifacts(Path(temporary), published=published_core)
            bundle.build_bundle(
                output,
                binary,
                core_wheel=core.wheel,
                adapter_wheel=output / f"ngn_nagents-{version}-py3-none-any.whl",
                official=published_core,
                source_commit=commit(),
                cache=Path.home() / ".cache/ngn-runtime-artifacts",
            )
            for path in (core.wheel, core.sdist):
                shutil.copyfile(path, output / path.name)
        shutil.copy2(binary, output / "ngn-installer-linux-x86_64")
        shutil.copyfile(NGN / "install.sh", output / "install.sh")
    manifest = {
        "schema_version": 2 if bundled else 1,
        "commit": commit(),
        "ngn_version": version,
        "nagents_version": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
        "runtime_protocol": 1,
        "platform": "linux-x86_64",
        "artifacts": {path.name: digest(path) for path in sorted(output.iterdir())},
    }
    if bundled:
        manifest["bundle"] = f"ngn-v{version}-linux-x86_64-bundle.tar.gz"
    (output / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in sorted(output.iterdir())))
    verify(output, require_bundle=published_core)


def validate_bundle_metadata(
    metadata: dict[str, object], manifest: dict[str, object], *, require_published: bool = False
) -> None:
    assert type(metadata.get("bundle_schema")) is int and metadata["bundle_schema"] == 1, "Bundle schema mismatch"
    for bundle_key, release_key in (
        ("ngn_version", "ngn_version"),
        ("adapter_version", "ngn_version"),
        ("nagents_version", "nagents_version"),
        ("source_commit", "commit"),
        ("runtime_protocol", "runtime_protocol"),
        ("platform", "platform"),
    ):
        assert metadata.get(bundle_key) == manifest[release_key], f"Bundle {bundle_key} provenance mismatch"
    assert metadata.get("runner") == ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"], (
        "Unexpected bundled runner"
    )
    if require_published:
        assert metadata.get("official") is True, "Publication requires a bundle built with the matching published core"


def verify_bundled_install(output: Path, manifest: dict[str, object], *, require_published: bool) -> None:
    """Use the real verified native extractor/installer, not a Python extraction bypass."""
    archive = output / str(manifest["bundle"])
    installer = output / "ngn-installer-linux-x86_64"
    bundle.verify_bundle_core(
        archive,
        output / f"nagents-{manifest['nagents_version']}-py3-none-any.whl",
        version=str(manifest["nagents_version"]),
        source_commit=str(manifest["commit"]),
        official=require_published,
    )
    with tarfile.open(archive, "r:gz") as source:
        member = source.getmember("ngn/bundle.json")
        assert member.isfile() and 0 < member.size <= 8 * 1024 * 1024, "Invalid bundle metadata member"
        stream = source.extractfile(member)
        assert stream is not None
        value: object = json.loads(stream.read())
        assert isinstance(value, dict), "Bundle metadata must be an object"
        metadata = cast("dict[str, object]", value)
        validate_bundle_metadata(metadata, manifest, require_published=require_published)
        native = source.extractfile("ngn/ngn")
        assert native is not None, "Missing bundled native application"
        checksum = hashlib.sha256()
        while chunk := native.read(1024 * 1024):
            checksum.update(chunk)
        assert checksum.hexdigest() == digest(installer), "Bundled application and native installer differ"
    assert (output / "install.sh").read_bytes() == (NGN / "install.sh").read_bytes(), "Bootstrap source mismatch"
    with tempfile.TemporaryDirectory(prefix="ngn-bundled-install-", dir=output.parent) as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir(mode=0o700)
        environment = {"HOME": str(home), "TMPDIR": str(root), "PATH": ""}
        subprocess.run(
            [
                str(installer),
                "install",
                "--bundle",
                str(archive),
                "--sha256",
                digest(archive),
                "--version",
                str(manifest["ngn_version"]),
                "--prefix",
                str(root / "installation"),
                "--bin-dir",
                str(root / "bin"),
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        doctor = subprocess.run(
            [str(root / "bin/ngn"), "doctor"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "runtime/bin/python3" in doctor.stdout, "Installed default runner did not select bundled Python"
    print("PASS: native installation and bundled default runner with no Python/pip/Rust/Node on PATH")


def verify(output: Path, *, require_bundle: bool = False) -> None:
    version = version_check()
    manifest = json.loads((output / "release-manifest.json").read_text())
    schema = manifest["schema_version"]
    assert type(schema) is int and schema in (1, 2) and manifest["commit"] == commit(), (
        "Artifacts do not belong to this exact tested commit"
    )
    assert not require_bundle or schema == 2, "Publication requires a self-contained runtime bundle"
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
    if schema == 2:
        bundle_name = f"ngn-v{version}-linux-x86_64-bundle.tar.gz"
        assert manifest.get("bundle") == bundle_name, "Invalid bundled archive name"
        expected.update({bundle_name, "ngn-installer-linux-x86_64", "install.sh"})
        expected.update({f"nagents-{version}-py3-none-any.whl", f"nagents-{version}.tar.gz"})
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
    if schema == 2:
        bundle.verify_core_artifacts(
            bundle.CoreArtifacts(output / f"nagents-{version}-py3-none-any.whl", output / f"nagents-{version}.tar.gz"),
            version=version,
            source_commit=commit(),
            published=require_bundle,
        )
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
    if schema == 2:
        verify_bundled_install(output, manifest, require_published=require_bundle)
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
    parser.add_argument("action", choices=("bump", "check", "package", "verify"))
    parser.add_argument("version", nargs="?", default="", help="bump: the new shared 0.100.x version")
    parser.add_argument("--notes", default="", help="bump: one-line release notes")
    parser.add_argument("--tag", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--binary", type=Path, default=NGN / "target/release/ngn")
    parser.add_argument("--bundle", action="store_true", help="Package relocatable Python, dependencies and installer")
    parser.add_argument(
        "--published-core", action="store_true", help="Require the matching public PyPI core for the bundle"
    )
    parser.add_argument(
        "--require-bundle", action="store_true", help="Reject legacy or unofficial bundles before publication"
    )
    arguments = parser.parse_args()
    if arguments.action == "bump":
        bump(arguments.version, arguments.notes)
        print("PASS: bumped every coordinated component to", version_check())
    elif arguments.action == "check":
        print("PASS: consistent Rust component versions:", version_check(arguments.tag))
    elif arguments.action == "package":
        package(
            arguments.output.absolute(),
            arguments.binary.resolve(),
            bundled=arguments.bundle,
            published_core=arguments.published_core,
        )
    else:
        verify(arguments.output.resolve(), require_bundle=arguments.require_bundle)


if __name__ == "__main__":
    main()
