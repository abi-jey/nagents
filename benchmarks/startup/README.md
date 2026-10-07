# Offline startup benchmark

From the repository root, using its virtual environment:

```sh
PYTHONPATH=src .venv/bin/python -m benchmarks.startup.run --repeats 20 > /tmp/ngn-startup.json
```

For a worktree sharing another checkout's virtualenv, replace `.venv/bin/python`
with its absolute path and retain `PYTHONPATH=src`. The report records the imported
Harness source path. `--directory /path/to/storage` chooses the filesystem used
for fresh temporary databases; existing databases are never touched.

Each repeat measures legacy and atomic session migrations on separate empty
databases, alternating their order. It also measures the shipping Harness's
initialization on a fresh database and then a second Harness against that same
database. The benchmark uses offline demo mode, private configuration directories,
and normal SQLite durability. It makes no model calls.

Output includes individual samples and median/minimum/maximum seconds. Timings
cover `initialize()` only, excluding imports, object construction, and cleanup.
Compare the paired migration samples to isolate transaction overhead; the fresh
Harness includes additional session/configuration work. Filesystem latency,
concurrent workloads, and caching affect results. These measurements have no hard
timing assertions and are not provider latency, token-efficiency, or portable
performance guarantees.

One 20-repeat local run measured median migration initialization of 367 ms with
legacy autocommits and 24 ms with atomic initialization. Shipping Harness
initialization measured 82 ms fresh and 35 ms against an existing database. These
are observations from this machine; rerun on the target storage for comparisons.

Schema, data preservation, rollback, cancellation, and concurrent initialization
are verified separately:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/migrations/test_atomic_initialization.py
```
