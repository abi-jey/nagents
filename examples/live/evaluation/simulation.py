"""Run evaluation: independent caller and assistant connected by continuously paced audio."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _support import drive
from _support import execute
from _support import parser
from _support import started
from _support import voice

from nagents import Agent
from nagents import AudioDuplex
from nagents import Event
from nagents.audio import AudioFormat
from nagents.live import LiveConfig


class AudioWire:
    """One direction of a duplex relay, preserving pauses with 20ms PCM frames."""

    audio_format = AudioFormat()

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.frames = bytearray()

    async def write(self, chunk: bytes) -> None:
        if len(self.buffer) + len(chunk) > 24000 * 2 * 5:
            raise BufferError("Simulation relay is more than five seconds behind")
        self.buffer.extend(chunk)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        clock = asyncio.get_running_loop().time()
        while True:
            frame = bytes(self.buffer[:960])
            del self.buffer[:960]
            frame = frame.ljust(960, b"\0")
            self.frames.extend(frame)
            yield frame
            clock += 0.02
            await asyncio.sleep(max(0, clock - asyncio.get_running_loop().time()))

    async def interrupt(self) -> None:
        self.buffer.clear()

    async def close(self) -> None:
        pass  # Both agents share this relay; the application owns its lifetime.


async def main() -> None:
    cli = parser(__doc__ or "Simulated caller")
    cli.add_argument("--output", type=Path, default=Path("simulation.json"))
    args = cli.parse_args()
    bookings: list[dict[str, object]] = []

    def availability() -> str:
        """List offered demo dates at 19:00."""
        return "2030-08-06 and 2030-08-07 at 19:00; two guests are available."

    def reserve(name: str, date: str, guests: int) -> str:
        """Save one local simulated booking after the caller requests it."""
        if date not in {"2030-08-06", "2030-08-07"} or guests != 2:
            return "Unavailable; no booking was saved."
        record: dict[str, object] = {"name": name, "date": date, "guests": guests}
        if record not in bookings:
            bookings.append(record)
        return json.dumps({"saved": record})

    to_assistant, to_caller = AudioWire(), AudioWire()
    assistant = voice(
        tools=[availability, reserve],
        audio=AudioDuplex(input=to_assistant, output=to_caller),
        instructions="You are a reservation assistant. Clarify missing information. Delegate tools; confirm only saved bookings.",
    )
    goal = "You are Maya, calling to book for two at 19:00 on August 6, 2030. Initially say August 7, then correct yourself to August 6. Confirm when asked. End politely once the correct booking is confirmed."
    caller = voice(
        LiveConfig(backend_instructions=goal),
        instructions=goal,
        audio=AudioDuplex(input=to_caller, output=to_assistant),
    )

    async def begin(agent: Agent, event: Event) -> None:
        if started(event):
            await agent.add_instructions("Start the call now with your reservation request.")

    async with asyncio.TaskGroup() as group:
        group.create_task(drive(assistant, duration=args.duration or 60))
        group.create_task(drive(caller, begin, duration=args.duration or 60))
    passed = len(bookings) == 1 and bookings[0] == {"name": "Maya", "date": "2030-08-06", "guests": 2}
    args.output.write_text(
        json.dumps(
            {
                "task_passed": passed,
                "bookings": bookings,
                "assistant_seconds": assistant.live.status.seconds,
                "caller_seconds": caller.live.status.seconds,
                "finalized": assistant.live.status.finalized and caller.live.status.finalized,
            },
            indent=2,
        )
    )
    args.output.with_suffix(".caller.pcm").write_bytes(to_assistant.frames)
    args.output.with_suffix(".assistant.pcm").write_bytes(to_caller.frames)
    print("Task result:", passed, "Review audio separately; one run is not a benchmark.")


if __name__ == "__main__":
    execute(main)
