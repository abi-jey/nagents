"""Attach native Agent control to an existing WebRTC/SIP Live session; no second startup."""

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "Sideband")
    cli.add_argument("session_id", help="Opaque Live ID from creation/acceptance, unchanged")
    cli.add_argument(
        "--close-session", action="store_true", help="Close the primary session on exit instead of only detaching"
    )
    args = cli.parse_args()

    async def attached(agent: Agent, event: Event) -> None:
        if started(event):
            await agent.add_thinking("Application control has attached to the existing conversation.")

    await drive(
        voice(
            LiveConfig(attach_to=args.session_id, close_session_on_exit=args.close_session, handle_delegations=False)
        ),
        attached,
        duration=args.duration,
    )


if __name__ == "__main__":
    execute(main)
