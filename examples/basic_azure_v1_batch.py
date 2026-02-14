"""
Example demonstrating batch processing with Azure OpenAI V1 API.

Azure OpenAI Batch API offers 50% cost discount compared to real-time API.
Batch requests are processed asynchronously with 24-hour target turnaround.

IMPORTANT: You need a Global-Batch deployment type for batch processing.
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


def get_time(tz: str = "UTC") -> str:
    """Get the current time in a specific timezone."""
    now = datetime.now(UTC)
    return f"Current time in {tz}: {now.strftime('%Y-%m-%d %H:%M:%S')}"


def order_coffee(quantity: int, store: str) -> str:
    """Simulate ordering coffee."""
    return f"Ordered {quantity} coffee(s) from {store}."


def get_azure_v1_provider() -> Provider:
    """Create a Provider configured for Azure OpenAI V1 API with batch support."""
    endpoint = os.getenv("AZURE_DEPLOYED_MODEL_ENDPOINT")
    api_key = os.getenv("AZURE_DEPLOYED_MODEL_NAME")
    model_name = os.getenv("AZURE_DEPLOYED_MODEL_KEY")

    if not endpoint:
        raise ValueError("AZURE_DEPLOYED_MODEL_ENDPOINT not set. Please set it in your .env file.")
    if not api_key:
        raise ValueError("AZURE_DEPLOYED_MODEL_NAME (API key) not set. Please set it in your .env file.")
    if not model_name:
        raise ValueError("AZURE_DEPLOYED_MODEL_KEY (model name) not set. Please set it in your .env file.")

    base_url = f"{endpoint.rstrip('/')}/openai"

    console.print(f"[dim]Azure V1 endpoint: {base_url}[/dim]")
    console.print(f"[dim]Model: {model_name}[/dim]")

    return Provider(
        provider_type=ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
        api_key=api_key,
        model=model_name,
        base_url=base_url,
    )


async def main() -> None:
    """Main example demonstrating batch processing with Azure V1."""
    console.print(Panel.fit("[bold blue]nagents Azure V1 Batch Processing Example[/bold blue]"))
    console.print("[dim]Using Azure OpenAI V1 provider with batch mode[/dim]\n")

    try:
        provider = get_azure_v1_provider()
    except ValueError as e:
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        return

    session_manager = SessionManager(Path("sessions_azure_v1_batch.db"))
    session_id = f"azure-v1-batch-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    log_file = Path("logs") / f"{session_id}.txt"

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_time, order_coffee],
        system_prompt="You are a helpful assistant with access to coffee ordering and time tools.",
        streaming=False,
        batch=True,
        batch_poll_interval=10.0,
        log_file=log_file,
    )

    try:
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")
        console.print("[yellow]Batch mode enabled - 50% cost discount![/yellow]")

        query = "What time is it? Can you order 2 coffees from the local shop?"
        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, ToolCallEvent):
                console.print(f"[yellow]Tool Call:[/yellow] {event.name}({event.arguments})")
            elif isinstance(event, ToolResultEvent):
                result_str = str(event.result) if event.result else "None"
                error_str = f" [red]Error: {event.error}[/red]" if event.error else ""
                console.print(f"[green]Tool Result:[/green] {result_str}{error_str} ({event.duration_ms:.1f}ms)")
            elif isinstance(event, TextDoneEvent):
                console.print(
                    Panel(
                        event.text,
                        title=f"[bold blue]Response[/bold blue] (finish_reason: {event.finish_reason.value})",
                        border_style="blue",
                    )
                )
                if event.extra:
                    console.print(f"[dim]Response ID: {event.extra.get('id', 'N/A')}[/dim]")
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
                if event.usage.cached_tokens > 0:
                    usage_text.append(f", cached={event.usage.cached_tokens}", style="green")
                if event.usage.reasoning_tokens > 0:
                    usage_text.append(f", reasoning={event.usage.reasoning_tokens}", style="magenta")
                console.print(usage_text)
                console.print(f"[dim]Finish reason: {event.finish_reason.value}[/dim]")
                console.print("[green]Cost savings: 50% compared to real-time API[/green]")
                console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")

    finally:
        await agent.close()

    console.print("\n[dim]Azure V1 batch example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
