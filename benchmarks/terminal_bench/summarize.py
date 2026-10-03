"""Summarize observed harness failures separately from the task verifier reward."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def summarize_events(path: Path) -> dict[str, object]:
    counts: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    failures: list[dict[str, object]] = []
    recoverable_errors: list[dict[str, object]] = []
    status = "missing_events"
    elapsed = 0.0
    usage: dict[str, object] = {}
    if not path.is_file():
        return {"status": status, "counts": {}, "tools": {}, "failures": []}
    status = "interrupted_without_end_event"
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            counts["malformed_event_lines"] += 1
            continue
        if not isinstance(event, dict):
            counts["malformed_event_lines"] += 1
            continue
        kind = event.get("event", "")
        if not isinstance(kind, str):
            continue
        counts[kind] += 1
        elapsed = max(elapsed, float(event.get("elapsed_seconds", 0)))
        if kind == "benchmark_end":
            status = str(event.get("status", "unknown"))
        if kind == "benchmark_approval":
            counts[f"approval_{event.get('decision', 'unknown')}"] += 1
        if kind == "tool_call":
            tools[str(event.get("name", "unknown"))] += 1
        if kind == "tool_result":
            error = event.get("error")
            result = event.get("result")
            reason = ""
            if isinstance(error, str) and error:
                reason = "tool_error"
                counts["tool_errors"] += 1
                # These are labeled hints from shipping error strings, not an
                # invented structured exception taxonomy. Preserve the raw error.
                if error.startswith(("arguments", "Invalid arguments for ")):
                    counts["validation_error_hints"] += 1
                if any(word in error.lower() for word in ("denied", "approval", "not allowed", "protected path")):
                    counts["permission_error_hints"] += 1
            if isinstance(result, dict) and event.get("name") == "shell":
                if result.get("timed_out") is True:
                    counts["shell_timeouts"] += 1
                    reason = "shell_timeout"
                if result.get("truncated") is True:
                    counts["shell_output_truncations"] += 1
                code = result.get("exit_code")
                if isinstance(code, int) and code != 0:
                    counts["shell_nonzero_exits"] += 1
                    reason = reason or "shell_nonzero_exit"
            if reason:
                failures.append({"sequence": event.get("benchmark_sequence"), "kind": reason, "event": event})
        elif kind == "error" and event.get("recoverable") is True:
            counts["recoverable_errors"] += 1
            recoverable_errors.append(
                {"sequence": event.get("benchmark_sequence"), "kind": "recoverable_error", "event": event}
            )
        elif kind in {"error", "benchmark_exception", "benchmark_timeout", "benchmark_cancelled"}:
            if kind == "error":
                counts["terminal_errors"] += 1
            failures.append({"sequence": event.get("benchmark_sequence"), "kind": kind, "event": event})
        if kind == "done" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return {
        "status": status,
        "elapsed_seconds": elapsed,
        "counts": dict(counts),
        "tool_metric_scope": "Root Harness.run events; child lifecycle and approvals included, child tool events excluded",
        "tools": dict(tools),
        "failures": failures,
        "recoverable_errors": recoverable_errors,
        "root_done_usage": usage,
        "usage_scope": "Reported root DoneEvent only; not a total for compaction or background agents",
        "completion_is_verifier_success": False,
    }


def summarize_job(directory: Path) -> dict[str, object]:
    trials: list[dict[str, object]] = []
    for result_path in sorted(directory.glob("*/result.json")):
        result = json.loads(result_path.read_text())
        trial = result_path.parent
        summary = summarize_events(trial / "agent/events.jsonl")
        summary["trial"] = trial.name
        summary["verifier_result"] = result.get("verifier_result")
        summary["harbor_exception"] = result.get("exception_info")
        trials.append(summary)
    return {"job": str(directory), "trials": trials, "note": "Bounded reliability pilot, not a leaderboard score"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize_job(args.job), indent=2))
