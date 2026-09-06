"""
Realtime (speech-to-speech) example in text mode.

The simplest way to verify the Realtime integration end-to-end without any
microphone or audio files: connect to a realtime model, send a text message,
and stream the response plus any tool calls.

Run:
    OPENAI_API_KEY=sk-... python examples/realtime-live/realtime_text.py
"""

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from event_printer import print_event
from event_printer import start_timer

from nagents import Agent
from nagents import DoneEvent
from nagents import Provider
from nagents import ProviderType
from nagents import RealtimeConfig
from nagents import SessionManager


def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Weather in {city}: sunny, 22C"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Realtime text-mode example")
    parser.add_argument("--log", default="logs/realtime-text.log", help="JSON-lines event log file")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY to run this example.")
        return

    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-realtime-2.1",
        realtime_config=RealtimeConfig(output_modalities=["text"]),
    )
    agent = Agent(
        provider=provider,
        session_manager=SessionManager(Path("sessions.db")),
        tools=[get_weather],
        system_prompt="You are a helpful voice assistant. Be concise.",
    )

    async with agent.realtime_session(log_file=args.log) as session:
        print(f"[realtime] connected: {session.session_id} (model={provider.model})")
        print(f"[realtime] logging all events to {args.log}")
        await session.send_text("What is the weather in Paris?")

        start = start_timer()
        async for event in session:
            if isinstance(event, DoneEvent):
                print_event(event, start)
                break
            print_event(event, start)

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
