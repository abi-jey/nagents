"""Correlate a command and its acknowledgment without treating acceptance as speech."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.live import LiveCommandError
from nagents.live import LiveEvent


async def main() -> None:
    args = parser(__doc__ or "Commands").parse_args()

    async def observe(agent: Agent, event: Event) -> None:
        if started(event):
            identifier = await agent.add_thinking("This is an acknowledgment demonstration.")
            try:
                acknowledgment = await agent.live.wait(identifier, timeout=15)
                print("Accepted command:", acknowledgment.get("client_event_id"))
            except (LiveCommandError, TimeoutError) as error:
                print("Command outcome:", error)
        elif isinstance(event, LiveEvent) and event.event_type == "error":
            print("Server rejected a command; continue observing this session.")

    await drive(voice(), observe, duration=args.duration)


if __name__ == "__main__":
    execute(main)
