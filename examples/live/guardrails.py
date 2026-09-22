"""Application state blocks an operation independently of a corrective spoken instruction."""

from _support import drive
from _support import execute
from _support import microphone
from _support import parser
from _support import voice

from nagents import Agent
from nagents import Event
from nagents import InputTranscriptDeltaEvent
from nagents.live import LiveConfig
from nagents.live.audio import PlaybackOutput


async def main() -> None:
    args = parser(__doc__ or "Guardrails").parse_args()
    blocked = False
    transcript = ""

    async def protected_lookup(context: str) -> str:
        if blocked:
            return "This request is blocked by application policy. No action was performed."
        return "The demo service contains no private customer records."

    audio = microphone(controlled=True)
    assert isinstance(audio.output, PlaybackOutput)
    player = audio.output

    async def check(agent: Agent, event: Event) -> None:
        nonlocal blocked, transcript
        if isinstance(event, InputTranscriptDeltaEvent):
            transcript += event.delta
            if "another customer's" in transcript.lower() and not blocked:
                blocked = True  # Demo rule; production authorization belongs in the service.
                agent.live.invalidate_tasks()
                await player.pause()
                await agent.add_instructions(
                    "Stop discussing that request. Explain briefly that access to another customer's records is not permitted."
                )
                player.resume()  # Explicit demo recovery policy, not an acknowledgment side effect.

    await drive(
        voice(LiveConfig(delegation="client", client_handler=protected_lookup), audio=audio),
        check,
        duration=args.duration,
    )


if __name__ == "__main__":
    execute(main)
