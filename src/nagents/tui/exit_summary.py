"""Terminal-only exit presentation; never imported by the Agent library."""

from __future__ import annotations

import os

from rich.console import Console
from rich.text import Text


def print_exit_summary(
    elapsed: str,
    session_id: str,
    resume: str,
    runtime_log: str,
    *,
    opened: bool,
    wire_log: str = "",
    console: Console | None = None,
) -> None:
    console = console or Console(no_color="NO_COLOR" in os.environ, highlight=False)
    console.print()
    heading = Text("ngn", style="bold cyan")
    heading.append(" closed", style="bold")
    heading.append(f"  ·  {elapsed}", style="dim")
    console.print(heading)
    for label, value, style in (
        ("Session", session_id or "not opened", ""),
        ("Resume", resume, "cyan"),
        ("Runtime log", runtime_log, "dim"),
    ):
        if not value:
            continue
        line = Text(f"  {label:<12}", style="dim")
        line.append(value, style=style)
        console.print(line, soft_wrap=True)
    if opened and not resume:
        console.print(Text("  No session was saved.", style="yellow"))
    if wire_log:
        line = Text("  Wire log    ", style="dim")
        line.append(wire_log)
        console.print(line, soft_wrap=True)
        console.print(Text("  Sensitive request/response payload logging enabled", style="yellow"))
    else:
        console.print(Text("  Wire log off · metadata only; no request/response payloads", style="dim"))
    console.print()
