"""Freeze an independent local diagnostic; preparation never launches a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.terminal_bench.prepare import build_bundle
from benchmarks.terminal_bench.prepare import digest

if TYPE_CHECKING:
    from collections.abc import Iterator

TASK_SOURCE = Path(__file__).parent / "tasks" / "review-followthrough"
JOB_NAME = "ngn-owned-review-followthrough-v2"
REPOSITORY = Path(__file__).resolve().parents[2]


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _task_files(directory: Path) -> Iterator[Path]:
    for path in sorted(directory.iterdir()):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Diagnostic task refuses symlinks: {path}")
        if stat.S_ISDIR(mode):
            if path.name not in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}:
                yield from _task_files(path)
        elif not stat.S_ISREG(mode):
            raise ValueError(f"Diagnostic task requires regular files: {path}")
        elif path.suffix not in {".pyc", ".pyo"}:
            yield path


def _copy_task(source: Path, destination: Path) -> dict[str, str]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Diagnostic task source must be a real directory")
    files = list(_task_files(source))
    destination.mkdir()
    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[str(relative)] = digest(target)
    return hashes


def _prepare(repo: Path, bundle: Path, jobs: Path, agents: list[dict[str, object]], *, verifier_only: bool) -> Path:
    repo, bundle, jobs = repo.resolve(), bundle.resolve(), jobs.resolve()
    # Reuse the audited wheel/runner/adapter snapshot. Only the dataset envelope
    # is replaced below: this task contains no Terminal-Bench material.
    manifest = build_bundle(repo, bundle)
    task = bundle / "diagnostic-task"
    files = _copy_task(TASK_SOURCE, task)
    shutil.copyfile(Path(__file__).with_name("audit.py"), bundle / "audit_workflow.py")
    task_manifest = {
        "kind": "ngn-owned-independent-diagnostic",
        "version": 2,
        "contains_terminal_bench_content": False,
        "files_sha256": files,
        "aggregate_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "network": "No task network required or allowed by instruction; no enforced egress firewall",
    }
    _write_json(bundle / "task-provenance.json", task_manifest)
    _write_json(
        bundle / "isolation.json",
        {"services": {"main": {"pids_limit": 512, "security_opt": ["no-new-privileges:true"]}}},
    )
    config = {
        "job_name": JOB_NAME + ("-verifier-check" if verifier_only else ""),
        "jobs_dir": str(jobs),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "delete": True,
            "cpu_enforcement_policy": "limit",
            "memory_enforcement_policy": "limit",
            "extra_docker_compose": [str(bundle / "isolation.json")],
        },
        "agents": agents,
        "tasks": [{"path": str(task)}],
    }
    _write_json(bundle / "job.json", config)
    manifest["dataset"] = {
        "kind": "ngn-owned-local-diagnostic",
        "version": "2",
        "terminal_bench": False,
        "task_manifest_sha256": digest(bundle / "task-provenance.json"),
    }
    manifest["tasks"] = ["ngn-diagnostics/review-followthrough"]
    manifest["run_kind"] = "verifier-setup-check-no-model" if verifier_only else "independent-harness-diagnostic"
    manifest["workflow_audit_sha256"] = digest(bundle / "audit_workflow.py")
    manifest["isolation_sha256"] = digest(bundle / "isolation.json")
    manifest["job_sha256"] = digest(bundle / "job.json")
    _write_json(bundle / "provenance.json", manifest)
    return bundle / "job.json"


def prepare_bundle(repo: Path, bundle: Path, jobs: Path, credentials: Path, *, model: str = "gpt-6-astra") -> Path:
    """Record only an explicitly supplied access-only credential path, never its contents."""
    agents: list[dict[str, object]] = [
        {
            "import_path": "benchmarks.terminal_bench.agent:NgnAgent",
            "model_name": model,
            "override_timeout_sec": 930,
            "override_setup_timeout_sec": 600,
            "kwargs": {
                "bundle": str(bundle.resolve()),
                "credentials_path": str(credentials.resolve()),
                "timeout_seconds": 900,
            },
        }
    ]
    return _prepare(repo, bundle, jobs, agents, verifier_only=False)


def prepare_verifier_check(repo: Path, bundle: Path, jobs: Path) -> Path:
    """Prepare Harbor's no-op agent to exercise separate verification without a model."""
    return _prepare(repo, bundle, jobs, [{"name": "nop"}], verifier_only=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPOSITORY)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument(
        "--credentials", type=Path, help="Explicit access-only credential path; not read during preparation"
    )
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--verifier-only", action="store_true", help="Use Harbor nop; no credentials or model calls")
    args = parser.parse_args()
    if args.verifier_only:
        if args.credentials is not None:
            parser.error("--verifier-only does not accept credentials")
        result = prepare_verifier_check(args.repo, args.bundle, args.jobs)
    else:
        if args.credentials is None:
            parser.error("--credentials is required unless --verifier-only is selected")
        result = prepare_bundle(args.repo, args.bundle, args.jobs, args.credentials, model=args.model)
    print(result)


if __name__ == "__main__":
    main()
