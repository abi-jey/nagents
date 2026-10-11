"""Configure a message threshold and custom summary prompt."""

import asyncio
import os
from pathlib import Path

from nagents import Agent
from nagents import ModelCompaction
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager


async def main() -> None:
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, os.environ["OPENAI_API_KEY"], "gpt-4o-mini")
    strategy = ModelCompaction(
        message_threshold=20,
        prompt="Summarize decisions, constraints, and open questions for continuity.",
    )
    agent = Agent(provider, SessionManager(Path("sessions/custom_compaction.db")), compaction_strategy=strategy)
    try:
        async for event in agent.run("Help plan a small garden.", session_id="garden"):
            print(event)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
