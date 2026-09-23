"""Play an application-verified WAV while gating model output; require actual device drain."""

from pathlib import Path

from _support import drive
from _support import execute
from _support import microphone
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.audio import WAVFileAudioInput
from nagents.live.audio import PlaybackOutput


async def main() -> None:
    cli = parser(__doc__ or "Verified disclosure")
    cli.add_argument("clip", type=Path, help="Previously verified mono PCM16/24kHz disclosure WAV")
    args = cli.parse_args()
    audio = microphone(controlled=True)
    assert isinstance(audio.output, PlaybackOutput)
    player = audio.output
    await player.pause()

    async def disclose(agent: Agent, event: Event) -> None:
        if started(event):
            await player.play_clip(WAVFileAudioInput(args.clip))
            print("Verified clip playback completed:", player.clip_completed)
            await agent.add_thinking("The application has finished playing its opening disclosure.")
            player.resume()

    await drive(voice(audio=audio), disclose, duration=args.duration)


if __name__ == "__main__":
    execute(main)
