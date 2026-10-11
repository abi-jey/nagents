"""Use a separate, smaller provider for summary generation."""

import asyncio
import os
from pathlib import Path

from nagents import Agent
from nagents import ModelCompaction
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager


async def main() -> None:
    key = os.environ["OPENAI_API_KEY"]
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, key, "gpt-4o")
    summary_provider = Provider(ProviderType.OPENAI_COMPATIBLE, key, "gpt-4o-mini")
    strategy = ModelCompaction(token_threshold=80_000, provider=summary_provider)
    agent = Agent(provider, SessionManager(Path("sessions/separate_compactor.db")), compaction_strategy=strategy)
    try:
        async for event in agent.run("Research the migration plan.", session_id="migration"):
            print(event)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
