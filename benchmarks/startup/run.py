"""Measure cold startup without model requests or changes to existing databases.

Run with a repository virtualenv: python -m benchmarks.startup.run --repeats 10.
Use --directory to choose the filesystem on which fresh databases are created.
Legacy/atomic migration order alternates to reduce ordering bias. Results are
local wall-clock samples, not portable performance guarantees.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

import nagents.harness.runtime as runtime
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.migrations.manager import MigrationManager
from nagents.migrations.sessions import migrations


async def measure(root: Path, repeats: int) -> dict[str, object]:
    samples: dict[str, list[float]] = {
        "legacy_migrations": [],
        "atomic_migrations": [],
        "fresh_harness": [],
        "existing_harness": [],
    }
    previous = {key: os.environ.get(key) for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    os.environ["XDG_DATA_HOME"] = str(root / "data")
    try:
        for index in range(repeats):
            for atomic in (False, True) if index % 2 == 0 else (True, False):
                label = "atomic_migrations" if atomic else "legacy_migrations"
                manager = MigrationManager(root / f"{label}-{index}.db", migrations=migrations, atomic=atomic)
                started = time.perf_counter()
                await manager.initialize()
                samples[label].append(time.perf_counter() - started)
            workspace = root / f"workspace-{index}"
            workspace.mkdir()
            for label in ("fresh_harness", "existing_harness"):
                harness = Harness(HarnessConfig(workspace=workspace, data_dir=workspace / "state", demo=True))
                try:
                    started = time.perf_counter()
                    await harness.initialize()
                    samples[label].append(time.perf_counter() - started)
                finally:
                    await harness.close()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {
        "source": runtime.__file__,
        "filesystem_directory": str(root.parent),
        "repeats": repeats,
        "seconds": {
            label: {"median": statistics.median(values), "min": min(values), "max": max(values), "samples": values}
            for label, values in samples.items()
        },
        "scope": "Wall time for initialize only; no model calls, imports, construction or close timing. Normal SQLite durability.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--directory", type=Path, default=Path(tempfile.gettempdir()))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 1000:
        parser.error("repeats must be 1..1000")
    with tempfile.TemporaryDirectory(prefix="ngn-startup-", dir=args.directory) as temporary:
        print(json.dumps(asyncio.run(measure(Path(temporary), args.repeats)), indent=2))


if __name__ == "__main__":
    main()
