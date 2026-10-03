"""Container entry point: shipping Harness, native tools, explicit approvals.

This module is uploaded separately from the measured wheel. It adds no solving
loop, tool implementation, retry policy, or task-specific prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import stat
import tempfile
import time
from contextlib import aclosing
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from nagents.cli import _event_record
from nagents.cli import _json_default
from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness.auth import OpenAIAuth
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import _load_config

if TYPE_CHECKING:
    from nagents.harness.types import ApprovalRequest

PRIVATE_ROOT = Path("/installed-agent/ngn-benchmark")
PRIVATE_CODEX_HOME = PRIVATE_ROOT / "codex-access"


def configuration(workspace: Path, model: str, private_root: Path = PRIVATE_ROOT) -> HarnessConfig:
    """Use shipping YAML configuration with fresh, process-local XDG stores.

    Both global and workspace provider registries live below XDG_CONFIG_HOME in
    the released Harness. An empty private directory excludes image/host state
    without replacing providers, loaders, or native child construction.
    """
    private_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=private_root)).resolve()
    for name, child in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        target = directory / child
        target.mkdir(mode=0o700)
        os.environ[name] = str(target)
    return HarnessConfig(
        workspace=workspace,
        data_dir=directory / "sessions",
        provider="openai",
        provider_id="",
        auth="chatgpt",
        model=model,
        model_explicit=True,
    )


def request_deadline(value: object) -> float:
    # Older frozen runtimes remain usable with the unchanged callback fixture.
    try:
        from nagents.harness.providers import validate_request_timeout
    except ImportError:
        raise ValueError("A named request deadline requires a nagents 0.18-compatible runtime") from None
    return validate_request_timeout(value)


async def named_deadline(config: HarnessConfig, credentials: CodexCredentials, seconds: object) -> HarnessConfig:
    """Exercise a real named profile using only the provisioned access identity."""
    timeout = request_deadline(seconds)
    path = PRIVATE_CODEX_HOME / "auth.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": credentials.access_token, "account_id": credentials.account_id},
            },
            stream,
        )
    os.environ["CODEX_HOME"] = str(PRIVATE_CODEX_HOME)
    # Codex derives residency from token claims. Refuse any identity/routing
    # difference instead of silently measuring another account or region.
    resolved = await _load_config(home=PRIVATE_CODEX_HOME, model=config.model).credentials()
    expected = replace(credentials, residency="" if credentials.residency == "no_constraint" else credentials.residency)
    if resolved != expected:
        raise ValueError("Private Codex access identity or routing differs from provisioned credentials")
    profile = ProviderProfile(kind="openai", auth="codex", request_timeout=timeout)
    ScopedProviderRegistryStore(config.workspace).workspace_store.save(
        ProviderRegistry(active="benchmark", providers={"benchmark": profile}), expected="0" * 64
    )
    return replace(config, provider_id="benchmark", auth="codex")


def container_gate(marker: Path = Path("/installed-agent/ngn-benchmark/isolated")) -> None:
    """Accident prevention only. Docker inspection in the adapter is the boundary."""
    if (
        os.environ.get("NGN_BENCHMARK_ISOLATED") != "1"
        or not Path("/.dockerenv").is_file()
        or marker.read_text().strip() != "harbor-docker"
        or Path("/var/run/docker.sock").exists()
        or Path("/run/docker.sock").exists()
        or Path("/tests").exists()
        or Path("/solution").exists()
    ):
        raise RuntimeError("Benchmark approval requires the reviewed isolated Harbor Docker adapter")


def load_credentials(path: Path) -> CodexCredentials:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 32768:
        raise ValueError("Benchmark credentials must be a private regular file of at most 32 KiB")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {"access_token", "account_id", "residency"}:
        raise ValueError("Benchmark credentials must contain only access_token, account_id, residency")
    if any(not isinstance(item, str) for item in value.values()) or not value["access_token"]:
        raise ValueError("Benchmark access credentials are malformed")
    return CodexCredentials(**value)


class EventLog:
    """Flush each real event so a killed trial retains its failure evidence."""

    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.path = path
        self.secrets = tuple(value for value in secrets if value)
        self.started = time.monotonic()
        self.sequence = 0

    def write(self, record: dict[str, object]) -> None:
        self.sequence += 1
        value = {
            **record,
            "benchmark_sequence": self.sequence,
            "elapsed_seconds": time.monotonic() - self.started,
        }
        text = json.dumps(value, default=_json_default, ensure_ascii=True)
        for secret in self.secrets:
            # Match the JSON-escaped spelling too; tokens normally contain ASCII.
            text = text.replace(json.dumps(secret, ensure_ascii=True)[1:-1], "[REDACTED]")
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    async def approve(self, request: ApprovalRequest) -> bool:
        self.write({"event": "benchmark_approval", "decision": "allow", "request": asdict(request)})
        return True


class AccessOnlyAuth(OpenAIAuth):
    """Explicit short-lived auth injection, shared by shipping native subagents."""

    def __init__(self, credentials: CodexCredentials, path: Path) -> None:
        super().__init__(path)
        self._benchmark_credentials = credentials

    def logged_in(self) -> bool:
        return True

    async def credentials(self) -> CodexCredentials:
        return self._benchmark_credentials


async def run(args: argparse.Namespace) -> int:
    container_gate()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    credential_path = Path(args.credentials)
    credentials = load_credentials(credential_path)
    log = EventLog(output / "events.jsonl", (credentials.access_token,))
    harness: Harness | None = None
    request_timeout = getattr(args, "request_timeout", None)
    codex_owned = False
    completed = False
    saw_done = False
    failed = False
    compacting = False
    status = "exception"
    try:
        config = configuration(Path.cwd(), args.model, PRIVATE_ROOT)
        if request_timeout is not None:
            request_deadline(request_timeout)
            PRIVATE_CODEX_HOME.mkdir(mode=0o700)
            codex_owned = True
            config = await named_deadline(config, credentials, request_timeout)
        harness = Harness(config)
        # The default fixture shares this access-only callback. Explicit named
        # deadlines use ordinary Codex discovery from the private access file;
        # both routes retain shipping provider and native child construction.
        harness.openai_auth = AccessOnlyAuth(credentials, credential_path)
        harness.approval_handler = log.approve
        log.write(
            {
                "event": "benchmark_start",
                "model": args.model,
                "workspace": str(config.workspace),
                "session_id": harness.session_id,
                "request_timeout": request_timeout if request_timeout is not None else 120.0,
                "authentication_fixture": "private_named_codex"
                if request_timeout is not None
                else "access_only_callback",
                "approval_policy": "allow-within-reviewed-harbor-container",
                "harness_config": {
                    "max_tool_rounds": config.max_tool_rounds,
                    "shell_timeout": config.shell_timeout,
                    "max_output": config.max_output,
                    "max_file_bytes": config.max_file_bytes,
                    "max_subagent_depth": config.max_subagent_depth,
                },
            }
        )
        async with asyncio.timeout(args.timeout):
            await harness.initialize()
            async with aclosing(harness.run(Path(args.instruction).read_text())) as events:
                async for event in events:
                    log.write({**_event_record(event), "event_scope": "root_harness_stream"})
                    if isinstance(event, CompactionStartedEvent):
                        compacting = True
                    elif isinstance(event, CompactionDoneEvent):
                        compacting = False
                    elif isinstance(event, DoneEvent) and not compacting and event.session_id == harness.session_id:
                        saw_done = True
                        completed = event.finish_reason is FinishReason.STOP
                    elif isinstance(event, ErrorEvent) and not event.recoverable:
                        failed = True
        status = "harness_error" if failed else "completed" if completed else "incomplete"
    except TimeoutError:
        status = "agent_timeout"
        log.write({"event": "benchmark_timeout", "limit_seconds": args.timeout})
    except asyncio.CancelledError:
        status = "cancelled"
        log.write({"event": "benchmark_cancelled"})
        raise
    except Exception as exc:
        log.write({"event": "benchmark_exception", "class": type(exc).__name__, "message": str(exc)})
    finally:
        try:
            # Native child streams are intentionally private to SubagentManager.
            # Preserve the supported inspection view without changing that loop.
            for task in harness.tasks.list() if harness is not None else ():
                assert harness is not None
                try:
                    history = await harness.tasks.history(task.id, limit=200)
                    log.write(
                        {
                            "event": "benchmark_task_history",
                            "task": asdict(task),
                            "messages": [asdict(message) for message in history],
                            "limit": 200,
                            "scope": "Current stored child history, possibly compacted; not a complete event stream",
                        }
                    )
                except Exception as exc:
                    log.write(
                        {"event": "benchmark_task_history_error", "task_id": task.id, "class": type(exc).__name__}
                    )
        finally:

            def cleanup_failed(resource: str, exc: Exception) -> None:
                nonlocal status
                # Do not erase the original failure/timeout/cancellation, but a
                # successful model turn alone cannot certify a clean trial exit.
                if status == "completed":
                    status = "cleanup_error"
                log.write(
                    {
                        "event": "benchmark_exception",
                        "phase": "cleanup",
                        "resource": resource,
                        "class": type(exc).__name__,
                        "message": str(exc),
                    }
                )

            try:
                if harness is not None:
                    await harness.close()
            except asyncio.CancelledError:
                status = "cancelled"
                log.write({"event": "benchmark_cancelled", "phase": "cleanup"})
                raise
            except Exception as exc:
                cleanup_failed("harness", exc)
            finally:
                try:
                    paths = [credential_path]
                    if codex_owned:
                        paths.append(PRIVATE_CODEX_HOME / "auth.json")
                    for path in paths:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError as exc:
                            cleanup_failed("credentials", exc)
                    if codex_owned and PRIVATE_CODEX_HOME.exists():
                        try:
                            PRIVATE_CODEX_HOME.rmdir()
                        except OSError as exc:
                            cleanup_failed("private_codex_directory", exc)
                finally:
                    log.write(
                        {
                            "event": "benchmark_end",
                            "status": status,
                            "saw_done": saw_done,
                            "session_id": harness.session_id if harness is not None else "",
                        }
                    )
    return 0 if status == "completed" else 1


async def main(args: argparse.Namespace) -> int:
    task = asyncio.current_task()
    if task is not None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
    return await run(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--request-timeout", type=float, default=None)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
