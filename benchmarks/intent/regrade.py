"""Regrade frozen native outcomes without repeating model requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.orchestration.run import private_json

from .cases import cases
from .run import assess
from .run import grade
from .run import source_hashes

if TYPE_CHECKING:
    from .cases import Case


def regrade_result(case: Case, original: dict[str, object]) -> dict[str, object]:
    result = dict(original)
    saved = result["saved_files"]
    events = result["tool_events"]
    assert isinstance(saved, dict) and isinstance(events, list)
    modified: set[str] = set()
    verified: set[str] = set()
    for event in events:
        assert isinstance(event, dict)
        if event.get("error"):
            continue
        arguments = event["arguments"]
        assert isinstance(arguments, dict)
        requested = str(arguments.get("path", ""))
        matches = [name for name in case.scenario.files if requested == name or requested.endswith("/" + name)]
        if len(matches) != 1:
            continue
        name = matches[0]
        if event["tool"] in {"edit", "write"}:
            modified.add(name)
            verified.discard(name)
        elif event["tool"] == "read_file" and name in modified:
            verified.add(name)
    with tempfile.TemporaryDirectory(prefix="ngn-intent-regrade-") as temporary:
        workspace = Path(temporary)
        for name in case.scenario.files:
            if name in saved:
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(saved[name]))
        result["failures"] = grade(case.scenario, workspace, str(result["final_text"]), verified)
    result["task_correct"] = not result["failures"]
    if result["errors"]:
        assert isinstance(result["failures"], list)
        result["failures"].append("runtime_errors")
    result["original_failures"] = original["failures"]
    return assess(case, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original: dict[str, object] = json.loads(args.summary.read_text())
    raw_results = original["results"]
    assert isinstance(raw_results, list)
    by_name = {case.scenario.name: case for case in cases()}
    results = [regrade_result(by_name[result["scenario"]], result) for result in raw_results]
    private_json(
        args.output,
        {
            "original_summary": str(args.summary.resolve()),
            "original_summary_sha256": hashlib.sha256(args.summary.read_bytes()).hexdigest(),
            "grader_sha256": source_hashes(),
            "original_correct": original["correct"],
            "correct": sum(result["correct"] is True for result in results),
            "results": results,
        },
    )


if __name__ == "__main__":
    main()
