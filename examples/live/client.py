"""GPT-Live delegates to an ordinary Nagents agent with its own provider and tools."""

import argparse
import asyncio
from contextlib import aclosing
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from pathlib import Path

from _support import microphone
from _support import print_event
from _support import start_timer
from dotenv import load_dotenv

from nagents import Agent
from nagents import CodexProvider
from nagents import LiveConfig
from nagents import SessionManager


def get_utc_time() -> str:
    """Read the current UTC date and time."""
    return datetime.now(UTC).isoformat()


async def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    load_dotenv()
    sessions = SessionManager(Path("live-sessions.db"))
    backend = Agent(
        provider=CodexProvider(model="gpt-5.6-terra"),
        session_manager=sessions,
        tools=[get_utc_time],
        streaming=True,
        system_prompt="Answer the latest voice request; respect corrections. Return concise facts in under 60 words.",
    )
    voice = Agent(
        provider=CodexProvider(model="gpt-live-1", live_config=LiveConfig(delegation="client")),
        session_manager=sessions,
        delegation_agent=backend,
        system_prompt="Be concise. Delegate reasoning and tool requests. Keep listening while the backend works.",
        audio=microphone(),
    )
    try:
        start = start_timer()
        async with aclosing(voice.run()) as events:
            async for event in events:
                print_event(event, start)
    finally:
        await voice.close()
        await backend.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
