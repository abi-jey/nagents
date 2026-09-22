"""Use PCM16 at 16 or 24 kHz; capture and playback must actually use the chosen rate."""

from _support import drive
from _support import execute
from _support import microphone
from _support import parser
from _support import voice


async def main() -> None:
    cli = parser(__doc__ or "Audio formats")
    cli.add_argument("--rate", choices=(16000, 24000), type=int, default=24000)
    args = cli.parse_args()
    await drive(voice(audio=microphone(args.rate)), duration=args.duration)


if __name__ == "__main__":
    execute(main)
