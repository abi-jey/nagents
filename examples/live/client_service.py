"""Client delegation can call application code without a second language model."""

from datetime import UTC
from datetime import datetime

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents.live import LiveConfig


async def clock_service(transcript: str) -> str:
    # This deliberately narrow service always returns the clock, not guessed intent.
    return "The clock service reports UTC time " + datetime.now(UTC).isoformat()


async def main() -> None:
    args = parser(__doc__ or "Client service").parse_args()
    agent = voice(
        LiveConfig(delegation="client", client_handler=clock_service),
        instructions="You have a UTC clock backend. Delegate time questions. Clarify other requests.",
    )
    await drive(agent, duration=args.duration)


if __name__ == "__main__":
    execute(main)
