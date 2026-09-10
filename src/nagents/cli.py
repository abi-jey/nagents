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
from .harness.config import API_NAMES
from .harness.config import THEME_BACKGROUNDS
from .harness.config import THEME_NAMES
from .harness.types import ApprovalRequest
from .harness.types import HarnessEvent
from .harness.types import Notice
from .harness.types import TaskCompleted
from .harness.types import TaskMessage
from .harness.types import TaskNotification
from .harness.types import TaskStarted
from .harness.types import ToolOutput

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .harness.auth import DeviceAuthorization


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--workspace", "-C", type=Path, help="Working directory (default: current directory)")
    common.add_argument("--config", type=Path, help="Explicit, trusted TOML configuration")
    common.add_argument("--provider", help="Provider name, for example openai, anthropic, or gemini")
    common.add_argument("--model", "-m", help="Provider model ID")
    common.add_argument("--base-url", help="Provider API endpoint")
    common.add_argument("--api", choices=API_NAMES, help="Provider HTTP API (default: provider-specific auto)")
    common.add_argument("--api-key-env", help="Environment variable containing the API key, never the key itself")
    common.add_argument("--auth", choices=("auto", "api-key", "chatgpt"), help="Authentication method (default: auto)")
    common.add_argument("--agent", "-a", help="Agent profile (built-ins: agent, build, reviewer)")
    common.add_argument(
        "--max-subagent-depth",
        type=int,
        help="Delegation depth: 0 disables, 2 permits children and grandchildren (default)",
    )
    common.add_argument(
        "--theme", choices=THEME_NAMES, help="Terminal appearance (default: terminal inherits terminal colors)"
    )
    common.add_argument("--no-animations", action="store_false", dest="animations", help="Disable activity animation")
    common.add_argument(
        "--theme-background",
        choices=THEME_BACKGROUNDS,
        help="Use preset background (theme), inherited terminal background (terminal), or preset default (auto)",
    )
    common.add_argument(
        "--dictation",
        action=argparse.BooleanOptionalAction,
        dest="dictation_enabled",
        help="Enable opt-in /dictate microphone transcription (never records automatically)",
    )
    common.add_argument("--dictation-model", help="Transcription API model, separate from the coding model")
    common.add_argument("--dictation-base-url", help="OpenAI-compatible transcription API base URL")
    common.add_argument("--dictation-api-key-env", help="Environment variable containing the transcription API key")
    common.add_argument("--dictation-language", help="Two-letter transcription language, or empty for auto-detect")
    common.add_argument("--dictation-max-seconds", type=int, help="Maximum recording duration (1-300, default: 120)")
    common.add_argument(
        "--submit-mode", choices=("queue", "interrupt"), help="New prompts while working: queue or interrupt-and-send"
    )
    common.add_argument(
        "--tab-action",
        choices=("agent", "complete", "focus"),
        help="Tab cycles agents (default), completes commands, or moves focus",
    )
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
    serve = commands.add_parser("serve", parents=[common], help="Serve the local React web client (web extra)")
    serve.add_argument("--host", default="127.0.0.1", help="Loopback address only (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="Local HTTP port (default: 8765)")
    login = commands.add_parser("login", parents=[common], help="Sign in to OpenAI with a ChatGPT device code")
    method = login.add_mutually_exclusive_group()
    method.add_argument(
        "--status", action="store_true", help="Show local OpenAI login status, without a network request"
    )
    method.add_argument("--device-auth", action="store_true", help="Use device-code login (the default)")
    commands.add_parser("logout", parents=[common], help="Remove ngn's saved OpenAI login from this machine")
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
    if isinstance(event, Event):
        record["event"] = event.type.value
    else:
        record["event"] = {
            ToolOutput: "tool_output",
            Notice: "notice",
            TaskStarted: "task_started",
            TaskCompleted: "task_completed",
            TaskMessage: "task_message",
            TaskNotification: "task_notification",
        }[type(event)]
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
        if args.command == "login":
            if args.status:
                print(_plain(harness.auth_status() if harness.config.demo else harness.openai_auth.status()))
                return 0

            async def show_code(authorization: DeviceAuthorization) -> None:
                print("\nSign in with ChatGPT / Codex using device authorization.")
                print("Enable device-code login in your ChatGPT security or workspace settings if required.")
                print(_plain(f"\nOpen: {authorization.verification_url}"))
                print(_plain(f"Code: {authorization.user_code}"), flush=True)
                print("\nOnly continue if you initiated this login in ngn. Do not share the code.")
                print("Waiting for approval (up to 15 minutes). Ctrl+C cancels.", flush=True)

            await harness.login(show_code)
            print(_plain(f"Signed in. {harness.auth_status()}\nModel: {harness.config.model}"))
            return 0
        if args.command == "logout":
            await harness.logout()
            print("Removed ngn's local OpenAI login. Other clients and remote sessions were not changed.")
            return 0
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
                    if event.text:
                        print(flush=True)
                    streamed = False
                elif isinstance(event, ToolCallEvent):
                    print(_plain(f"\n[{event.name}]"), file=sys.stderr)
                    streamed = False
                elif isinstance(event, ToolOutput):
                    print(_plain(event.text), end="", file=sys.stderr, flush=True)
                elif isinstance(event, TaskStarted):
                    print(_plain(f"[Subagent {event.name} started: {event.task_id}]"), file=sys.stderr)
                elif isinstance(event, TaskCompleted):
                    print(
                        _plain(f"[Subagent {event.name}: {'failed' if event.error else 'completed'}]"), file=sys.stderr
                    )
                elif isinstance(event, TaskMessage):
                    print(_plain(f"[Human follow-up to {event.name}]\n{event.prompt}"), file=sys.stderr)
                elif isinstance(event, TaskNotification):
                    print(_plain(f"[{event.cause}: {event.source_name} -> {event.recipient_name}]"), file=sys.stderr)
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
                    print(flush=True)
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
            for name in (
                "provider",
                "model",
                "base_url",
                "api_key_env",
                "agent",
                "demo",
                "auth",
                "theme",
                "animations",
                "submit_mode",
                "tab_action",
                "api",
                "max_subagent_depth",
                "theme_background",
                "dictation_enabled",
                "dictation_model",
                "dictation_base_url",
                "dictation_api_key_env",
                "dictation_language",
                "dictation_max_seconds",
            )
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
        if args.command == "serve":
            from .web import serve

            serve(
                config,
                host=args.host,
                port=args.port,
                resume_session=getattr(args, "resume", ""),
                continue_session=getattr(args, "continue_session", False),
            )
            return 0
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
                "The TUI extra is not installed. From this source checkout, run poetry install -E tui "
                "or uv pip install -e '.[tui]' in the intended environment."
            ) from error

        # Defer initialization and resuming to the app's event loop; asyncio locks,
        # plugin clients, and subprocess resources must not cross event loops.
        app = NagentsApp(
            harness,
            resume_session=getattr(args, "resume", ""),
            continue_session=getattr(args, "continue_session", False),
        )
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
