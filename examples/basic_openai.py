"""
Example demonstrating the nagents module with all event types.

This example shows how to use the Agent class with streaming events,
tool execution, HTTP traffic logging, and comprehensive event handling.
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

load_dotenv()

logger = getLogger(__name__)
console = Console()


async def order_coffee(quantity: int, store: str) -> str:
    """Simulate ordering a coffee."""
    await asyncio.sleep(1)  # Simulate some delay
    return f"Ordered {quantity} coffee(s) from {store}."


def get_time(tz: str = "UTC") -> str:
    """Get the current time in a specific timezone."""
    # Simplified - just return UTC time with timezone label
    now = datetime.now(UTC)
    return f"Current time in {tz}: {now.strftime('%Y-%m-%d %H:%M:%S')}"


async def main() -> None:
    """Main example demonstrating all event types."""
    console.print(Panel.fit("[bold blue]nagents Example[/bold blue]"))

    api_key = os.getenv("OPENAI_API_KEY", "")
    console.print("[dim]Using OpenAI provider[/dim]")
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-4o-mini",
    )

    # Create session manager
    session_manager = SessionManager(Path("sessions.db"))

    # Use a unique session ID for this run
    session_id = f"session-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

    # Log file path: logs/<session_id>.txt
    log_file = Path("logs") / f"{session_id}.txt"

    # Create agent with tools and HTTP logging enabled
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[order_coffee, get_time],
        system_prompt="You are a helpful assistant with access to coffee ordering and time tools. "
        "Use them when appropriate to answer user questions.",
        streaming=True,  # Enable streaming to see TextChunkEvents
        log_file=log_file,  # Enable HTTP/SSE logging
    )

    try:
        # You can call agent.initialize() explicitly to catch errors early: but it is also auto ran on first turn.
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")

        # Example prompts that will trigger different behaviors
        query = "Hey, what time it is, can you order 2 coffees for me from local shop? can you also calculate 123*456 for me?"

        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()
        response_text = ""

        async for event in agent.run(
            user_message=query,
            session_id=session_id,
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
                result_text.append("Tool call result: ", style="bold green")
                result_text.append(f"`{event.name}` ", style="cyan")
                if event.error:
                    result_text.append(f"ERROR: {event.error}", style="red")
                else:
                    result_text.append(str(event.result), style="white")
                result_text.append(f" ({event.duration_ms:.1f}ms)", style="dim")
                console.print(result_text)
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
                # Generation complete - includes session_id and usage for reference
                console.print()
                done_text = Text()
                done_text.append("Generation complete", style="bold green")
                done_text.append(" - ", style="dim")
                done_text.append(f"{len(event.final_text)} chars", style="dim")
                if event.session_id:
                    done_text.append(f" (session: {event.session_id})", style="dim blue")
                console.print(done_text)
                # Print usage statistics from the DoneEvent (usage is always present)
                usage_text = Text()
                usage_text.append("Usage: ", style="bold blue")
                usage_text.append(
                    f"prompt={event.usage.prompt_tokens}, "
                    f"completion={event.usage.completion_tokens}, "
                    f"total={event.usage.total_tokens}",
                    style="dim",
                )
                if event.usage.session:
                    usage_text.append(
                        f" | Session total: {event.usage.session.total_tokens}",
                        style="dim cyan",
                    )
                console.print(usage_text)
        console.print()

        # Show where the log file is
        console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")
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
    asyncio.run(main())
