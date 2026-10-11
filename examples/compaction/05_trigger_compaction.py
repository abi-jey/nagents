"""Force the configured strategy to run before the next provider request."""

import asyncio
import os
from pathlib import Path

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager


async def main() -> None:
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, os.environ["OPENAI_API_KEY"], "gpt-4o-mini")
    agent = Agent(provider, SessionManager(Path("sessions/trigger_compaction.db")))
    try:
        agent.trigger_compaction()
        async for event in agent.run("Continue from the saved context.", session_id="demo"):
            print(event)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
