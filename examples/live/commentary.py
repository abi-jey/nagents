"""Optional commentary: a model-callable tool, a nested tool call, and an app call."""

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
from nagents.live import LiveEvent


async def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    load_dotenv()
    sessions = SessionManager(Path("live-sessions.db"))
    voice = Agent(
        provider=CodexProvider(model="gpt-live-1", live_config=LiveConfig(delegation="client")),
        session_manager=sessions,
        system_prompt="Delegate reasoning and tool requests; keep listening while the backend works.",
        audio=microphone(),
    )

    async def get_utc_time() -> str:
        """Read the UTC clock and announce progress."""
        # 1. Direct call inside another tool: automatically uses its delegation ID.
        await voice.add_comment("I'm checking the current UTC time.")
        await asyncio.sleep(10)  # Simulate a slow service while voice continues.
        return datetime.now(UTC).isoformat()

    backend = Agent(
        provider=CodexProvider(),
        session_manager=sessions,
        # 2. Optional model-callable tool. Nothing is injected automatically.
        tools=[voice.add_comment, get_utc_time],
        streaming=True,
        system_prompt=(
            "Respect the latest voice request and corrections. You may use add_comment for useful brief progress. "
            "The clock tool announces its own progress. Return final facts in under 60 words; they are forwarded automatically."
        ),
    )
    voice.delegation_agent = backend
    try:
        start = start_timer()
        async with aclosing(voice.run()) as events:
            async for event in events:
                if isinstance(event, LiveEvent) and event.event_type == "session.started":
                    # 3. Application call: outside delegated work, so the ID is null.
                    await voice.add_comment("I'm ready. Ask me for the current UTC time or a reasoning question.")
                    await voice.add_thinking("The application has a real UTC clock. No actions have been taken.")
                print_event(event, start)
    finally:
        await voice.close()
        await backend.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
