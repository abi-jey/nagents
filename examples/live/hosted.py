"""GPT-Live voice with OpenAI-hosted Responses inference and web search."""

import argparse
import asyncio
from contextlib import aclosing
from contextlib import suppress
from pathlib import Path

from _support import microphone
from _support import print_event
from _support import start_timer
from dotenv import load_dotenv

from nagents import Agent
from nagents import CodexProvider
from nagents import LiveConfig
from nagents import SessionManager


async def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    load_dotenv()
    agent = Agent(
        provider=CodexProvider(
            model="gpt-live-1",
            live_config=LiveConfig(delegation="responses", backend_model="gpt-5.6-luna", web_search=True),
        ),
        session_manager=SessionManager(Path("live-sessions.db")),
        system_prompt="Be concise. Delegate questions needing reasoning or current information to the backend.",
        audio=microphone(),
    )
    try:
        start = start_timer()
        async with aclosing(agent.run()) as events:
            async for event in events:
                print_event(event, start)
    finally:
        await agent.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
