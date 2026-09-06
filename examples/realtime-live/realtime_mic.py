"""
Realtime (speech-to-speech) example with a live microphone and speakers.

Uses the reference ``sounddevice`` adapters from ``audio_adapters.py`` to run a
continuous voice conversation. The audio duplex (mic + speaker) is configured
on the agent, and the Realtime model + its settings live on the provider, so
voice uses the exact same interface as text: ``agent.run()``.

Requirements:
    pip install -e .[dev]   # installs sounddevice + numpy

Run:
    OPENAI_API_KEY=sk-... python examples/realtime-live/realtime_mic.py

Every event is printed with timing/audio info, and every raw client/server
event is logged to logs/realtime-mic.log (override with --log).

Press Ctrl+C to end the conversation.
"""

import argparse
import asyncio
import os
from pathlib import Path

from audio_adapters import SoundDeviceAudioInput
from audio_adapters import SoundDeviceAudioOutput
from dotenv import load_dotenv
from event_printer import print_event
from event_printer import start_timer

from nagents import Agent
from nagents import AudioDuplex
from nagents import Provider
from nagents import ProviderType
from nagents import RealtimeConfig
from nagents import SessionManager


def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Weather in {city}: sunny, 22C"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Realtime live mic conversation")
    parser.add_argument("--log", default="logs/realtime-mic.log", help="JSON-lines event log file")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY to run this example.")
        return

    # The Realtime model and its settings live in the provider block.
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-realtime-2.1",
        realtime_config=RealtimeConfig(
            turn_detection_type="semantic_vad",
            input_transcription_model="gpt-transcribe",
        ),
    )

    # The mic/speaker duplex lives on the agent (each end is disable-able).
    agent = Agent(
        provider=provider,
        session_manager=SessionManager(Path("sessions.db")),
        tools=[get_weather],
        system_prompt="You are a helpful voice assistant. Be concise.",
        audio=AudioDuplex(
            input=SoundDeviceAudioInput(),
            output=SoundDeviceAudioOutput(),
        ),
    )

    print("[realtime] live conversation started - speak into your microphone (Ctrl+C to stop).")
    print(f"[realtime] logging all events to {args.log}")
    start = start_timer()
    try:
        # Same interface as text: agent.run() with no message runs voice.
        async for event in agent.run(log_file=args.log):
            print_event(event, start)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
