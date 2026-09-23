"""Reuse the microphone and event-printer support shared with Realtime examples."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import aclosing
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv

from nagents import Agent
from nagents import AudioDuplex
from nagents import Event
from nagents import OpenAIProvider
from nagents import SessionManager
from nagents.live import LiveConfig
from nagents.live import LiveEvent
from nagents.live.audio import PlaybackOutput

_shared = str(Path(__file__).resolve().parents[1] / "_voice")
if _shared not in sys.path:
    sys.path.append(_shared)

from event_printer import print_event as print_event  # noqa: E402
from event_printer import start_timer as start_timer  # noqa: E402


def microphone(rate: int = 24000, *, controlled: bool = False) -> AudioDuplex:
    from audio_adapters import SoundDeviceAudioInput
    from audio_adapters import SoundDeviceAudioOutput

    speaker = SoundDeviceAudioOutput(sample_rate=rate)
    return AudioDuplex(
        input=SoundDeviceAudioInput(sample_rate=rate), output=PlaybackOutput(speaker) if controlled else speaker
    )


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument(
        "--duration", type=float, default=0, help="Close after this many connected seconds; default: Ctrl+C"
    )
    return result


def voice(
    config: LiveConfig | None = None,
    *,
    instructions: str = "Be concise. Delegate requests needing tools or reasoning.",
    tools: Sequence[Callable[..., object]] = (),
    backend: Agent | None = None,
    audio: AudioDuplex | None = None,
) -> Agent:
    load_dotenv()
    config = config or LiveConfig()
    return Agent(
        provider=OpenAIProvider(model="gpt-live-1", live_config=config),
        session_manager=SessionManager(Path("live-sessions.db")),
        system_prompt=instructions,
        tools=list(tools),
        delegation_agent=backend,
        audio=audio if audio is not None else None if config.attach_to else microphone(),
    )


async def ignore(agent: Agent, event: Event) -> None:
    pass


def started(event: Event) -> bool:
    return isinstance(event, LiveEvent) and event.event_type in {"session.started", "connection.attached"}


async def drive(
    agent: Agent, handler: Callable[[Agent, Event], Awaitable[None]] = ignore, *, duration: float = 0
) -> None:
    tasks: list[asyncio.Task[None]] = []

    async def close_later() -> None:
        await asyncio.sleep(duration)
        await agent.live.close()

    try:
        start = start_timer()
        async with aclosing(agent.run()) as events:
            async for event in events:
                if started(event) and duration > 0:
                    tasks.append(asyncio.create_task(close_later()))
                print_event(event, start)
                await handler(agent, event)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await agent.close()
        if agent.delegation_agent:
            await agent.delegation_agent.close()


def execute(main: Callable[[], Awaitable[None]]) -> None:
    async def invoke() -> None:
        await main()

    with suppress(KeyboardInterrupt):
        asyncio.run(invoke())
