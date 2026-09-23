"""Separate voice persona, backchannels and interruption policy from backend procedures."""

from _support import drive
from _support import execute
from _support import parser
from _support import voice

PROMPT = """You are a calm, concise appointment assistant.
Backchannel policy: Use moderate acknowledgments without competing with the caller.
Interruption policy: Stop speaking when interrupted and listen. Speech interruption is not task cancellation.
Delegation policy:
Backend tools: reasoning about the conversation; no booking service is configured.
Delegate when careful reasoning is needed. Do not invent appointments or availability.
Do not delegate greetings or simple clarifications. Clarify uncertain names and dates.
"""


async def main() -> None:
    args = parser(__doc__ or "Prompting").parse_args()
    await drive(voice(instructions=PROMPT), duration=args.duration)


if __name__ == "__main__":
    execute(main)
