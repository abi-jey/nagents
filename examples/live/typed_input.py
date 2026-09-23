"""Send an exact typed value to the hosted backend, rather than misrepresenting it as speech."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event


async def main() -> None:
    cli = parser(__doc__ or "Typed input")
    cli.add_argument("text", help="Exact user text to send to the backend")
    args = cli.parse_args()

    async def submit(agent: Agent, event: Event) -> None:
        if started(event):
            await agent.live.submit_text(args.text)

    await drive(voice(), submit, duration=args.duration)


if __name__ == "__main__":
    execute(main)
