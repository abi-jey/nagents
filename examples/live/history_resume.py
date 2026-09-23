"""Persist transcript fragments, then seed a replacement connection on the next run."""

import json
from pathlib import Path

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents import Agent
from nagents import AudioTranscriptDeltaEvent
from nagents import Event
from nagents import InputTranscriptDeltaEvent
from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "Resume")
    cli.add_argument("--state", type=Path, default=Path("voice-history.json"))
    args = cli.parse_args()
    history = json.loads(args.state.read_text()) if args.state.exists() else []

    async def save(agent: Agent, event: Event) -> None:
        if isinstance(event, AudioTranscriptDeltaEvent | InputTranscriptDeltaEvent):
            user = isinstance(event, InputTranscriptDeltaEvent)
            history.append(
                {
                    "type": "message",
                    "role": "user" if user else "assistant",
                    "content": [{"type": "input_text" if user else "output_text", "text": event.delta}],
                }
            )
            args.state.write_text(json.dumps(history[-64:]))

    await drive(voice(LiveConfig(history=tuple(history[-64:]))), save, duration=args.duration)


if __name__ == "__main__":
    execute(main)
