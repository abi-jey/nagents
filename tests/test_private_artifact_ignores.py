"""Check ignore rules using hypothetical paths, never reading private artifacts."""

import subprocess
from pathlib import Path


def test_private_runtime_paths_are_ignored() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        "deployments/private/probe",
        "deployment/private/probe",
        "sessions.db-wal",
        "sessions.db-shm",
        "sessions.sqlite3-wal",
        "sessions.sqlite3-shm",
        ".env.production",
        "credentials.json",
        "wire-synthetic.jsonl",
        "tool-images/image.png",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(paths) + "\n",
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(result.stdout.splitlines()) == set(paths)
