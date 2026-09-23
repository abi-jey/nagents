"""Hosted Responses chooses functions; Nagents executes the whole tool batch natively."""

from datetime import UTC
from datetime import datetime

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents.live import LiveConfig


def utc_time() -> str:
    """Read the current UTC time."""
    return datetime.now(UTC).isoformat()


def multiply(left: float, right: float) -> float:
    """Multiply two numbers exactly as supplied."""
    return left * right


async def main() -> None:
    args = parser(__doc__ or "Hosted functions").parse_args()
    config = LiveConfig(backend_options={"parallel_tool_calls": True})
    await drive(voice(config, tools=[utc_time, multiply]), duration=args.duration)


if __name__ == "__main__":
    execute(main)
