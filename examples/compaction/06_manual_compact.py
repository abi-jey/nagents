"""Compact the active session explicitly between runs."""

import asyncio
import os
from pathlib import Path

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager


async def main() -> None:
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, os.environ["OPENAI_API_KEY"], "gpt-4o-mini")
    sessions = SessionManager(Path("sessions/manual_compact.db"))
    agent = Agent(provider, sessions)
    try:
        async for event in agent.run("Help me plan a project.", session_id="project"):
            print(event)
        result = await agent.compact("project")
        print(f"Compacted {result.original_message_count} messages to {result.new_message_count}.")
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
