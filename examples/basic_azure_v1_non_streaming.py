"""
Example demonstrating non-streaming usage with Azure OpenAI V1 API.

This example shows chain-of-thought reasoning content from models like Kimi-K2.5.
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
from nagents import ReasoningChunkEvent
from nagents import SessionManager
from nagents import TextDoneEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent

load_dotenv()

logger = getLogger(__name__)
console = Console()


async def order_coffee(quantity: int, store: str) -> str:
    """Simulate ordering a coffee."""
    await asyncio.sleep(1)
    return f"Ordered {quantity} coffee(s) from {store}."


def get_time(tz: str = "UTC") -> str:
    """Get the current time in a specific timezone."""
    now = datetime.now(UTC)
    return f"Current time in {tz}: {now.strftime('%Y-%m-%d %H:%M:%S')}"


def get_azure_provider() -> Provider:
    """Create a Provider configured for Azure OpenAI V1 API."""
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
    """Main example demonstrating non-streaming Azure OpenAI V1."""
    console.print(Panel.fit("[bold blue]nagents Azure V1 Non-Streaming Example[/bold blue]"))

    try:
        provider = get_azure_provider()
    except ValueError as e:
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        console.print("\n[yellow]Please ensure your .env file contains:[/yellow]")
        console.print("  AZURE_DEPLOYED_MODEL_ENDPOINT=https://your-resource.openai.azure.com")
        console.print("  AZURE_DEPLOYED_MODEL_NAME=your-api-key")
        console.print("  AZURE_DEPLOYED_MODEL_KEY=your-model-name")
        return

    console.print("[dim]Using Azure OpenAI V1 provider (non-streaming)[/dim]")

    session_manager = SessionManager(Path("sessions_azure.db"))
    session_id = f"azure-v1-nonstream-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    log_file = Path("logs") / f"{session_id}.txt"

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[order_coffee, get_time],
        system_prompt="You are a helpful assistant with access to coffee ordering and time tools. "
        "Use them when appropriate to answer user questions.",
        streaming=False,
        log_file=log_file,
    )

    try:
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")

        query = "Hey, what time is it? Can you order 2 coffees for me from the local shop?"
        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        reasoning_text = ""
        response_text = ""

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, ReasoningChunkEvent):
                reasoning_text += event.chunk
                console.print(
                    Panel(
                        event.chunk,
                        title="[dim italic]Reasoning[/dim italic]",
                        border_style="dim yellow",
                    )
                )
            elif isinstance(event, TextDoneEvent):
                response_text = event.text
                console.print(
                    Panel(
                        event.text,
                        title="[bold blue]Response[/bold blue]",
                        border_style="blue",
                    )
                )
            elif isinstance(event, ToolCallEvent):
                tool_text = Text()
                tool_text.append("Tool Call: ", style="bold yellow")
                tool_text.append(event.name, style="cyan")
                tool_text.append("(", style="dim")
                args_str = ", ".join(f"{k}={v!r}" for k, v in event.arguments.items())
                tool_text.append(args_str, style="white")
                tool_text.append(")", style="dim")
                console.print(tool_text)
            elif isinstance(event, ToolResultEvent):
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
                console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")

        console.print()
        console.print(f"[dim]Reasoning length: {len(reasoning_text)} chars[/dim]")
        console.print(f"[dim]Response length: {len(response_text)} chars[/dim]")

    finally:
        await agent.close()

    console.print("\n[dim]Azure V1 non-streaming example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
