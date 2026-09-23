"""Apply sparse hosted-backend settings and wait for server confirmation."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event


async def main() -> None:
    args = parser(__doc__ or "Backend settings").parse_args()

    async def configure(agent: Agent, event: Event) -> None:
        if started(event):
            command = await agent.live.update_backend(reasoning={"effort": "low"}, text={"verbosity": "low"})
            await agent.live.wait(command)
            print("Backend update accepted; voice identity and delegation mode remain fixed.")

    await drive(voice(), configure, duration=args.duration)


if __name__ == "__main__":
    execute(main)
