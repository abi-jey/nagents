"""Request exact disclosure wording; record acceptance separately from audio evidence."""

import logging

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import AudioTranscriptDeltaEvent
from nagents import Event

DISCLOSURE = "This call may be recorded for quality and training purposes."


async def main() -> None:
    args = parser(__doc__ or "Disclosure").parse_args()
    transcript = ""
    wording_reported = False

    async def observe(agent: Agent, event: Event) -> None:
        nonlocal transcript, wording_reported
        if started(event):
            command = await agent.add_instructions(
                "Immediately say this disclosure exactly and in full before answering: " + DISCLOSURE
            )
            await agent.live.wait(command)
            logging.getLogger(__name__).info("Instruction accepted. Delivery still needs audio/playback verification.")
        if isinstance(event, AudioTranscriptDeltaEvent):
            transcript += event.delta
            if not wording_reported and DISCLOSURE.lower() in transcript.lower():
                wording_reported = True
                logging.getLogger(__name__).info("Wording observed in transcript; this alone does not prove playback.")

    await drive(voice(), observe, duration=args.duration)


if __name__ == "__main__":
    execute(main)
