"""Speak first in a chosen language while continuous microphone audio stays active."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event


async def main() -> None:
    cli = parser(__doc__ or "Greeting")
    cli.add_argument("--language", default="English")
    args = cli.parse_args()

    async def greet(agent: Agent, event: Event) -> None:
        if started(event):
            identifier = await agent.add_instructions(
                f"Greet the caller now in {args.language}. Introduce yourself as the support assistant. Ask how you can help, then pause and listen."
            )
            await agent.live.wait(identifier)  # Acceptance, not playback completion.

    await drive(voice(), greet, duration=args.duration)


if __name__ == "__main__":
    execute(main)
