"""
Example demonstrating the nagents module with all event types.

This example shows how to use the Agent class with streaming events,
tool execution, and comprehensive event handling.
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
from nagents import TextChunkEvent
from nagents import TextDoneEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent
from nagents import UsageEvent

load_dotenv()

logger = getLogger(__name__)
console = Console()


# Define some example tools
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # Simulated weather data
    weather_data = {
        "paris": "Sunny, 22C",
        "london": "Cloudy, 15C",
        "tokyo": "Rainy, 18C",
        "new york": "Clear, 25C",
    }
    return weather_data.get(city.lower(), f"Weather data not available for {city}")


def calculate(expression: str) -> str:
    """
    Evaluate a mathematical expression.

    Args:
        expression: A mathematical expression like "2 + 2" or "10 * 5"
    """
    try:
        # Only allow safe math operations
        allowed = set("0123456789+-*/(). ")
        if not all(c in allowed for c in expression):
            return "Error: Invalid characters in expression"
        result = eval(expression)
        return f"{expression} = {result}"
    except Exception as e:
        return f"Error evaluating expression: {e}"


def get_time(tz: str = "UTC") -> str:
    """Get the current time in a specific timezone."""
    # Simplified - just return UTC time with timezone label
    now = datetime.now(UTC)
    return f"Current time in {tz}: {now.strftime('%Y-%m-%d %H:%M:%S')} (simulated)"


async def main() -> None:
    """Main example demonstrating all event types."""
    console.print(Panel.fit("[bold blue]nagents Example[/bold blue]"))

    # Choose provider based on available API keys
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if api_key:
        console.print("[dim]Using Gemini Native provider[/dim]")
        provider = Provider(
            provider_type=ProviderType.GEMINI_NATIVE,
            api_key=api_key,
            model="gemini-2.0-flash",
        )
    else:
        api_key = os.getenv("OPENAI_API_KEY", "")
        console.print("[dim]Using OpenAI provider[/dim]")
        provider = Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key=api_key,
            model="gpt-4o-mini",
        )

    # Create session manager
    session_manager = SessionManager(Path("sessions.db"))

    # Create agent with tools
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_weather, calculate, get_time],
        system_prompt="You are a helpful assistant with access to weather, calculator, and time tools. "
        "Use them when appropriate to answer user questions.",
    )

    try:
        # You can call agent.initialize() explicitly to catch errors early: but it is also auto ran on first turn.
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")

        # Example prompts that will trigger different behaviors
        prompts = [
            "What's the weather like in Paris and London? Also, what is 15 * 7?",
        ]

        for prompt in prompts:
            console.print()
            console.print(Panel(f"[bold]User:[/bold] {prompt}", border_style="green"))
            console.print()

            # Collect response text for display
            response_text = ""

            # Process all events
            # Note: session_id is optional - if not provided, a new session is created
            async for event in agent.run(
                user_message=prompt,
                session_id="example-session",  # Optional: use existing or create new
                user_id="example-user",
            ):
                # Handle each event type
                if isinstance(event, TextChunkEvent):
                    # Streaming text chunk - print without newline
                    console.print(event.chunk, end="")
                    response_text += event.chunk

                elif isinstance(event, TextDoneEvent):
                    # Complete text received (may be emitted instead of chunks for non-streaming)
                    if not response_text:  # Only use if we didn't get chunks
                        response_text = event.text
                        console.print(event.text)

                elif isinstance(event, ToolCallEvent):
                    # Model is calling a tool
                    console.print()
                    tool_text = Text()
                    tool_text.append("Tool Call: ", style="bold yellow")
                    tool_text.append(event.name, style="cyan")
                    tool_text.append("(", style="dim")
                    args_str = ", ".join(f"{k}={v!r}" for k, v in event.arguments.items())
                    tool_text.append(args_str, style="white")
                    tool_text.append(")", style="dim")
                    console.print(tool_text)

                elif isinstance(event, ToolResultEvent):
                    # Tool execution completed
                    result_text = Text()
                    result_text.append("  Result: ", style="bold green")
                    if event.error:
                        result_text.append(f"ERROR: {event.error}", style="red")
                    else:
                        result_text.append(str(event.result), style="white")
                    result_text.append(f" ({event.duration_ms:.1f}ms)", style="dim")
                    console.print(result_text)

                elif isinstance(event, UsageEvent):
                    # Token usage statistics
                    console.print()
                    usage_text = Text()
                    usage_text.append("Usage: ", style="bold blue")
                    usage_text.append(
                        f"prompt={event.prompt_tokens}, "
                        f"completion={event.completion_tokens}, "
                        f"total={event.total_tokens}",
                        style="dim",
                    )
                    console.print(usage_text)

                elif isinstance(event, ErrorEvent):
                    # Error occurred
                    console.print()
                    error_text = Text()
                    error_text.append("ERROR: ", style="bold red")
                    error_text.append(event.message, style="red")
                    if event.code:
                        error_text.append(f" (code: {event.code})", style="dim red")
                    if event.recoverable:
                        error_text.append(" [recoverable]", style="yellow")
                    console.print(error_text)

                elif isinstance(event, DoneEvent):
                    # Generation complete - includes session_id for reference
                    console.print()
                    done_text = Text()
                    done_text.append("Generation complete", style="bold green")
                    done_text.append(" - ", style="dim")
                    done_text.append(f"{len(event.final_text)} chars", style="dim")
                    if event.session_id:
                        done_text.append(f" (session: {event.session_id})", style="dim blue")
                    console.print(done_text)

            console.print()
            console.print("-" * 60)

    finally:
        # Always close to release resources
        await agent.close()

    console.print("\n[dim]Example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )

    # Run the full example
    asyncio.run(main())
