"""Download a finalized stored recording (stereo WAV: input left, output right)."""

from pathlib import Path

from _support import execute
from _support import parser
from dotenv import load_dotenv

from nagents import OpenAIProvider
from nagents.live import LiveAPI
from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "Recording")
    cli.add_argument("session_id")
    cli.add_argument("--output", type=Path, default=Path("recording.wav"))
    args = cli.parse_args()
    load_dotenv()
    provider = OpenAIProvider(model="gpt-live-1", live_config=LiveConfig())
    try:
        await LiveAPI(provider).download_recording(args.session_id, args.output)
    finally:
        await provider.close()


if __name__ == "__main__":
    execute(main)
