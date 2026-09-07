"""Load with ngn --plugin examples/harness/commands.py:setup, then type /."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nagents.harness import CommandResult

if TYPE_CHECKING:
    from nagents.harness import Harness


async def show_harness(harness: Harness, arguments: str) -> CommandResult:
    return CommandResult(
        message=f"Model: {harness.agent.provider.model}\nTools: {', '.join(harness.agent.tool_registry.names())}"
    )


def setup(harness: Harness) -> None:
    harness.commands.register(
        "review-code",
        "Review a file or goal without making changes",
        prompt="Review the following target for correctness and missing tests. Do not change files.\n\n$ARGUMENTS",
        argument_hint="<file or goal>",
        requires_arguments=True,
    )
    harness.commands.register(
        "harness-info", "Show local harness information without a model call", handler=show_harness
    )
