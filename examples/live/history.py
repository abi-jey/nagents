"""Seed a new Live connection with saved, text-only conversation history."""

import json
from pathlib import Path

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "History")
    cli.add_argument("history", type=Path, help="JSON list of role/content message objects in Live startup format")
    args = cli.parse_args()
    history = json.loads(args.history.read_text())
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise ValueError("History must be a JSON list of message objects")
    await drive(voice(LiveConfig(history=tuple(history))), duration=args.duration)


if __name__ == "__main__":
    execute(main)
