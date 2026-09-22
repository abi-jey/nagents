"""Choose an already-authorized custom voice ID at startup, with its intended English accent."""

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "Custom voice")
    cli.add_argument("voice_id", help="An existing custom voice authorized for this project")
    cli.add_argument("--accent", default="British English")
    args = cli.parse_args()
    await drive(
        voice(
            LiveConfig(voice={"id": args.voice_id}),
            instructions=f"Be concise and helpful. Speak {args.accent}. Delegate reasoning tasks.",
        ),
        duration=args.duration,
    )


if __name__ == "__main__":
    execute(main)
