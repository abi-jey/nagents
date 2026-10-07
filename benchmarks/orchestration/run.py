"""Run real Harness strategy comparisons in isolated per-trial subprocesses."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import runtime
from nagents.harness.config import AgentProfile
from nagents.harness.providers import ProviderProfile
from nagents.harness.tools import HarnessExecutor
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskStarted

from .fixtures import grade
from .fixtures import scenarios
from .fixtures import strategy_instruction
from .telemetry import Meter
from .telemetry import provider_factory

if TYPE_CHECKING:
    from nagents.events import ToolResultEvent
    from nagents.harness import ApprovalRequest
    from nagents.types import ToolCall

    from .fixtures import Scenario


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def private_json(path: Path, value: object) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def maximum_overlap(intervals: list[tuple[float, float]]) -> int:
    events = [(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals]
    active = maximum = 0
    for _, change in sorted(events):
        active += change
        maximum = max(active, maximum)
    return maximum


def source_roots() -> dict[str, str]:
    """Identify the imported implementation independently of the benchmark checkout."""
    return {
        "src/nagents": str(Path(runtime.__file__).resolve().parents[1]),
        "benchmarks/orchestration": str(Path(__file__).resolve().parent),
    }


def source_hashes() -> dict[str, str]:
    return {
        f"{prefix}/{path.relative_to(directory).as_posix()}": hashlib.sha256(path.read_bytes()).hexdigest()
        for prefix, root in source_roots().items()
        for directory in (Path(root),)
        for path in sorted(directory.rglob("*.py"))
    }


def root_completion(event: object, session_id: str, compacting: bool) -> bool:
    return isinstance(event, DoneEvent) and not compacting and event.session_id == session_id


async def trial(scenario: Scenario, strategy: str, model: str, timeout: float) -> dict[str, object]:
    source_roots_at_start = source_roots()
    source_at_start = source_hashes()
    meter = Meter()
    tool_events: list[dict[str, object]] = []
    modified: set[str] = set()
    verified: set[str] = set()
    inspected: set[str] = set()
    child_started: dict[str, float] = {}
    child_intervals: list[tuple[float, float]] = []
    child_outcomes: list[dict[str, object]] = []
    errors: list[str] = []
    final_text = ""
    root_done = False
    compacting = False
    root_finish_reason = "missing"
    initialization_seconds = 0.0
    run_seconds = 0.0
    original_execute = HarnessExecutor.execute

    async def execute(executor: HarnessExecutor, call: ToolCall) -> ToolResultEvent:
        result = await original_execute(executor, call)
        path = call.arguments.get("path", "")
        tool_events.append(
            {
                "tool": call.name,
                "role": "child" if executor.harness._is_subagent else "root",
                "arguments": call.arguments,
                "error": result.error or "",
                "duration_ms": result.duration_ms,
            }
        )
        if not result.error and isinstance(path, str):
            requested = Path(path)
            absolute = requested if requested.is_absolute() else executor.harness.config.workspace / requested
            try:
                path = absolute.resolve().relative_to(executor.harness.config.workspace.resolve()).as_posix()
            except ValueError:
                path = "outside_workspace"
            if call.name in {"edit", "write"}:
                modified.add(path)
                verified.discard(path)
            elif call.name == "read_file":
                inspected.add(path)
                if path in modified:
                    verified.add(path)
        return result

    async def approve(request: ApprovalRequest) -> bool:
        # Only the fixture's allowed correction is approved, and only at root.
        path = request.arguments.get("path")
        if request.task_id or request.tool != "edit" or not isinstance(path, str):
            return False
        target = Path(path)
        absolute = target if target.is_absolute() else workspace / target
        if absolute.is_symlink():
            return False
        try:
            relative = absolute.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            return False
        return relative in scenario.corrected_files

    with tempfile.TemporaryDirectory(prefix="ngn-orchestration-") as temporary:
        private = Path(temporary)
        workspace = private / "workspace"
        workspace.mkdir(mode=0o700)
        for name, text in scenario.files.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (workspace / ".ngn").mkdir()
        (workspace / ".ngn/tools.yaml").write_text(
            "version: 1\nagents:\n  assistant:\n    shell: false\n    schedule_wakeup: false\n"
            "    wake_up_in: false\n  reviewer:\n    shell: false\n    schedule_wakeup: false\n    wake_up_in: false\n"
        )
        os.environ["XDG_CONFIG_HOME"] = str(private / "config")
        os.environ["XDG_DATA_HOME"] = str(private / "data")
        instruction = strategy_instruction(strategy)
        config = HarnessConfig(
            workspace=workspace,
            data_dir=private / "state",
            model=model,
            model_explicit=True,
            providers={"": ProviderProfile(kind="openai", auth="api-key")},
            profiles={"reviewer": AgentProfile(mode="reviewer")},
            max_tool_rounds=12,
            max_subagent_depth=1,
        )
        with (
            patch.object(runtime, "HarnessProvider", provider_factory(meter)),
            patch.object(HarnessExecutor, "execute", execute),
        ):
            started = time.monotonic()
            harness = Harness(config, allow_subagents=strategy != "direct")
            harness.approval_handler = approve
            try:
                async with asyncio.timeout(timeout):
                    await harness.initialize()
                    initialization_seconds = time.monotonic() - started
                    run_start = time.monotonic()
                    async for event in harness.run(scenario.goal + "\n\nExecution strategy: " + instruction):
                        now = time.monotonic() - run_start
                        if isinstance(event, TaskStarted):
                            child_started[event.task_id] = now
                        elif isinstance(event, TaskCompleted):
                            child_intervals.append((child_started.pop(event.task_id, now), now))
                            child_outcomes.append(
                                {"status": event.status, "error": event.error, "result": event.result}
                            )
                        elif isinstance(event, CompactionStartedEvent):
                            compacting = True
                        elif isinstance(event, CompactionDoneEvent):
                            compacting = False
                        elif root_completion(event, harness.session_id, compacting):
                            assert isinstance(event, DoneEvent)
                            final_text = event.final_text
                            root_done = True
                            root_finish_reason = event.finish_reason.value
                        elif isinstance(event, ErrorEvent):
                            errors.append(event.code or "harness_error")
                    run_seconds = time.monotonic() - run_start
            except Exception as error:
                errors.append(type(error).__name__)
                run_seconds = max(0.0, time.monotonic() - started - initialization_seconds)
            finally:
                await harness.close()
        failures = grade(scenario, workspace, final_text, verified)
        task_correct = not failures
        inspection_complete = set(scenario.files).issubset(inspected)
        child_failures = sum(outcome["status"] != "completed" or bool(outcome["error"]) for outcome in child_outcomes)
        source_changed = source_at_start != source_hashes() or source_roots_at_start != source_roots()
        provider_errors = [error for call in meter.calls for error in call.errors]
        run_complete = root_done and root_finish_reason == FinishReason.STOP.value and not errors
        usage_complete = meter.summary()["usage_complete"] is True
        if errors:
            failures.append("runtime_errors")
        for start in child_started.values():
            child_intervals.append((start, run_seconds))
        concurrency = maximum_overlap(child_intervals)
        strategy_followed = (
            not child_intervals
            if strategy == "direct"
            else len(child_intervals) >= 2 and concurrency >= 2
            if strategy == "delegated"
            else True
        )
        return {
            "scenario": scenario.name,
            "strategy": strategy,
            "model": model,
            "correct": not failures,
            "task_correct": task_correct,
            "root_done": root_done,
            "root_finish_reason": root_finish_reason,
            "run_complete": run_complete,
            "provider_errors": provider_errors,
            "inspection_complete": inspection_complete,
            "inspected_files": sorted(inspected),
            "child_failures": child_failures,
            "source_changed": source_changed,
            "efficiency_eligible": inspection_complete
            and not child_failures
            and not source_changed
            and task_correct
            and run_complete
            and usage_complete
            and strategy_followed
            and not provider_errors,
            "strategy_followed": strategy_followed,
            "failures": failures,
            "errors": errors,
            "initialization_seconds": round(initialization_seconds, 4),
            "run_seconds": round(run_seconds, 4),
            "final_text": final_text,
            "child_count": len(child_intervals),
            "maximum_child_lifecycle_overlap": concurrency,
            "child_intervals_seconds": child_intervals,
            "child_outcomes": child_outcomes,
            "tool_calls": len(tool_events),
            "tool_errors": sum(bool(event["error"]) for event in tool_events),
            "tools_by_name": dict(Counter(str(event["tool"]) for event in tool_events)),
            "tool_events": tool_events,
            "usage": meter.summary(),
            "source_sha256": source_at_start,
            "source_roots": source_roots_at_start,
            "goal_sha256": sha256(scenario.goal),
            "strategy_sha256": sha256(instruction),
            "fixture_sha256": {name: sha256(content) for name, content in scenario.files.items()},
            "saved_files": {
                name: (workspace / name).read_text() for name in scenario.files if (workspace / name).is_file()
            },
            "bounds": {
                "timeout_seconds": timeout,
                "max_requests": meter.max_requests,
                "observed_token_admission_limit": meter.max_observed_tokens,
            },
        }


async def matrix(args: argparse.Namespace) -> dict[str, object]:
    output: Path = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.concurrency)
    selected = [case for case in scenarios() if args.scenario in {"all", case.name}]
    strategies = ("direct", "delegated") if args.strategy == "both" else (args.strategy,)

    async def worker(case: Scenario, strategy: str, repeat: int) -> dict[str, object]:
        async with semaphore:
            path = output / f"{case.name}-{strategy}-{repeat}.json"
            command = [
                sys.executable,
                "-m",
                "benchmarks.orchestration.run",
                "--worker",
                "--model",
                args.model,
                "--scenario",
                case.name,
                "--strategy",
                strategy,
                "--timeout",
                str(args.timeout),
                "--output",
                str(path),
            ]
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), args.timeout + 45)
            except TimeoutError:
                process.kill()
                await process.communicate()
                return {"scenario": case.name, "strategy": strategy, "correct": False, "worker_error": "parent_timeout"}
            if process.returncode or not path.exists():
                # Store private failure diagnostics without printing environment/auth data.
                private_json(
                    path.with_suffix(".error.json"),
                    {"exit_code": process.returncode, "stderr": stderr.decode(errors="replace")},
                )
                return {
                    "scenario": case.name,
                    "strategy": strategy,
                    "correct": False,
                    "worker_error": "subprocess_failure",
                }
            result: dict[str, object] = json.loads(path.read_text())
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in ("scenario", "strategy", "correct", "run_seconds", "child_count", "tool_errors")
                    }
                ),
                flush=True,
            )
            return result

    results = await asyncio.gather(
        *(
            worker(case, strategy, repeat)
            for repeat in range(args.repeats)
            for case in selected
            for strategy in strategies
        )
    )
    summary: dict[str, object] = {
        "model": args.model,
        "results": results,
        "trials": len(results),
        "correct": sum(result["correct"] is True for result in results),
    }
    private_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--strategy", choices=("direct", "delegated", "adaptive", "both"), default="both")
    parser.add_argument("--scenario", choices=("all", *(case.name for case in scenarios())), default="all")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4 or not 1 <= args.repeats <= 10 or not 10 <= args.timeout <= 600:
        parser.error("concurrency 1..4, repeats 1..10, timeout 10..600 seconds")
    if args.worker:
        if args.scenario == "all" or args.strategy == "both":
            parser.error("workers require one scenario and strategy")
        scenario = next(case for case in scenarios() if case.name == args.scenario)
        private_json(args.output, asyncio.run(trial(scenario, args.strategy, args.model, args.timeout)))
    else:
        summary = asyncio.run(matrix(args))
        print(json.dumps({key: value for key, value in summary.items() if key != "results"}))


if __name__ == "__main__":
    main()
