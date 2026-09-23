"""Crawl/Walk replay: paced caller WAV, separate output audio, event evidence and final usage."""

import json
import sys
import wave
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.audio import AudioDuplex
from nagents.audio import WAVFileAudioInput
from nagents.audio import WAVFileAudioOutput
from nagents.live.audio import PacedAudioInput


async def main() -> None:
    cli = parser(__doc__ or "Replay")
    cli.add_argument("input", type=Path, help="Synthetic (Crawl) or approved human (Walk) mono PCM WAV")
    cli.add_argument("--output", type=Path, default=Path("evaluation.wav"))
    args = cli.parse_args()
    source = WAVFileAudioInput(args.input)
    with wave.open(str(args.input), "rb") as recording:
        seconds = recording.getnframes() / recording.getframerate()
    audio = AudioDuplex(input=PacedAudioInput(source), output=WAVFileAudioOutput(args.output, source.audio_format))
    agent = voice(audio=audio)
    with args.output.with_suffix(".jsonl").open("w") as evidence:

        async def save(owner: Agent, event: Event) -> None:
            evidence.write(json.dumps(asdict(event), default=str) + "\n")

        await drive(agent, save, duration=args.duration or seconds + 20)
    print("Finalized:", agent.live.status.finalized, "voice seconds:", agent.live.status.seconds)
    print("Replay measures generated audio; inspect task state and playback separately.")


if __name__ == "__main__":
    execute(main)
