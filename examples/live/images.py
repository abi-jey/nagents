"""Send a local image to a vision-capable backend; the Live frontend receives findings as text."""

import base64
import mimetypes
from pathlib import Path

from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import Event
from nagents import ImageContent


async def main() -> None:
    cli = parser(__doc__ or "Backend vision")
    cli.add_argument("image", type=Path)
    args = cli.parse_args()
    image = ImageContent(
        base64_data=base64.b64encode(args.image.read_bytes()).decode(),
        media_type=mimetypes.guess_type(args.image.name)[0] or "image/png",
    )

    async def submit(agent: Agent, event: Event) -> None:
        if started(event):
            await agent.live.submit_image(
                image, "Describe this image briefly for the caller. Ask if important text is unclear."
            )

    await drive(voice(), submit, duration=args.duration)


if __name__ == "__main__":
    execute(main)
