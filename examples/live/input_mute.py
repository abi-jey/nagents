"""Mute model input briefly; microphone capture, speech and backend work are independent."""

import asyncio

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event


async def main() -> None:
    args = parser(__doc__ or "Input mute").parse_args()

    async def toggle(agent: Agent, event: Event) -> None:
        if started(event):
            await agent.live.wait(await agent.live.mute_input())
            print("Model input muted for three seconds; local microphone capture continues.")
            await asyncio.sleep(3)
            await agent.live.wait(await agent.live.unmute_input())

    await drive(voice(), toggle, duration=args.duration)


if __name__ == "__main__":
    execute(main)
