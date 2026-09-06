"""
Realtime (speech-to-speech) example with full-duplex file audio.

Streams a WAV file into the model and writes the model's spoken reply to an
output WAV file. The audio duplex (file in / file out) is configured on the
agent, and the Realtime model + settings live on the provider — voice uses the
same interface as text: ``agent.run()``.

Run:
    OPENAI_API_KEY=sk-... python examples/realtime-live/realtime_voice.py \
        --input speech.wav --output reply.wav

If no input WAV is provided, a short synthetic tone is generated so the
full-duplex plumbing can be exercised end-to-end without a microphone.
"""

import argparse
import asyncio
import math
import os
import struct
import wave
from pathlib import Path

from dotenv import load_dotenv
from event_printer import print_event
from event_printer import start_timer

from nagents import Agent
from nagents import AudioDuplex
from nagents import Provider
from nagents import ProviderType
from nagents import RealtimeConfig
from nagents import SessionManager
from nagents import WAVFileAudioInput
from nagents import WAVFileAudioOutput


def get_time(tz: str = "UTC") -> str:
    """Get the current time in a timezone."""
    return f"The current time in {tz} is a placeholder."


def write_tone_wav(path: str | Path, seconds: float = 1.0, rate: int = 24000, freq: float = 440.0) -> None:
    """Write a mono 16-bit PCM WAV file containing a sine tone."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            sample = int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", sample)
        wf.writeframes(bytes(frames))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Realtime duplex voice example")
    parser.add_argument("--input", default=None, help="Input WAV file (mono 16-bit PCM)")
    parser.add_argument("--output", default="reply.wav", help="Output WAV file to write the reply to")
    parser.add_argument("--log", default="logs/realtime-voice.log", help="JSON-lines event log file")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY to run this example.")
        return

    input_path = args.input or "input_tone.wav"
    if not Path(input_path).exists():
        print(f"[realtime] no input file; generating a test tone at {input_path}")
        write_tone_wav(input_path)

    # Push-to-talk: turn detection off -> the session auto-commits + responds
    # once the input file is exhausted.
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-realtime-2.1",
        realtime_config=RealtimeConfig(
            turn_detection=False,
            input_transcription_model="gpt-transcribe",
        ),
    )

    agent = Agent(
        provider=provider,
        session_manager=SessionManager(Path("sessions.db")),
        tools=[get_time],
        system_prompt="You are a helpful voice assistant. Be concise.",
        audio=AudioDuplex(
            input=WAVFileAudioInput(input_path),
            output=WAVFileAudioOutput(args.output),
        ),
    )

    print(f"[realtime] streaming {input_path} -> model -> {args.output}")
    print(f"[realtime] logging all events to {args.log}")
    start = start_timer()
    try:
        async for event in agent.run(log_file=args.log):
            print_event(event, start)
    finally:
        await agent.close()

    print(f"[realtime] reply saved to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
