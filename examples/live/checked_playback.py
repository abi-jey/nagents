"""Buffer candidate speech until application approval; a caption is not an authoritative turn end."""

from _support import drive
from _support import execute
from _support import microphone
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import AudioTranscriptDeltaEvent
from nagents import Event
from nagents.live.audio import PlaybackOutput


async def main() -> None:
    args = parser(__doc__ or "Checked playback").parse_args()
    audio = microphone(controlled=True)
    assert isinstance(audio.output, PlaybackOutput)
    player = audio.output
    revision = await player.hold()
    text = ""

    async def review(agent: Agent, event: Event) -> None:
        nonlocal text
        if started(event):
            await agent.add_instructions("Say hello briefly, then wait.")
        elif isinstance(event, AudioTranscriptDeltaEvent):
            text += event.delta
            # This narrow demo only releases a greeting candidate. It is not a
            # general semantic guardrail or proof that an entire answer finished.
            if "hello" in text.lower():
                print("Greeting candidate approved:", await player.approve(revision))

    await drive(voice(audio=audio), review, duration=args.duration or 8)


if __name__ == "__main__":
    execute(main)
