"""Keep independent caption streams and original timestamps, including overlapping speech."""

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


async def main() -> None:
    cli = parser(__doc__ or "Captions")
    cli.add_argument("--output", type=Path, default=Path("captions.jsonl"))
    args = cli.parse_args()
    with args.output.open("a", encoding="utf-8") as output:

        async def caption(agent: Agent, event: Event) -> None:
            if isinstance(event, AudioTranscriptDeltaEvent | InputTranscriptDeltaEvent):
                output.write(
                    json.dumps(
                        {
                            "speaker": "user" if isinstance(event, InputTranscriptDeltaEvent) else "assistant",
                            "text": event.delta,
                            "start_ms": event.extra.get("start_ms"),
                            "end_ms": event.extra.get("end_ms"),
                        }
                    )
                    + "\n"
                )
                output.flush()

        await drive(voice(), caption, duration=args.duration)


if __name__ == "__main__":
    execute(main)
