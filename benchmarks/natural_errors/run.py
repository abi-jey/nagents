"""Run natural user requests against native Harness tools in private subprocesses."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import patch

from benchmarks.orchestration.run import private_json
from benchmarks.orchestration.run import root_completion
from benchmarks.orchestration.run import sha256
from benchmarks.orchestration.telemetry import Meter
from benchmarks.orchestration.telemetry import provider_factory
from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import runtime
from nagents.harness.providers import ProviderProfile
from nagents.harness.tools import HarnessExecutor
from nagents.observation import json_default
from nagents.observation import observer
from nagents.observation import scope

from .audit import audit_trace
from .cases import cases
from .cases import grade

if TYPE_CHECKING:
    from nagents.events import ToolResultEvent
    from nagents.harness import ApprovalRequest
    from nagents.types import ToolCall

    from .cases import NaturalCase


def source_hashes() -> dict[str, str]:
    roots = {
        "runtime": Path(runtime.__file__).resolve().parents[1],
        "natural_errors": Path(__file__).resolve().parent,
        "orchestration": Path(__file__).resolve().parents[1] / "orchestration",
    }
    return {
        f"{prefix}/{path.relative_to(root)}": hashlib.sha256(path.read_bytes()).hexdigest()
        for prefix, root in roots.items()
        for path in sorted(root.rglob("*.py"))
    }


def snapshot(value: object) -> object:
    """Freeze application payloads before the runtime can mutate them."""
    return cast("object", json.loads(json.dumps(value, default=json_default)))


def permitted_path(workspace: Path, value: object, allowed: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    target = Path(value)
    absolute = target if target.is_absolute() else workspace / target
    try:
        relative = absolute.resolve().relative_to(workspace.resolve()).as_posix()
    except (ValueError, OSError):
        return False
    return relative in allowed


class Trace:
    def __init__(self) -> None:
        self.sequence = 0
        self.requests: list[dict[str, object]] = []
        self.tools: list[dict[str, object]] = []
        self.session_actors: dict[str, tuple[str, str]] = {}

    def tick(self) -> int:
        self.sequence += 1
        return self.sequence

    def observe(self, kind: str, data: dict[str, object]) -> None:
        if kind != "model_context":
            return
        payload = snapshot({key: data.get(key) for key in ("messages", "tools", "config")})
        self.requests.append(
            {
                "sequence": self.tick(),
                "generation_id": data.get("model_call_id", ""),
                "session_id": data.get("session_id", ""),
                "input_sha256": sha256(json.dumps(payload, sort_keys=True)),
                **(payload if isinstance(payload, dict) else {}),
            }
        )

    def annotate(self) -> None:
        for request in self.requests:
            actor, task = self.session_actors.get(str(request["session_id"]), ("unknown", ""))
            request.update(actor=actor, task_id=task)


async def trial(case: NaturalCase, model: str, timeout: float) -> dict[str, object]:
    before = source_hashes()
    meter = Meter()
    trace = Trace()
    errors: list[str] = []
    final_text = ""
    root_done = False
    compacting = False
    finish_reason = "missing"
    started = time.monotonic()
    original_execute = HarnessExecutor.execute
    mutation_done = False

    with tempfile.TemporaryDirectory(prefix="ngn-trial-") as directory:
        private = Path(directory)
        workspace = private / "workspace"
        workspace.mkdir(mode=0o700)
        for name, content in case.files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        (workspace / ".ngn").mkdir(exist_ok=True)
        (workspace / ".ngn/tools.yaml").write_text(
            "version: 1\nagents:\n  assistant:\n    shell: false\n    schedule_wakeup: false\n    wake_up_in: false\n"
        )
        for key, suffix in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
            os.environ[key] = str(private / suffix)

        async def approve(request: ApprovalRequest) -> bool:
            return request.tool in {"edit", "write"} and permitted_path(
                workspace, request.arguments.get("path"), case.authorized_paths
            )

        async def execute(executor: HarnessExecutor, call: ToolCall) -> ToolResultEvent:
            nonlocal mutation_done
            harness = executor.harness
            actor = "child" if harness._is_subagent else "root"
            task_id = harness._task_id
            trace.session_actors[harness.session_id] = (actor, task_id)
            context = scope.get()
            entry: dict[str, object] = {
                "sequence": trace.tick(),
                "actor": actor,
                "task_id": task_id,
                "session_id": harness.session_id,
                "generation_id": context.get("model_call_id", ""),
                "native_generation_id": context.get("tool_generation_id", ""),
                "tool": call.name,
                "call_id": call.id,
                "arguments": snapshot(call.arguments),
            }
            trace.tools.append(entry)
            try:
                result = await original_execute(executor, call)
            except (Exception, asyncio.CancelledError) as error:
                entry.update(outcome="interrupted", interruption=type(error).__name__, completed_sequence=trace.tick())
                raise
            entry.update(
                outcome="error" if result.error else "success",
                response=snapshot(result.result),
                error=result.error or "",
                duration_ms=result.duration_ms,
                completed_sequence=trace.tick(),
            )
            if (
                not mutation_done
                and case.external_edit_path
                and call.name == "read_file"
                and not result.error
                and permitted_path(workspace, call.arguments.get("path"), (case.external_edit_path,))
            ):
                (workspace / case.external_edit_path).write_bytes(case.external_edit_content.encode("utf-8"))
                mutation_done = True
            return result

        config = HarnessConfig(
            workspace=workspace,
            data_dir=private / "state",
            model=model,
            model_explicit=True,
            providers={"": ProviderProfile(kind="openai", auth="api-key")},
            max_tool_rounds=32,
            max_subagent_depth=0,
        )
        token = observer.set(trace.observe)
        try:
            with (
                patch.object(runtime, "HarnessProvider", provider_factory(meter)),
                patch.object(HarnessExecutor, "execute", execute),
            ):
                harness = Harness(config, allow_subagents=False)
                harness.approval_handler = approve
                trace.session_actors[harness.session_id] = ("root", "")
                try:
                    async with asyncio.timeout(timeout):
                        await harness.initialize()
                        async for event in harness.run(case.prompt):
                            if isinstance(event, CompactionStartedEvent):
                                compacting = True
                            elif isinstance(event, CompactionDoneEvent):
                                compacting = False
                            elif root_completion(event, harness.session_id, compacting):
                                assert isinstance(event, DoneEvent)
                                final_text, root_done, finish_reason = event.final_text, True, event.finish_reason.value
                            elif isinstance(event, ErrorEvent):
                                errors.append(event.code or "harness_error")
                except Exception as error:
                    errors.append(type(error).__name__)
                finally:
                    await harness.close()
        finally:
            observer.reset(token)
        trace.annotate()
        failures = grade(case, workspace, final_text)
        audit = audit_trace(trace.requests, trace.tools)
        run_complete = root_done and finish_reason == "stop" and not errors
        return {
            "case_id": case.case_id,
            "split": case.split,
            "seed": case.seed,
            "model": model,
            "prompt": case.prompt,
            "prompt_sha256": sha256(case.prompt),
            "requests": trace.requests,
            "tool_events": trace.tools,
            "interrupted_tool_attempts": sum(event.get("outcome") == "interrupted" for event in trace.tools),
            "audit": {
                **asdict(audit),
                "avoidable_failures": audit.avoidable_failures,
                "repeated_failures": audit.repeated_failures,
                "recoveries": audit.recoveries,
                "pending_failures": audit.pending_failures,
            },
            "task_correct": not failures and not case.expected_blocked and run_complete,
            "artifact_correct": not failures,
            "expected_blocked": case.expected_blocked,
            "manual_honesty_review_required": case.expected_blocked,
            "task_failures": failures,
            "errors": errors,
            "root_done": root_done,
            "root_finish_reason": finish_reason,
            "final_text": final_text,
            "run_seconds": round(time.monotonic() - started, 4),
            "usage": meter.summary(),
            "source_roots": {
                "runtime": str(Path(runtime.__file__).resolve().parents[1]),
                "runner": str(Path(__file__).resolve().parent),
            },
            "source_sha256_before": before,
            "source_sha256_after": source_hashes(),
            "source_changed": before != source_hashes(),
            "run_complete": run_complete,
            "external_edit_applied": mutation_done,
            "saved_files": {
                p.relative_to(workspace).as_posix(): p.read_bytes().decode("utf-8")
                for p in workspace.rglob("*")
                if p.is_file() and ".ngn" not in p.relative_to(workspace).parts
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
    selected = [case for case in cases() if args.case in {"all", case.case_id} and args.split in {"all", case.split}]

    async def worker(case: NaturalCase) -> dict[str, object]:
        async with semaphore:
            try:
                return await isolated_worker(case)
            except Exception as error:
                result: dict[str, object] = {
                    "case_id": case.case_id,
                    "split": case.split,
                    "seed": case.seed,
                    "worker_error": "worker_exception",
                    "error_class": type(error).__name__,
                }
                path = output / f"{case.split}-{case.seed}-{case.case_id}.error.json"
                if not path.exists():
                    private_json(path, result)
                return result

    async def isolated_worker(case: NaturalCase) -> dict[str, object]:
        path = output / f"{case.split}-{case.seed}-{case.case_id}.json"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "benchmarks.natural_errors.run",
            "--worker",
            "--case",
            case.case_id,
            "--split",
            case.split,
            "--model",
            args.model,
            "--timeout",
            str(args.timeout),
            "--output",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), args.timeout + 30)
        except TimeoutError:
            process.kill()
            _, stderr = await process.communicate()
            result: dict[str, object] = {"case_id": case.case_id, "worker_error": "parent_timeout"}
        else:
            if process.returncode or not path.exists():
                result = {"case_id": case.case_id, "worker_error": "subprocess_failure"}
            else:
                result = json.loads(path.read_text())
        result.setdefault("split", case.split)
        result.setdefault("seed", case.seed)
        if "worker_error" in result:
            private_json(path.with_suffix(".error.json"), {**result, "stderr": stderr.decode(errors="replace")})
        print(
            json.dumps({key: result[key] for key in ("case_id", "task_correct", "worker_error") if key in result}),
            flush=True,
        )
        return result

    results = await asyncio.gather(*(worker(case) for case in selected))
    summary: dict[str, object] = {"model": args.model, "trials": len(results), "results": results}
    private_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--case", default="all")
    parser.add_argument("--split", choices=("all", "development", "heldout"), default="development")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4:
        parser.error("concurrency must be 1..4")
    if not 10 <= args.timeout <= 600:
        parser.error("timeout must be 10..600 seconds")
    if args.case != "all" and args.case not in {case.case_id for case in cases()}:
        parser.error("unknown case")
    if args.worker:
        if args.case == "all" or args.split == "all":
            parser.error("workers require one case and split")
        case = next(case for case in cases() if case.case_id == args.case and case.split == args.split)
        private_json(args.output, asyncio.run(trial(case, args.model, args.timeout)))
    else:
        summary = asyncio.run(matrix(args))
        print(json.dumps({key: value for key, value in summary.items() if key != "results"}))


if __name__ == "__main__":
    main()
