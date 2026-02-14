"""
Example demonstrating batch processing with OpenAI API.

Batch processing offers 50% cost discount compared to real-time API.
The Agent interface remains the same - just set batch=True.

Note: Batch requests are processed asynchronously and may take several minutes
to complete. This example shows how to use batch mode for cost-effective
processing.

IMPORTANT: Batch mode requires a provider that supports batch API (OpenAI or Anthropic).
Azure OpenAI uses a different batch mechanism through Azure OpenAI Studio.
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

load_dotenv()

logger = getLogger(__name__)
console = Console()


def get_time(tz: str = "UTC") -> str:
    """Get the current time in a specific timezone."""
    now = datetime.now(UTC)
    return f"Current time in {tz}: {now.strftime('%Y-%m-%d %H:%M:%S')}"


def get_openai_provider() -> Provider:
    """Create a Provider configured for OpenAI."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set. Please set it in your .env file.")

    console.print("[dim]Using OpenAI provider[/dim]")
    return Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-4o-mini",
    )


async def main() -> None:
    """Main example demonstrating batch processing."""
    console.print(Panel.fit("[bold blue]nagents Batch Processing Example[/bold blue]"))
    console.print("[dim]Batch mode: 50% cost discount![/dim]\n")

    try:
        provider = get_openai_provider()
    except ValueError as e:
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        console.print("\n[yellow]Please ensure your .env file contains:[/yellow]")
        console.print("  OPENAI_API_KEY=your-api-key")
        return

    console.print("[dim]Using OpenAI provider (batch mode)[/dim]")

    session_manager = SessionManager(Path("sessions_batch.db"))
    session_id = f"batch-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    log_file = Path("logs") / f"{session_id}.txt"

    # Create agent with batch=True
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_time],
        system_prompt="You are a helpful assistant.",
        streaming=False,
        batch=True,  # Enable batch mode for 50% discount
        batch_poll_interval=10.0,  # Check status every 10 seconds
        log_file=log_file,
    )

    try:
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")
        console.print(f"[yellow]Batch mode enabled - waiting for processing...[/yellow]")

        query = "What time is it right now?"
        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        response_text = ""

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, TextDoneEvent):
                response_text = event.text
                console.print(
                    Panel(
                        event.text,
                        title="[bold blue]Response[/bold blue]",
                        border_style="blue",
                    )
                )
            elif isinstance(event, ErrorEvent):
                error_text = Text()
                error_text.append("ERROR: ", style="bold red")
                error_text.append(event.message, style="red")
                if event.code:
                    error_text.append(f" (code: {event.code})", style="dim red")
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
                console.print(f"[green]Cost savings: 50% compared to real-time API[/green]")
                console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")

    finally:
        await agent.close()

    console.print("\n[dim]Batch example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
