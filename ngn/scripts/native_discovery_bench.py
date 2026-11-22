"""Compare ABI3 discovery and search with the old Rust host on identical files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.types import JsonValue

REPO = Path(__file__).resolve().parents[2]


async def median(operation: Callable[[], Awaitable[dict[str, JsonValue]]]) -> tuple[int, dict[str, JsonValue]]:
    await operation()
    durations = []
    result: dict[str, JsonValue] = {}
    for _ in range(7):
        start = time.perf_counter_ns()
        result = await operation()
        durations.append(time.perf_counter_ns() - start)
    return int(statistics.median(durations)), result


async def benchmark(root: Path) -> dict[str, object]:
    # Stay below the shipped path cap; match near the tail so search has to
    # read the whole workspace instead of timing an early exit.
    for index in range(42):
        folder = root / f"src-{index:03d}"
        folder.mkdir()
        for number in range(100):
            text = "file content\nbenchmark-token at end\n" if index >= 40 and number == 10 else "file content\n"
            (folder / f"file-{number:03d}.txt").write_text(text, encoding="utf-8")
    (root / ".gitignore").write_text(".gitignore\nignored/\n", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored/discard.txt").write_text("benchmark-token not visible\n", encoding="utf-8")
    command = [
        "cargo",
        "test",
        "--locked",
        "--release",
        "--manifest-path",
        str(REPO / "ngn/Cargo.toml"),
        "-p",
        "ngn-core",
        "tools::discovery::tests::native_abi3_comparison_baseline",
        "--",
        "--ignored",
        "--exact",
        "--nocapture",
    ]
    process = await asyncio.to_thread(
        subprocess.run,
        command,
        env={**os.environ, "NGN_DISCOVERY_BENCH_ROOT": str(root)},
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr + process.stdout)
    baseline = json.loads(process.stdout.split("NGN_DISCOVERY_BASELINE:", 1)[1].splitlines()[0])
    os.environ["NGN_NATIVE_DISCOVERY"] = "1"
    tools = Harness(HarnessConfig(workspace=root, data_dir=root / "not-workspace-data", demo=True)).tools
    discovery_ns, paths = await median(lambda: tools.find("*.txt", limit=1000))
    search_ns, matches = await median(lambda: tools.search("benchmark-token", limit=1000))
    assert paths == {"paths": baseline["paths"], "truncated": baseline["paths_capped"]}
    expected = [f"{item['path']}:{item['line']}: {item['text']}" for item in baseline["matches"]]
    assert matches == {"matches": expected, "truncated": baseline["search_capped"], "skipped_files": 0}
    comparison = {}
    for operation, native in (("discovery", discovery_ns), ("search", search_ns)):
        old = baseline[f"{operation}_ns"]
        comparison[operation] = {"legacy_ms": old / 1e6, "abi3_ms": native / 1e6, "ratio": native / old}
    return {"files": 4200, "paths_verified": len(baseline["paths"]), "matches_verified": len(expected), **comparison}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ngn-native-bench-") as directory:
        report = asyncio.run(benchmark(Path(directory).resolve()))
    payload = json.dumps(report, indent=2) + "\n"
    print(payload, end="")
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
