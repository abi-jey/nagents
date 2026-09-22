"""An application-owned backend task outlives voice; resume with its verified result."""

import asyncio
from pathlib import Path

from _support import drive
from _support import execute
from _support import parser
from _support import voice
from dotenv import load_dotenv

from nagents import Agent
from nagents import CodexProvider
from nagents import DoneEvent
from nagents import SessionManager
from nagents.live import LiveConfig


async def main() -> None:
    args = parser(__doc__ or "Idle resume").parse_args()
    load_dotenv()
    backend = Agent(provider=CodexProvider(), session_manager=SessionManager(Path("background-work.db")))

    async def work() -> str:
        answer = ""
        async for event in backend.run(
            "List three concise ways to test a CSV export. This is planning only; no files are modified."
        ):
            if isinstance(event, DoneEvent):
                answer = event.final_text
        if not answer.strip():
            raise RuntimeError("The background task did not return a verified result")
        return answer

    task = asyncio.create_task(work())  # Application owns this task, not the voice connection.
    try:
        await drive(
            voice(instructions="A separate planning task is running. Explain that voice will resume with its result."),
            duration=5,
        )
        result = await task
        history: tuple[dict[str, object], ...] = (
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Verified completed planning result: " + result}],
            },
        )
        await drive(
            voice(LiveConfig(history=history), instructions="Help the caller review the saved planning result."),
            duration=args.duration or 20,
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await backend.close()


if __name__ == "__main__":
    execute(main)
