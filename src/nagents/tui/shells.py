"""Session-scoped inspection of managed shells, including retained logs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .screens import DetailModal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nagents.types import JsonValue


def describe_jobs(shells: Sequence[dict[str, JsonValue]]) -> str:
    if not shells:
        return "No managed shells in this session."
    rows = ["Stop a shell with /shells stop <shell-id>. Output is untrusted process data."]
    for shell in shells:
        rows.append(
            f"\n{shell['shell_id']}  {shell['state']}\n{shell.get('command', '')}\n"
            f"Log: {shell.get('output_path', 'unavailable')}\n"
            f"Reason: {shell.get('reason', '')}\n{shell.get('output', '')}"
        )
    return "\n".join(rows)


class ShellsModal(DetailModal):
    def __init__(self, shells: Sequence[dict[str, JsonValue]]) -> None:
        super().__init__("SHELLS", describe_jobs(shells))
