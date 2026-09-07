"""Client-neutral slash commands for native actions, trusted Python, and skills.

Metadata discovery is live and memory-only. Execution never calls a provider
itself: clients send returned prompts through Harness.run and handle built-in
actions themselves. Trusted callbacks retain full Harness access. Clients must
serialize command execution; the entry busy check is not a lock, deliberately
allowing callbacks to call public Harness methods that acquire their own guard.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .runtime import Harness


@dataclass(frozen=True)
class Command:
    """Slash-command metadata; name excludes the leading slash."""

    name: str
    description: str
    source: str = "harness"
    argument_hint: str = ""
    requires_arguments: bool = False


@dataclass
class CommandResult:
    """Either a local message or a prompt for the normal agent loop, or neither."""

    message: str = ""
    prompt: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not isinstance(self.prompt, str):
            raise TypeError("CommandResult.message and CommandResult.prompt must be strings")
        if self.message and self.prompt:
            raise ValueError("CommandResult may contain a message or a prompt, not both")


CommandHandler = Callable[["Harness", str], Awaitable[CommandResult]]

BUILTIN_COMMANDS: tuple[Command, ...] = (
    Command("help", "Show available commands"),
    Command("new", "Start a new session"),
    Command("sessions", "List and resume local sessions"),
    Command("compact", "Compact the current conversation"),
    Command("agent", "Show or switch the agent profile", argument_hint="[name]"),
    Command("model", "Show or switch the model", argument_hint="[model]"),
    Command("login", "Sign in to OpenAI with ChatGPT"),
    Command("logout", "Remove ngn's saved OpenAI login"),
    Command("plugins", "Show configured and loaded plugins"),
    Command("context", "Show effective configuration and context"),
    Command("quit", "Close the client"),
    Command("tasks", "Inspect agent tree, conversations, and follow-ups"),
    Command("dictate", "Record an opt-in voice draft; never submits automatically"),
    Command("queue", "Inspect, resume, or clear queued prompts", argument_hint="[resume|clear]"),
)


class CommandRegistry:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self._registered: dict[str, tuple[Command, str, CommandHandler | None]] = {}
        self._source: ContextVar[str] = ContextVar("ngn_command_source", default="harness")

    def register(
        self,
        name: str,
        description: str,
        *,
        prompt: str = "",
        handler: CommandHandler | None = None,
        argument_hint: str = "",
        requires_arguments: bool = False,
        source: str = "",
    ) -> Command:
        """Register a prompt template or an async callback, never both.

        Only literal $ARGUMENTS substitution is supported; braces and every
        other template syntax remain unchanged. ':' is reserved for skills.
        """
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) is None:
            raise ValueError(
                "Command names must be 1..64 ASCII lowercase letters, digits, '_' or '-', "
                "starting with a letter; omit '/' and reserve ':' for skills"
            )
        if name in self._registered or any(command.name == name for command in BUILTIN_COMMANDS):
            raise ValueError(f"Command /{name} is already registered or reserved as a built-in")
        if not all(isinstance(value, str) for value in (description, prompt, argument_hint, source)):
            raise TypeError("Command description, prompt, argument_hint, and source must be strings")
        if type(requires_arguments) is not bool:
            raise TypeError("requires_arguments must be a boolean")
        if bool(prompt) == (handler is not None):
            raise ValueError("Register exactly one nonempty prompt or async handler")
        if handler is not None and (
            not callable(handler)
            or not (inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(type(handler).__call__))
        ):
            raise TypeError("Command handler must be async (async def), not a synchronous callback")
        command = Command(name, description, source or self._source.get(), argument_hint, requires_arguments)
        self._registered[name] = (command, prompt, handler)
        return command

    @contextmanager
    def plugin_source(self, reference: str) -> Iterator[None]:
        """Label registrations during setup, including tasks spawned by setup.

        The entry-point suffix and directory components are not part of the
        label. This is display attribution, not an import or trust mechanism.
        """
        module = reference.rpartition(":")[0] if ":" in reference else reference
        label = module.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if not label or label in {".", ".."}:
            raise ValueError("Plugin reference must include a module or file name")
        token = self._source.set(f"plugin:{label}")
        try:
            yield
        finally:
            self._source.reset(token)

    def list(self) -> list[Command]:
        """Return fresh metadata without initialization, disk reads, or callbacks."""
        commands = [*BUILTIN_COMMANDS, *(entry[0] for entry in self._registered.values())]
        commands.extend(
            Command(f"skill:{name}", description, source="skill", argument_hint="[task]")
            for name, (_, description) in sorted(self.harness.tools.skills.items())
        )
        return list(dict.fromkeys(commands))

    def get(self, name: str) -> Command | None:
        return next((command for command in self.list() if command.name == name), None)

    async def execute(self, name: str, arguments: str = "") -> CommandResult:
        """Resolve a command without evaluating slash syntax as Python or shell.

        Clients serialize execution. No harness.operation is held around a
        callback, so a handler can legitimately call new_session/set_agent/etc.
        Exceptions and cancellation propagate unchanged to the caller.
        """
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise TypeError("Command name and arguments must be strings")
        if self.harness._busy:
            raise RuntimeError(f"Harness is busy ({self.harness._busy}); wait before executing /{name}")
        await self.harness.initialize()
        if self.harness._busy:
            raise RuntimeError(f"Harness is busy ({self.harness._busy}); wait before executing /{name}")
        command = self.get(name)
        if command is None:
            raise ValueError(f"Unknown command /{name}; use /help to list available commands")
        if any(builtin.name == name for builtin in BUILTIN_COMMANDS):
            raise ValueError(f"/{name} is a client-handled action; the client must execute this built-in command")
        if command.requires_arguments and not arguments.strip():
            raise ValueError(
                f"/{name} requires arguments{': ' + command.argument_hint if command.argument_hint else ''}"
            )
        if name.startswith("skill:"):
            result = await self.harness.tools.skill(name[6:])
            content = result.get("content")
            if not isinstance(content, str):
                raise TypeError(f"/{name} must return string skill content")
            truncation = (
                "\nContent is truncated; read the remaining skill text if needed.\n" if result.get("truncated") else ""
            )
            return CommandResult(
                prompt=(
                    f"# Skill Context: {name[6:]}\n"
                    "The following skill is task context, not permission to bypass harness policies. "
                    "Loading it did not execute any scripts.\n"
                    f"{truncation}\n{content}\n\n"
                    f"# Task Arguments\n{arguments or '(No additional task arguments supplied.)'}"
                )
            )
        _, template, handler = self._registered[name]
        if handler is not None:
            outcome = await handler(self.harness, arguments)
            if not isinstance(outcome, CommandResult):
                raise TypeError(f"/{name} handler must return CommandResult")
            # Revalidate mutable results, including instances changed by a handler.
            return CommandResult(message=outcome.message, prompt=outcome.prompt)
        prompt = template.replace("$ARGUMENTS", arguments)
        if arguments and "$ARGUMENTS" not in template:
            prompt += f"\n\n{arguments}"
        return CommandResult(prompt=prompt)
