"""Create a stored source, or continue a finalized source on a new fork connection."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "Stored fork")
    cli.add_argument("--source", default="", help="Finalized stored Live session ID to fork")
    cli.add_argument("--store", action="store_true", help="Store this new session (project access required)")
    args = cli.parse_args()

    async def identify(agent: Agent, event: Event) -> None:
        if started(event):
            print("Save this session ID:", agent.live.status.session_id)

    agent = voice(LiveConfig(fork_from=args.source, store=args.store))
    await drive(agent, identify, duration=args.duration)
    print("Forkable only if stored and finalization confirmed:", agent.live.status.finalized)


if __name__ == "__main__":
    execute(main)
