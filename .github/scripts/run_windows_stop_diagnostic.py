"""Run exact Windows shard 2 with payload-free stop/cancellation tracing."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PRODUCT_SHA = "f5af6038de5a226b9790193539bdd964ffc6fc58"
SUMMARY = re.compile(r"^=+ .*?\d+ (?:passed|failed|skipped|error|errors).*?=+$")
FAILED_TEST = re.compile(r"\bFAILED tests[/\\]([^:\s]+)::([A-Za-z_][A-Za-z0-9_]*)")
WORKER_COUNT = re.compile(r"created: (\d+)/(\d+) workers")


def main() -> int:
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    product = workspace / "product"
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    artifact = runner_temp / "ngn-voice-stop-trace.log"
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=product, text=True).strip()
    if sha != PRODUCT_SHA:
        raise RuntimeError(f"diagnostic must use reviewed product SHA {PRODUCT_SHA}; got {sha}")

    shutil.copy2(workspace / ".github" / "scripts" / "ngn_stop_trace.py.txt", runner_temp / "ngn_stop_trace.py")
    selected = subprocess.check_output(
        [sys.executable, ".github/scripts/shard_tests.py", "--shards", "3", "--group", "2"],
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
        "-s",
        "-o",
        "faulthandler_timeout=60",
        "-p",
        "ngn_stop_trace",
    ]
    env = os.environ.copy()
    env.update(
        {
            "NGN_STOP_TRACE": "1",
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
        f"shard2_test_modules={len(selected)}",
        f"runner_cpu_count={os.cpu_count()}",
    ]
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("NGN_STOP_TRACE ") or SUMMARY.search(stripped):
            safe_lines.append(stripped)
            continue
        workers = WORKER_COUNT.search(stripped)
        if workers:
            safe_lines.append(f"xdist_worker_count={workers.group(1)}")
            continue
        failed = FAILED_TEST.search(stripped)
        if failed:
            safe_lines.append(f"failed_test=tests/{failed.group(1)}::{failed.group(2)}")

    safe_lines.append(f"pytest_exit={result.returncode}")
    artifact.write_text("\n".join(safe_lines) + "\n", encoding="utf-8")
    for line in safe_lines:
        print(line)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
