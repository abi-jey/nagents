"""
Example demonstrating tool hallucination handling in nagents.

This example shows how to configure the agent to handle cases where the LLM
attempts to call tools that don't exist ("hallucinated" tools).
HTTP traffic logging is also enabled for debugging.

Two modes are available:
1. Recovery mode (default): The error message (including available tools list)
   is passed back to the LLM so it can correct itself.

2. Fail mode: Set `fail_on_invalid_tool=True` to raise a ToolHallucinationError
   immediately when a hallucinated tool call is detected.
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
from nagents import ToolCallEvent
from nagents import ToolHallucinationError
from nagents import ToolResultEvent

load_dotenv()

logger = getLogger(__name__)
console = Console()


def add_numbers(a: int, b: int) -> int:
    """Add two numbers together.

    Args:
        a: First number
        b: Second number
    """
    return a + b


def multiply_numbers(a: int, b: int) -> int:
    """Multiply two numbers together.

    Args:
        a: First number
        b: Second number
    """
    return a * b


async def demo_recovery_mode() -> None:
    """Demonstrate recovery mode - LLM can recover from hallucination."""
    console.print(Panel.fit("[bold blue]Recovery Mode (default)[/bold blue]"))
    console.print("[dim]Error is passed back to LLM so it can correct itself.[/dim]\n")

    api_key = os.getenv("OPENAI_API_KEY", "")
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-4o-mini",
    )
    session_manager = SessionManager(Path("sessions.db"))

    # Use a unique session ID for this run
    session_id = f"hallucination-recovery-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    log_file = Path("logs") / f"{session_id}.txt"

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[add_numbers, multiply_numbers],
        # Lie about available tools in the system prompt to trick the model
        system_prompt="""You are a calculator with the following tools:
- add_numbers(a, b): adds two numbers
- multiply_numbers(a, b): multiplies two numbers
- divide_numbers(a, b): divides a by b
- subtract_numbers(a, b): subtracts b from a

When the user asks for a calculation, IMMEDIATELY call the appropriate tool. Do not explain, just call the tool.""",
        streaming=True,
        fail_on_invalid_tool=False,  # Default: errors passed back to LLM
        log_file=log_file,  # Enable HTTP/SSE logging
    )

    try:
        await agent.initialize()
        console.print("[green]Agent initialized (fail_on_invalid_tool=False)[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")
        console.print(f"[dim]Available tools: {agent.tool_registry.names()}[/dim]")

        # Explicitly ask to use a tool that doesn't exist
        query = "Use the divide_numbers tool to calculate 100 / 4"
        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, TextChunkEvent):
                console.print(event.chunk, end="")
            elif isinstance(event, ToolCallEvent):
                console.print()
                tool_text = Text()
                tool_text.append("Tool Call: ", style="bold yellow")
                tool_text.append(f"{event.name}({event.arguments})", style="cyan")
                console.print(tool_text)
            elif isinstance(event, ToolResultEvent):
                result_text = Text()
                if event.error:
                    result_text.append("Tool ERROR: ", style="bold red")
                    result_text.append(event.error, style="red")
                else:
                    result_text.append("Tool Result: ", style="bold green")
                    result_text.append(str(event.result), style="white")
                console.print(result_text)
            elif isinstance(event, ErrorEvent):
                error_text = Text()
                error_text.append("Error: ", style="bold red")
                error_text.append(event.message, style="red")
                console.print(error_text)
            elif isinstance(event, DoneEvent):
                console.print()
                console.print("[bold green]Done[/bold green]\n")
                console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")
    finally:
        await agent.close()


async def demo_fail_mode() -> None:
    """Demonstrate fail mode - raises ToolHallucinationError."""
    console.print(Panel.fit("[bold blue]Fail Mode (fail_on_invalid_tool=True)[/bold blue]"))
    console.print("[dim]ToolHallucinationError is raised immediately on hallucination.[/dim]\n")

    api_key = os.getenv("OPENAI_API_KEY", "")
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=api_key,
        model="gpt-4o-mini",
    )
    session_manager = SessionManager(Path("sessions.db"))

    # Use a unique session ID for this run
    session_id = f"hallucination-fail-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    log_file = Path("logs") / f"{session_id}.txt"

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[add_numbers, multiply_numbers],
        # Lie about available tools in the system prompt to trick the model
        system_prompt="""You are a calculator with the following tools:
- add_numbers(a, b): adds two numbers
- multiply_numbers(a, b): multiplies two numbers
- divide_numbers(a, b): divides a by b
- subtract_numbers(a, b): subtracts b from a

When the user asks for a calculation, IMMEDIATELY call the appropriate tool. Do not explain, just call the tool.""",
        streaming=True,
        fail_on_invalid_tool=True,  # Raise exception on hallucination
        log_file=log_file,  # Enable HTTP/SSE logging
    )

    try:
        await agent.initialize()
        console.print("[green]Agent initialized (fail_on_invalid_tool=True)[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")
        console.print(f"[dim]Available tools: {agent.tool_registry.names()}[/dim]")

        # Explicitly ask to use a tool that doesn't exist
        query = "Use the divide_numbers tool to calculate 100 / 4"
        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, TextChunkEvent):
                console.print(event.chunk, end="")
            elif isinstance(event, ToolCallEvent):
                console.print()
                tool_text = Text()
                tool_text.append("Tool Call: ", style="bold yellow")
                tool_text.append(f"{event.name}({event.arguments})", style="cyan")
                console.print(tool_text)
            elif isinstance(event, ToolResultEvent):
                result_text = Text()
                if event.error:
                    result_text.append("Tool ERROR: ", style="bold red")
                    result_text.append(event.error, style="red")
                else:
                    result_text.append("Tool Result: ", style="bold green")
                    result_text.append(str(event.result), style="white")
                console.print(result_text)
            elif isinstance(event, DoneEvent):
                console.print()
                console.print("[bold green]Done[/bold green]\n")
                console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]")

    except ToolHallucinationError as e:
        console.print()
        console.print("[bold red]ToolHallucinationError caught![/bold red]")
        console.print(f"[red]Message: {e.message}[/red]")
        console.print(f"[yellow]Tool attempted: {e.tool_name}[/yellow]")
        console.print(f"[green]Available tools: {e.available_tools}[/green]")
        console.print(f"[dim]HTTP traffic logged to: {log_file.absolute()}[/dim]\n")
    finally:
        await agent.close()


async def main() -> None:
    """Run both examples to demonstrate the difference."""
    console.print("\n" + "=" * 60)
    console.print("[bold]Tool Hallucination Handling in nagents[/bold]")
    console.print("=" * 60 + "\n")

    # First, show recovery mode
    await demo_recovery_mode()

    console.print("\n" + "-" * 60 + "\n")

    # Then, show fail mode
    await demo_fail_mode()

    console.print("[dim]Example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
