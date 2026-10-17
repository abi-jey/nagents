"""Run the exact Windows shard with payload-free SQLite timing probes."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PRODUCT_SHA = "fc1f7d45eab4f0f28a3c9a7a95e2a41883399d0c"
SUMMARY = re.compile(r"^=+ .*?\d+ (?:passed|failed|skipped|error|errors).*?=+$")
FAILED_TEST = re.compile(r"\bFAILED tests[/\\]([^:\s]+)::([A-Za-z_][A-Za-z0-9_]*)")
WATCHDOG_FILE = re.compile(r'File ".*[\\/]([^\\/\"]+)", line (\d+) in ([^\s]+)')
WORKER_COUNT = re.compile(r"created: (\d+)/(\d+) workers")


def main() -> int:
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    product = workspace / "product"
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    artifact = runner_temp / "ngn-voice-instructions-sqlite-trace.log"
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=product, text=True).strip()
    if sha != PRODUCT_SHA:
        raise RuntimeError(f"diagnostic must use reviewed product SHA {PRODUCT_SHA}; got {sha}")

    for plugin in ("ngn_sqlite_trace.py", "ngn_snapshot_trace.py"):
        shutil.copy2(workspace / ".github" / "scripts" / f"{plugin}.txt", runner_temp / plugin)

    selected = subprocess.check_output(
        [
            sys.executable,
            ".github/scripts/shard_tests.py",
            "--shards",
            "3",
            "--group",
            "3",
        ],
        cwd=product,
        text=True,
    ).split()
    command = [
        sys.executable,
        "-m",
        "pytest",
        *selected,
        "-n",
        "auto",
        "-v",
        "-rs",
        "-o",
        "faulthandler_timeout=60",
        "-p",
        "ngn_sqlite_trace",
        "-p",
        "ngn_snapshot_trace",
    ]
    env = os.environ.copy()
    env.update(
        {
            "NGN_SQLITE_TRACE": "1",
            "NGN_SQLITE_TRACE_DUMP_AT_EXIT": "1",
            "NGN_SQLITE_TRACE_TEST_MATCH": (
                "test_cancelled_mutations_join_atomic_commit_or_rollback|"
                "test_idle_purge_is_batch_bounded_and_never_prunes_recovered_active_membership|"
                "test_purge_keeps_content_free_channel_dedup_and_backup_restores_frozen_trash"
            ),
            "NGN_SNAPSHOT_TRACE": "1",
            "NGN_SNAPSHOT_TRACE_DUMP_AT_EXIT": "1",
            "PYTHONPATH": os.pathsep.join((str(runner_temp), str(product / "src"), str(product))),
        }
    )
    result = subprocess.run(
        command,
        cwd=product,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    safe_lines = [
        f"source_sha={PRODUCT_SHA}",
        f"shard3_test_modules={len(selected)}",
        f"runner_cpu_count={os.cpu_count()}",
    ]
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("NGN_SQLITE_TRACE ", "NGN_SNAPSHOT_TRACE ")) or SUMMARY.search(stripped):
            safe_lines.append(stripped)
        else:
            workers = WORKER_COUNT.search(stripped)
            if workers:
                safe_lines.append(f"xdist_worker_count={workers.group(1)}")
                continue
            failed = FAILED_TEST.search(stripped)
            if failed:
                safe_lines.append(f"failed_test=tests/{failed.group(1)}::{failed.group(2)}")
            elif "Timeout (" in stripped and stripped.endswith(")!"):
                safe_lines.append("watchdog_timeout=" + stripped.partition("Timeout (")[2][:-2])
            else:
                stack = WATCHDOG_FILE.search(stripped)
                if stack:
                    safe_lines.append(f"watchdog_frame={stack.group(1)}:{stack.group(2)} {stack.group(3)}")

    safe_lines.append(f"pytest_exit={result.returncode}")
    artifact.write_text("\n".join(safe_lines) + "\n", encoding="utf-8")
    for line in safe_lines:
        print(line)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
