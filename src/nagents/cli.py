"""The ngn terminal and headless clients. Importing the library never imports Textual."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import aclosing
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

from .events import CompactionDoneEvent
from .events import CompactionStartedEvent
from .events import DoneEvent
from .events import ErrorEvent
from .events import Event
from .events import TextChunkEvent
from .events import TextDoneEvent
from .events import ToolCallEvent
from .events import ToolResultEvent
from .harness import Harness
from .harness import load_config
from .harness.types import ApprovalRequest
from .harness.types import HarnessEvent
from .harness.types import Notice
from .harness.types import ToolOutput

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--workspace", "-C", type=Path, help="Working directory (default: current directory)")
    common.add_argument("--config", type=Path, help="Explicit, trusted TOML configuration")
    common.add_argument("--provider", help="Provider name, for example openai, anthropic, or gemini")
    common.add_argument("--model", "-m", help="Provider model ID")
    common.add_argument("--base-url", help="Provider API endpoint")
    common.add_argument("--api-key-env", help="Environment variable containing the API key, never the key itself")
    common.add_argument("--agent", "-a", help="Agent profile (build or reviewer by default)")
    common.add_argument("--plugin", action="append", help="Explicitly trust and load a Python path.py:setup extension")
    common.add_argument(
        "--trust-project", action="store_true", help="Trust this project's .ngn/config.toml and its code"
    )
    common.add_argument(
        "--demo", action="store_true", help="Offline interface demo; no model calls or workspace writes"
    )
    resume = common.add_mutually_exclusive_group()
    resume.add_argument("--continue", "-c", dest="continue_session", action="store_true", help="Resume latest session")
    resume.add_argument("--resume", help="Resume a session ID in this workspace")

    parser = argparse.ArgumentParser(
        prog="ngn",
        description="A Python-extensible agent harness, at home in your terminal.",
        epilog="Run without a subcommand for the TUI. Try ngn --demo without API credentials.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"ngn (nagents {version('nagents')})")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", parents=[common], help="Run a prompt without the full-screen interface")
    run.add_argument("--json", action="store_true", help="Emit versioned JSON Lines events; approvals fail closed")
    run.add_argument("prompt", nargs="*", help="Prompt text, or - to read from standard input")
    commands.add_parser("sessions", parents=[common], help="List this workspace's saved sessions")
    commands.add_parser(
        "doctor", parents=[common], help="Show configuration and extension diagnostics without an LLM call"
    )
    return parser


def _plain(text: str) -> str:
    """Do not let tool output send control sequences to the user's terminal."""
    return "".join(char for char in text if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159))


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _event_record(event: HarnessEvent) -> dict[str, object]:
    record: dict[str, object] = asdict(event)
    record["schema_version"] = 1
    record["event"] = (
        event.type.value if isinstance(event, Event) else ("tool_output" if isinstance(event, ToolOutput) else "notice")
    )
    return record


async def _approve(request: ApprovalRequest, *, interactive: bool) -> bool:
    print(f"\nApproval required: {_plain(request.tool)}", file=sys.stderr)
    print(_plain(request.description), file=sys.stderr)
    print(_plain(json.dumps(request.arguments, ensure_ascii=False, indent=2)), file=sys.stderr)
    if request.preview:
        print(_plain(request.preview), file=sys.stderr)
    if not interactive:
        print("Denied: non-interactive runs cannot grant approval. Use ngn or an interactive ngn run.", file=sys.stderr)
        return False
    print("Allow this operation once? [y/N] ", end="", file=sys.stderr, flush=True)
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[str] = loop.create_future()

    def read_answer() -> None:
        if not ready.done():
            ready.set_result(sys.stdin.readline())

    try:
        loop.add_reader(sys.stdin.fileno(), read_answer)
    except (NotImplementedError, AttributeError):
        print("Interactive approval is unavailable here; use the TUI. Denied.", file=sys.stderr)
        return False
    try:
        answer = await ready
    finally:
        loop.remove_reader(sys.stdin.fileno())
    return answer.strip().lower() in {"y", "yes"}


async def _prepare(harness: Harness, args: argparse.Namespace) -> None:
    await harness.initialize()
    if getattr(args, "resume", ""):
        await harness.resume(args.resume)
    elif getattr(args, "continue_session", False):
        sessions = await harness.list_sessions()
        if sessions:
            await harness.resume(sessions[0].id)


async def _headless(harness: Harness, args: argparse.Namespace) -> int:
    try:
        await _prepare(harness, args)
        if args.command == "doctor":
            print(_plain(harness.describe()))
            return 0
        if args.command == "sessions":
            sessions = await harness.list_sessions()
            if not sessions:
                print("No saved sessions in this workspace.")
            for session in sessions:
                print(_plain(f"{session.id}  {session.updated_at}  {session.title}"))
            return 0

        prompt = " ".join(args.prompt)
        if prompt == "-" or (not prompt and not sys.stdin.isatty()):
            prompt = sys.stdin.read()
        if not prompt.strip():
            raise ValueError('A prompt is required. Example: ngn run "Explain this project"')

        async def approve(request: ApprovalRequest) -> bool:
            return await _approve(request, interactive=sys.stdin.isatty() and not args.json)

        harness.approval_handler = approve
        failed = False
        streamed = False
        compacting = False
        async with aclosing(harness.run(prompt)) as events:
            async for event in events:
                if isinstance(event, ErrorEvent):
                    failed = True
                if args.json:
                    print(json.dumps(_event_record(event), default=_json_default, ensure_ascii=False), flush=True)
                    continue
                if isinstance(event, CompactionStartedEvent):
                    compacting = True
                    print("[Compacting context]", file=sys.stderr)
                elif isinstance(event, CompactionDoneEvent):
                    compacting = False
                    print(
                        f"[Context: {event.original_message_count} -> {event.new_message_count} messages]",
                        file=sys.stderr,
                    )
                elif isinstance(event, TextChunkEvent) and not compacting:
                    print(_plain(event.chunk), end="", flush=True)
                    streamed = True
                elif isinstance(event, TextDoneEvent) and not compacting:
                    if not streamed:
                        print(_plain(event.text), end="", flush=True)
                    streamed = False
                elif isinstance(event, ToolCallEvent):
                    print(_plain(f"\n[{event.name}]"), file=sys.stderr)
                    streamed = False
                elif isinstance(event, ToolOutput):
                    print(_plain(event.text), end="", file=sys.stderr, flush=True)
                elif isinstance(event, ToolResultEvent):
                    if event.error:
                        print(_plain(f"[{event.name}: {event.error}]"), file=sys.stderr)
                    else:
                        print(_plain(f"[{event.name} completed in {event.duration_ms:.0f} ms]"), file=sys.stderr)
                elif isinstance(event, ErrorEvent):
                    print(_plain(f"Error: {event.message}"), file=sys.stderr)
                elif isinstance(event, Notice):
                    print(_plain(event.text), file=sys.stderr)
                elif isinstance(event, DoneEvent) and not compacting:
                    print()
                    print(_plain(f"Session: {harness.session_id}"), file=sys.stderr)
        return 1 if failed else 0
    finally:
        await harness.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Start the terminal client or execute a headless command."""
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "continue_session", False) and getattr(args, "resume", ""):
            raise ValueError("Choose either --continue or --resume, not both.")
        workspace = getattr(args, "workspace", Path.cwd()).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {workspace}")
        config = load_config(
            workspace, getattr(args, "config", None), trust_project=getattr(args, "trust_project", False)
        )
        overrides = {
            name: getattr(args, name)
            for name in ("provider", "model", "base_url", "api_key_env", "agent", "demo")
            if hasattr(args, name)
        }
        if hasattr(args, "plugin"):
            plugins = list(config.plugins)
            for entry in args.plugin:
                location, separator, function = entry.rpartition(":")
                if not separator:
                    raise ValueError("Python plugins use path.py:setup or installed.module:setup notation.")
                if location.endswith(".py"):
                    location = str(Path(location).expanduser().resolve())
                plugins.append(f"{location}:{function}")
            overrides["plugins"] = tuple(plugins)
        config = replace(config, **overrides)
        harness = Harness(config)
        if args.command:
            return asyncio.run(_headless(harness, args))
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("The TUI needs a terminal. Use ngn run --json PROMPT for scripts and pipes.")
        try:
            from .tui import NagentsApp
        except ModuleNotFoundError as error:
            if error.name not in {"textual", "rich"}:
                raise
            raise ValueError(
                "The TUI extra is not installed. Install nagents[tui] or run poetry install -E tui."
            ) from error

        # Defer initialization and resuming to the app's event loop; asyncio locks,
        # plugin clients, and subprocess resources must not cross event loops.
        app = NagentsApp(harness)
        if getattr(args, "resume", "") or getattr(args, "continue_session", False):
            original_initialize = harness.initialize

            async def initialize_and_resume() -> None:
                harness.initialize = original_initialize  # type: ignore[method-assign]
                await original_initialize()
                if getattr(args, "resume", ""):
                    await harness.resume(args.resume)
                else:
                    sessions = await harness.list_sessions()
                    if sessions:
                        await harness.resume(sessions[0].id)

            harness.initialize = initialize_and_resume  # type: ignore[method-assign]
        logging.basicConfig(level=logging.WARNING)
        app.run()
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Completed actions are not rolled back.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as error:
        print(_plain(f"ngn: {error}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
