"""Build working source and run installed release-shaped artifacts, never editable imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from release import binary_archive, version_check

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Invocation:
    config: str
    data_dir: str
    arguments: list[str]


def invocation(arguments: list[str]) -> Invocation:
    """Remove only configuration/state options that this wrapper must isolate."""
    forwarded: list[str] = []
    config = ""
    data_dir = ""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            forwarded.extend(arguments[index:])
            break
        option, separator, value = argument.partition("=")
        if option in {"--config", "--data-dir"}:
            if not separator:
                index += 1
                if index >= len(arguments) or arguments[index].startswith("--"):
                    raise ValueError(f"{option} needs a path")
                value = arguments[index]
            if not value:
                raise ValueError(f"{option} needs a path")
            if option == "--config":
                if config:
                    raise ValueError("Pass --config only once")
                config = value
            else:
                if data_dir:
                    raise ValueError("Pass --data-dir only once")
                data_dir = value
        else:
            forwarded.append(argument)
        index += 1
    return Invocation(config, data_dir, forwarded)


def isolated_environment(runtime: Path, environ: dict[str, str]) -> dict[str, str]:
    environment = dict(environ)
    for variable in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(variable, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["VIRTUAL_ENV"] = str(runtime)
    environment["PATH"] = str(runtime / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def runtime_config(source: Path, runtime: Path) -> dict[str, object]:
    if source.stat().st_size > 262_144:
        raise ValueError("Rust configuration exceeds 256 KiB")
    value: object = json.loads(source.read_text())
    if not isinstance(value, dict):
        raise ValueError("Rust configuration must be a JSON object")
    settings = cast("dict[str, object]", value)
    if settings.get("config_version", 1) != 1:
        raise ValueError("Unsupported Rust config_version; legacy configurations are not imported")
    settings["runner"] = [str(runtime / "bin/python"), "-I", "-m", "ngn_nagents"]
    return settings


def command(binary: Path, settings: Invocation, config: Path, state: Path, *, demo: bool) -> list[str]:
    prefix = [str(binary), "--config", str(config), "--data-dir", str(state)]
    if demo and "--demo" not in settings.arguments:
        prefix.append("--demo")
    return [*prefix, *settings.arguments]


def run(arguments: list[str], *, directory: Path, environment: dict[str, str]) -> None:
    subprocess.run(arguments, cwd=directory, env=environment, check=True)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cargo_target(root: Path, cache: Path, environ: dict[str, str]) -> Path:
    """Reuse checkout-local targets; namespace external bases by canonical checkout.

    Relative paths resolve from the launcher's working directory, not the manifest.
    No directories are created and the supplied environment is left unchanged.
    """
    checkout = root.expanduser().resolve()
    target = Path(environ.get("CARGO_TARGET_DIR", str(cache / "target"))).expanduser().resolve()
    if target.is_relative_to(checkout):
        return target
    identity = hashlib.sha256(str(checkout).encode()).hexdigest()[:16]
    return target / f"ngn-dev-{identity}"


def ensure_private_cache(path: Path) -> None:
    """Create each missing canonical ancestor privately, never repair existing paths."""
    path = path.expanduser().resolve()
    for directory in (*reversed(path.parents), path):
        with suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(
                f"Development cache requires real directories: {directory}; existing paths were not changed"
            )
        if os.name == "posix":
            trusted_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if metadata.st_mode & 0o022 and not trusted_sticky:
                raise ValueError(
                    f"Development cache requires non-writable ancestors: {directory}. "
                    "Existing paths were not changed; choose a private NGN_DEV_CACHE or explicitly secure that directory."
                )


def offline_cache_probe(stage: Path) -> None:
    """Check real launcher ancestry, not only smoke fixtures; never use operator state."""
    workspace = stage / "offline-probe-workspace"
    workspace.mkdir(mode=0o700)
    result = subprocess.run(
        [
            str(stage / "app/ngn"),
            "--demo",
            "--read-only",
            "-C",
            str(workspace),
            "--data-dir",
            str(stage / "offline-probe-state"),
            "run",
            "--json",
            "offline development cache ancestry acceptance",
        ],
        # No Python or provider command is available. Demo is always explicit,
        # even when --test was invoked with a live --config or --data-dir.
        env={"PATH": str(stage / "app")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise ValueError(f"Offline installed cache/state probe failed: {result.stderr[:4096]}")
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if not events or events[-1].get("status") != "completed":
        raise ValueError("Offline installed cache/state probe did not complete")
    print(
        "PASS: extracted offline/read-only launch in actual cache ancestry, without Python or model access", flush=True
    )


def source_snapshot(root: Path) -> tuple[str, str, bool]:
    # A hook may export its parent's index or worktree. Never let that context
    # redirect even read-only provenance queries into another repository.
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, env=environment, text=True).strip()
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root, env=environment
    ).split(b"\0")
    fingerprint = hashlib.sha256()
    for name in sorted(set(names)):
        path = root / os.fsdecode(name)
        if name and path.is_file():
            fingerprint.update(name + b"\0" + path.read_bytes())
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, env=environment, text=True).strip()
    )
    return revision, fingerprint.hexdigest(), dirty


def build(root: Path, cache: Path, *, published_core: bool) -> tuple[Path, Path, dict[str, str]]:
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        raise ValueError("Release-shaped binary packaging is currently validated only on Linux x86_64")
    # Reuse only checkout-scoped compilation output, never a staged runtime or
    # installed Python package. Cargo locking alone does not isolate source trees.
    snapshot = source_snapshot(root)
    ensure_private_cache(cache)
    stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=cache))
    runtime = stage / "runtime"
    environment = isolated_environment(runtime, dict(os.environ))
    target = cargo_target(root, cache, environment)
    environment["CARGO_TARGET_DIR"] = str(target)
    # Use the bootstrap interpreter only to create the isolated installed runtime.
    run([sys.executable, "-m", "venv", str(runtime)], directory=root, environment=environment)
    python = str(runtime / "bin/python")
    run(
        ["cargo", "build", "--release", "--locked", "--manifest-path", str(root / "ngn/Cargo.toml"), "-p", "ngn"],
        directory=root,
        environment=environment,
    )
    version = tomllib.loads((root / "ngn/Cargo.toml").read_text())["workspace"]["package"]["version"]
    core_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    binary = stage / "built-ngn"
    shutil.copy2(target / "release/ngn", binary)
    actual = subprocess.check_output([str(binary), "--version"], text=True, env=environment).strip()
    if actual != f"ngn {version}":
        raise ValueError("The freshly built binary has a different version; do not reuse stale artifacts")
    wheels = stage / "wheels"
    wheels.mkdir()
    sources = [str(root / "ngn/integrations/nagents")]
    if not published_core:
        sources.insert(0, str(root))
    run(
        [
            python,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--wheel-dir",
            str(wheels),
            *sources,
        ],
        directory=stage,
        environment=environment,
    )
    artifacts = sorted(wheels.glob("*.whl"))
    expected = {f"ngn_nagents-{version}-py3-none-any.whl"}
    if not published_core:
        expected.add(f"nagents-{core_version}-py3-none-any.whl")
    if {path.name for path in artifacts} != expected:
        raise ValueError("Wheel versions do not match this checkout")
    packages = [str(path) for path in artifacts]
    if published_core:
        packages.insert(0, f"nagents=={core_version}")
    run(
        [python, "-m", "pip", "install", "--disable-pip-version-check", *packages],
        directory=stage,
        environment=environment,
    )
    run([python, "-m", "pip", "check"], directory=stage, environment=environment)
    run(
        [
            python,
            "-I",
            "-c",
            "import pathlib,sys,nagents,ngn_nagents; root=pathlib.Path(sys.prefix).resolve(); assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) for m in (nagents,ngn_nagents)), 'Editable or external runtime imports detected'",
        ],
        directory=stage,
        environment=environment,
    )
    archive = stage / f"ngn-v{version}-linux-x86_64.tar.gz"
    binary_archive(archive, binary, root / "ngn")
    app = stage / "app"
    app.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(app, filter="data")
    if digest(app / "ngn") != digest(binary):
        raise ValueError("Extracted binary does not match the fresh build")
    if source_snapshot(root) != snapshot:
        raise ValueError("Checkout changed during the build. Run ./ngn/dev again for a consistent candidate")
    revision, source_digest, dirty = snapshot
    manifest = {
        "kind": "local-development-candidate-not-an-official-release",
        "source_commit": revision,
        "working_source_sha256": source_digest,
        "working_tree_modified": dirty,
        "ngn_version": version,
        "nagents_version": core_version,
        "core_source": "published" if published_core else "local-wheel",
        "artifacts": {path.name: digest(path) for path in [archive, *artifacts]},
    }
    (stage / "dev-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (cache / "latest-path").write_text(str(stage) + "\n")
    print(f"\nngn {version} · installed local development candidate", flush=True)
    print(f"Source: {root} · {revision[:12]}{' + uncommitted edits' if dirty else ''}", flush=True)
    print(f"Artifacts and installed environment: {stage}", flush=True)
    return stage, runtime, environment


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Other arguments are forwarded unchanged to the Rust CLI. No config defaults to the offline demo; live runs require --config.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="build installed artifacts and run real CLI/HTTP/WebSocket/PTY acceptance, then exit",
    )
    parser.add_argument(
        "--published-core",
        action="store_true",
        help="use the pinned published Python core instead of a wheel built from local source",
    )
    options, arguments = parser.parse_known_args()
    version_check()
    settings = invocation(arguments)
    # Resolve paths before building. Neither old Python configuration nor .env is read.
    original_config = (
        Path(settings.config).expanduser().resolve() if settings.config else ROOT / "ngn/config.example.json"
    )
    if not original_config.is_file():
        raise ValueError("The explicit Rust --config file does not exist")
    identity = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache = Path(os.environ.get("NGN_DEV_CACHE", str(cache_home / "ngn-rust-dev" / identity))).expanduser().resolve()
    state = Path(settings.data_dir).expanduser().resolve() if settings.data_dir else cache / "state"
    stage, runtime, environment = build(ROOT, cache, published_core=options.published_core)
    config = stage / "config.json"
    config.write_text(json.dumps(runtime_config(original_config, runtime), indent=2) + "\n")
    config.chmod(0o600)
    python = str(runtime / "bin/python")
    if options.test:
        offline_cache_probe(stage)
        run(
            [python, str(ROOT / "ngn/scripts/smoke.py"), "--binary", str(stage / "app/ngn")],
            directory=stage,
            environment=environment,
        )
        print("PASS: release-shaped local installation acceptance", flush=True)
        return 0
    print(f"Development state: {state}", flush=True)
    if not settings.config:
        print("No --config: starting the offline Rust demo. Use --config PATH for a real model.", flush=True)
    argv = command(stage / "app/ngn", settings, config, state, demo=not settings.config)
    os.execve(argv[0], argv, environment)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"ngn dev: {error}", file=sys.stderr)
        raise SystemExit(1) from error
