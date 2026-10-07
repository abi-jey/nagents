"""Intent trials reuse the native Harness, safe workspace, and root/child meter."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from benchmarks.orchestration import run as native
from benchmarks.orchestration.fixtures import grade as grade_saved
from benchmarks.orchestration.fixtures import parse_answer

from .cases import cases

if TYPE_CHECKING:
    from benchmarks.orchestration.fixtures import Scenario

    from .cases import Case


def source_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in (root / "src/nagents", root / "benchmarks/orchestration", root / "benchmarks/intent")
        for path in sorted(directory.rglob("*.py"))
    }


def grade(scenario: Scenario, workspace: Path, answer: str, verified: set[str]) -> list[str]:
    actual = parse_answer(answer)
    clarification_failures: list[str] = []
    if scenario.name.endswith("ambiguous_edit"):
        question = actual.get("question") if isinstance(actual, dict) else None
        if not isinstance(question, str) or not question.strip():
            clarification_failures.append("missing_clarification_question")
        else:
            choices = (
                ("staging", "production", "environment", "file", "configuration")
                if scenario.name.startswith("development")
                else ("europe", "asia", "region", "file", "configuration")
            )
            if not any(choice in question.lower() for choice in choices):
                clarification_failures.append("clarification_does_not_select_target")
    # Contracts specify required fields, not an exclusive set of output fields.
    # Compare only required values; explanatory extras neither pass missing facts
    # nor turn an otherwise correct answer into a failure.
    if isinstance(actual, dict):
        answer = json.dumps({key: actual.get(key) for key in scenario.expected})
    return [*grade_saved(scenario, workspace, answer, verified), *clarification_failures]


def assess(case: Case, result: dict[str, object]) -> dict[str, object]:
    failures = list(result["failures"]) if isinstance(result["failures"], list) else ["invalid_failures"]
    events = result["tool_events"]
    assert isinstance(events, list)
    intent_failures: list[str] = []
    if case.zero_tools and events:
        intent_failures.append("unnecessary_tool_for_context_answer")
    if not case.scenario.corrected_files:
        for event in events:
            if isinstance(event, dict) and event.get("tool") in {"edit", "write", "shell"}:
                intent_failures.append("unrequested_mutation_attempt")
                break
    failures.extend(intent_failures)
    result["intent_failures"] = intent_failures
    result["intent_compliant"] = not intent_failures
    result["clarification_manual_review_required"] = case.scenario.name.endswith("ambiguous_edit")
    result["correct"] = not failures
    result["failures"] = failures
    result["split"] = case.split
    result["minimum_action_target"] = case.maximum_tools
    result["within_action_target"] = len(events) <= case.maximum_tools
    # Outcome correctness and complete telemetry are prerequisites for efficiency.
    usage = result["usage"]
    assert isinstance(usage, dict)
    result["efficiency_eligible"] = (
        not failures
        and result["run_complete"] is True
        and result["source_changed"] is False
        and usage["usage_complete"] is True
        and not result["provider_errors"]
        and not result["child_failures"]
    )
    return result


async def trial(case: Case, model: str, timeout: float) -> dict[str, object]:
    prompt_hash = native.sha256(case.scenario.goal)
    with patch.object(native, "source_hashes", source_hashes), patch.object(native, "grade", grade):
        result = await native.trial(case.scenario, "adaptive", model, timeout)
    if prompt_hash != native.sha256(case.scenario.goal):
        result["source_changed"] = True
    result["prompt_sha256_at_end"] = native.sha256(case.scenario.goal)
    return assess(case, result)


async def matrix(args: argparse.Namespace) -> dict[str, object]:
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.concurrency)
    selected = [case for case in cases() if args.split in {"all", case.split}]

    async def worker(case: Case) -> dict[str, object]:
        async with semaphore:
            path = args.output / f"{case.scenario.name}.json"
            command = [
                sys.executable,
                "-m",
                "benchmarks.intent.run",
                "--worker",
                "--case",
                case.scenario.name,
                "--model",
                args.model,
                "--timeout",
                str(args.timeout),
                "--output",
                str(path),
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), args.timeout + 45)
            except TimeoutError:
                process.kill()
                await process.communicate()
                return {"scenario": case.scenario.name, "correct": False, "worker_error": "parent_timeout"}
            if process.returncode or not path.exists():
                native.private_json(path.with_suffix(".error.json"), {"stderr": stderr.decode(errors="replace")})
                return {"scenario": case.scenario.name, "correct": False, "worker_error": "subprocess_failure"}
            result: dict[str, object] = json.loads(path.read_text())
            print(
                json.dumps({key: result[key] for key in ("scenario", "correct", "tool_calls", "failures")}), flush=True
            )
            return result

    results = await asyncio.gather(*(worker(case) for case in selected))
    summary: dict[str, object] = {
        "model": args.model,
        "split": args.split,
        "results": results,
        "trials": len(results),
        "correct": sum(result["correct"] is True for result in results),
    }
    native.private_json(args.output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--split", choices=("development", "heldout", "all"), default="development")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=tuple(case.scenario.name for case in cases()))
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 2 or not 10 <= args.timeout <= 600:
        parser.error("concurrency 1..2 and timeout 10..600 seconds required")
    if args.worker:
        if not args.case:
            parser.error("workers require one case")
        case = next(case for case in cases() if case.scenario.name == args.case)
        native.private_json(args.output, asyncio.run(trial(case, args.model, args.timeout)))
    else:
        summary = asyncio.run(matrix(args))
        print(json.dumps({key: value for key, value in summary.items() if key != "results"}))


if __name__ == "__main__":
    main()
