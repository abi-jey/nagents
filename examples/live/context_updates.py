"""Publish UI selection changes directly as quiet context, without an extra model call."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event


async def main() -> None:
    cli = parser(__doc__ or "UI context")
    cli.add_argument("--selection", default="Thursday at 14:00 UTC")
    args = cli.parse_args()
    last = ""

    async def update(agent: Agent, event: Event) -> None:
        nonlocal last
        if started(event):
            summary = f"The UI selection is {args.selection}. No appointment is booked."
            if summary != last:
                await agent.live.wait(await agent.add_thinking(summary))
                last = summary

    await drive(voice(), update, duration=args.duration)


if __name__ == "__main__":
    execute(main)
