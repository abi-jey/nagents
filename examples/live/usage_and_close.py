"""Keep cumulative voice duration separate from backend token usage and finalize explicitly."""

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents import Agent
from nagents import Event
from nagents.live import LiveEvent


async def main() -> None:
    args = parser(__doc__ or "Usage").parse_args()
    seen: set[str] = set()

    async def usage(agent: Agent, event: Event) -> None:
        if isinstance(event, LiveEvent) and event.event_type == "response.event":
            nested = event.payload.get("event", {})
            if isinstance(nested, dict) and nested.get("type") == "response.completed":
                response = nested.get("response", {})
                if isinstance(response, dict) and str(response.get("id")) not in seen:
                    seen.add(str(response.get("id")))
                    print("Backend usage:", response.get("usage"))

    agent = voice()
    await drive(agent, usage, duration=args.duration or 20)
    print("Latest voice seconds (not summed):", agent.live.status.seconds)
    print("Final usage confirmed:", agent.live.status.finalized, "reason:", agent.live.status.reason)


if __name__ == "__main__":
    execute(main)
