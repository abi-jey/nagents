"""Paced raw G.711 input/output bridge; carrier envelopes stay outside the Live protocol."""

from pathlib import Path

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents import AudioDuplex
from nagents.audio import AudioFormat
from nagents.audio import BytesAudioInput
from nagents.live.audio import PacedAudioInput


class RawOutput:
    def __init__(self, path: Path, audio_format: AudioFormat) -> None:
        self.audio_format = audio_format
        self.stream = path.open("wb")

    async def write(self, chunk: bytes) -> None:
        self.stream.write(chunk)

    async def interrupt(self) -> None:
        pass  # This records bytes; a carrier adapter must flush its own playback queue.

    async def close(self) -> None:
        self.stream.close()


async def main() -> None:
    cli = parser(__doc__ or "Media bridge")
    cli.add_argument("input", type=Path, help="Raw mono G.711 audio, not WAV")
    cli.add_argument("--codec", choices=("pcmu", "pcma"), default="pcmu")
    cli.add_argument("--output", type=Path, default=Path("reply.g711"))
    args = cli.parse_args()
    fmt = AudioFormat(encoding="audio/" + args.codec, sample_rate=8000, sample_width=1)
    source = PacedAudioInput(BytesAudioInput(args.input.read_bytes(), chunk_size=160, audio_format=fmt))
    await drive(
        voice(audio=AudioDuplex(input=source, output=RawOutput(args.output, fmt))), duration=args.duration or 30
    )


if __name__ == "__main__":
    execute(main)
