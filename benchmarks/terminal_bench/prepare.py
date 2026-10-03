"""Build an immutable wheel snapshot and reviewable pinned Harbor job config.

Does not start containers, copy credentials, or make model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC
from datetime import datetime
from importlib.metadata import distributions
from importlib.metadata import requires
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HARBOR_VERSION = "0.23.0"
HARBOR_COMMIT = "1e5c5c6db929a10a140d05e606882c671ae20729"
DATASET_COMMIT = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
DATASET_REPO = "https://github.com/harbor-framework/terminal-bench.git"
TASKS = ("session-window-debug", "wal-recovery-ordering", "data-anonymization")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_constraints() -> str:
    """Freeze the measured wheel's small runtime dependency tree, without Harbor."""
    pending = ["aiohttp", "aiosqlite", "PyYAML"]
    resolved: dict[str, str] = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in resolved:
            continue
        resolved[name] = version(name)
        for raw in requires(name) or []:
            dependency = Requirement(raw)
            # Pilot containers use Python 3.12. Do not select host-only markers.
            if dependency.marker is None or dependency.marker.evaluate({"python_version": "3.12", "extra": ""}):
                pending.append(dependency.name)
    return "".join(f"{name}=={number}\n" for name, number in sorted(resolved.items()))


def selected_tasks(tasks: tuple[str, ...]) -> tuple[str, ...]:
    """The selectable tasks are pinned, inspected, single-container pilot tasks."""
    if not tasks or any(task not in TASKS for task in tasks):
        raise ValueError(f"Select a pinned supported task: {', '.join(TASKS)}")
    if len(tasks) != len(set(tasks)):
        raise ValueError("Select each task only once; repeated trials need an explicit new run")
    return tasks


def build_bundle(repo: Path, destination: Path, tasks: tuple[str, ...] = TASKS) -> dict[str, object]:
    tasks = selected_tasks(tasks)
    if destination.exists():
        raise ValueError("Use a new bundle directory; benchmark inputs are immutable")
    destination.mkdir(parents=True)

    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=repo).decode()

    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    selected = sorted(
        name
        for name in set(files)
        if name in {"pyproject.toml", "README.md", "LICENSE"}
        or (
            name.startswith("src/nagents/")
            and not name.startswith("src/nagents/web-ui/")
            and "__pycache__" not in Path(name).parts
            and not name.endswith(".pyc")
        )
    )
    # Built web assets are intentional wheel artifacts and gitignored. Capture
    # their actual bytes too, without uploading source UI caches/node_modules.
    selected.extend(
        str(path.relative_to(repo)) for path in (repo / "src/nagents/web/static").rglob("*") if path.is_file()
    )
    hashes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="ngn-benchmark-source-") as directory:
        snapshot = Path(directory)
        for name in selected:
            source = repo / name
            if not source.exists():
                continue  # A tracked deletion is part of the dirty snapshot.
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"Snapshot refuses non-regular source file: {name}")
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[name] = digest(target)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(destination),
                str(snapshot),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    wheels = list(destination.glob("nagents-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Build must produce exactly one nagents wheel")
    runner = destination / "runner.py"
    shutil.copyfile(Path(__file__).with_name("runner.py"), runner)
    constraints = destination / "runtime-constraints.txt"
    constraints.write_text(runtime_constraints())
    adapter_hashes: dict[str, str] = {}
    for relative in (
        "benchmarks/__init__.py",
        "benchmarks/terminal_bench/__init__.py",
        "benchmarks/terminal_bench/agent.py",
        "benchmarks/terminal_bench/summarize.py",
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / relative, target)
        adapter_hashes[relative] = digest(target)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "created_utc": datetime.now(UTC).isoformat(),
        "git_head": git("rev-parse", "HEAD").strip(),
        "git_dirty": bool(git("status", "--porcelain")),
        "source_files_sha256": hashes,
        "source_manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "wheel": {"filename": wheels[0].name, "sha256": digest(wheels[0])},
        "runner_sha256": digest(runner),
        "runtime_constraints_sha256": digest(constraints),
        "adapter_files_sha256": adapter_hashes,
        "build_environment_packages": {
            distribution.metadata["Name"]: distribution.version for distribution in distributions()
        },
        "harbor": {"version": HARBOR_VERSION, "commit": HARBOR_COMMIT},
        "dataset": {"version": "4.0.0", "repository": DATASET_REPO, "commit": DATASET_COMMIT},
        "tasks": list(tasks),
        "build_python": sys.version,
    }
    (destination / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def job_config(
    bundle: Path, credentials: Path, jobs: Path, timeout: int, model: str, tasks: tuple[str, ...] = TASKS
) -> dict[str, object]:
    tasks = selected_tasks(tasks)
    if not 1 <= timeout <= 1800:
        raise ValueError("Pilot timeout must be between 1 and 1800 seconds")
    return {
        "job_name": "ngn-terminal-bench-4-reliability-pilot",
        "jobs_dir": str(jobs.resolve()),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "delete": True,
            "cpu_enforcement_policy": "limit",
            "memory_enforcement_policy": "limit",
            "extra_docker_compose": [str((bundle / "isolation.json").resolve())],
        },
        "agents": [
            {
                "import_path": "benchmarks.terminal_bench.agent:NgnAgent",
                "model_name": model,
                "override_timeout_sec": timeout + 30,
                "override_setup_timeout_sec": 600,
                "kwargs": {
                    "bundle": str(bundle.resolve()),
                    "credentials_path": str(credentials.resolve()),
                    "timeout_seconds": timeout,
                },
            }
        ],
        "tasks": [
            {"path": f"tasks/{name}", "git_url": DATASET_REPO, "git_commit_id": DATASET_COMMIT} for name in tasks
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--credentials", type=Path, required=True, help="Only records the explicit path; does not read it"
    )
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument(
        "--task", action="append", choices=TASKS, help="Repeat to select pinned pilot tasks; defaults to all three"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    bundle = args.bundle.resolve()
    tasks = selected_tasks(tuple(args.task) if args.task else TASKS)
    manifest = build_bundle(root, bundle, tasks)
    (bundle / "isolation.json").write_text(
        json.dumps({"services": {"main": {"pids_limit": 512, "security_opt": ["no-new-privileges:true"]}}}, indent=2)
        + "\n"
    )
    config = job_config(bundle, args.credentials, args.jobs, args.timeout, args.model, tasks)
    (bundle / "job.json").write_text(json.dumps(config, indent=2) + "\n")
    manifest["isolation_sha256"] = digest(bundle / "isolation.json")
    manifest["job_sha256"] = digest(bundle / "job.json")
    (bundle / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(bundle / "job.json")
