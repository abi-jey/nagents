"""
Example demonstrating non-streaming usage of nagents.

This example shows how to use the Agent with streaming=False (default),
which still yields events but emits TextDoneEvent instead of TextChunkEvents.
HTTP traffic logging is also enabled for debugging.
"""

import asyncio
import os
from datetime import UTC
from datetime import datetime
from logging import basicConfig
from logging import getLogger
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.text import Text

from nagents import Agent
from nagents import DoneEvent
from nagents import ErrorEvent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents import TextDoneEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent

load_dotenv()

logger = getLogger(__name__)
console = Console()


def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: The name of the city to get weather for.
    """
    # Simulated weather data
    weather_data = {
        "london": "Cloudy, 15°C",
        "paris": "Sunny, 22°C",
        "tokyo": "Rainy, 18°C",
        "new york": "Partly cloudy, 20°C",
    }
    return weather_data.get(city.lower(), f"Weather data not available for {city}")


def get_time(tz: str = "UTC") -> str:
    """Get the current time.

    Args:
        tz: Timezone label (for display only, returns UTC time).
    """
    now = datetime.now(UTC)
    return f"Current time ({tz}): {now.strftime('%Y-%m-%d %H:%M:%S')}"


async def main() -> None:
    """Main example demonstrating non-streaming mode."""
    console.print(Panel.fit("[bold blue]nagents Non-Streaming Example[/bold blue]"))

    api_key = os.getenv("OPENAI_API_KEY", "")
    console.print("[dim]Using OpenAI provider (non-streaming)[/dim]")

    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-4o-mini",
    )

    session_manager = SessionManager(Path("sessions.db"))

    # Use a unique session ID for this run
    session_id = f"session-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

    # Log file path: logs/<session_id>.txt
    log_file = Path("logs") / f"{session_id}.txt"

    # Create agent with streaming=False (this is the default)
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_weather, get_time],
        system_prompt="You are a helpful assistant with access to weather and time tools.",
        streaming=False,  # Non-streaming mode (default)
        log_file=log_file,  # Enable HTTP/SSE logging
    )

    try:
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")

        # Example query
        query = "What's the weather in Paris and what time is it?"

        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, ToolCallEvent):
                tool_text = Text()
                tool_text.append("Tool Call: ", style="bold yellow")
                tool_text.append(event.name, style="cyan")
                tool_text.append(f"({event.arguments})", style="dim")
                console.print(tool_text)

            elif isinstance(event, ToolResultEvent):
                result_text = Text()
                result_text.append("Tool Result: ", style="bold green")
                if event.error:
                    result_text.append(f"ERROR: {event.error}", style="red")
                else:
                    result_text.append(str(event.result), style="white")
                result_text.append(f" ({event.duration_ms:.1f}ms)", style="dim")
                console.print(result_text)

            elif isinstance(event, TextDoneEvent):
                # In non-streaming mode, we get the complete text at once
                console.print()
                console.print(Panel(event.text, title="[bold]Assistant[/bold]", border_style="blue"))

            elif isinstance(event, ErrorEvent):
                error_text = Text()
                error_text.append("ERROR: ", style="bold red")
                error_text.append(event.message, style="red")
                console.print(error_text)

            elif isinstance(event, DoneEvent):
                console.print()
                usage_text = Text()
                usage_text.append("Usage: ", style="bold blue")
                usage_text.append(
                    f"prompt={event.usage.prompt_tokens}, "
                    f"completion={event.usage.completion_tokens}, "
                    f"total={event.usage.total_tokens}",
                    style="dim",
                )
                console.print(usage_text)

        # Show where the log file is
        console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")

    finally:
        await agent.close()

    console.print("\n[dim]Example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
