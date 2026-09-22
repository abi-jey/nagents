"""Drop model playback briefly without pausing microphone input or backend work."""

import asyncio

from _support import drive
from _support import execute
from _support import microphone
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.live.audio import PlaybackOutput


async def main() -> None:
    args = parser(__doc__ or "Playback").parse_args()
    audio = microphone(controlled=True)
    assert isinstance(audio.output, PlaybackOutput)
    player = audio.output

    async def control(agent: Agent, event: Event) -> None:
        if started(event):
            await player.pause()
            await agent.add_instructions("Wait briefly, then greet the caller.")
            await asyncio.sleep(2)
            player.resume()
            print("Playback resumed; dropped bytes:", player.dropped_bytes)

    await drive(voice(audio=audio), control, duration=args.duration)


if __name__ == "__main__":
    execute(main)
