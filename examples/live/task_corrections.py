"""Invalidate an obsolete lookup when application intent changes; do not replay side effects."""

import asyncio

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents import InputTranscriptDeltaEvent
from nagents.live import LiveConfig


async def main() -> None:
    args = parser(__doc__ or "Corrections").parse_args()
    selected = "Thursday"

    async def lookup(context: str) -> str:
        date = selected
        await asyncio.sleep(5)
        return f"The application checked {date}. No booking was made."

    async def correct(agent: Agent, event: Event) -> None:
        nonlocal selected
        if isinstance(event, InputTranscriptDeltaEvent) and "friday" in event.delta.lower() and selected != "Friday":
            selected = "Friday"
            agent.live.invalidate_tasks()
            await agent.add_thinking(
                "The selected day is now Friday, replacing Thursday. No booking is confirmed. Delegate again for the new request."
            )
        elif started(event):
            await agent.add_thinking("This correction demo starts with Thursday selected. Say Friday during a lookup.")

    await drive(voice(LiveConfig(delegation="client", client_handler=lookup)), correct, duration=args.duration)


if __name__ == "__main__":
    execute(main)
