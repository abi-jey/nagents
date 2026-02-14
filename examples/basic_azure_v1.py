"""
Example demonstrating the nagents module with Azure OpenAI V1 API.

This example shows how to use the Agent class with Azure-deployed models
via the OpenAI-compatible V1 API endpoint with HTTP traffic logging.

Azure OpenAI V1 Setup:
---------------------
1. Create an Azure OpenAI resource in Azure Portal
2. Deploy a model (e.g., GPT-4o, Kimi-K2.5, etc.)
3. Get your endpoint URL and API key from the Azure Portal
4. Set the following environment variables in your .env file:

   AZURE_DEPLOYED_MODEL_ENDPOINT=https://your-resource.openai.azure.com
   AZURE_DEPLOYED_MODEL_NAME=your-api-key
   AZURE_DEPLOYED_MODEL_KEY=your-deployed-model-name

The V1 API uses:
- URL: https://{resource}.openai.azure.com/openai/v1/chat/completions
- Auth: Authorization Bearer token
- Model specified in request body (like standard OpenAI)
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


def get_azure_provider() -> Provider:
    """
    Create a Provider configured for Azure OpenAI V1 API.

    The V1 API is OpenAI-compatible and uses:
    - URL: https://{resource}.openai.azure.com/openai/v1/chat/completions
    - Auth: Authorization Bearer token
    - Model in request body

    Returns:
        Provider configured for Azure OpenAI V1 API

    Raises:
        ValueError: If required environment variables are not set
    """
    # Get Azure configuration from environment (same vars as basic_azure.py)
    endpoint = os.getenv("AZURE_DEPLOYED_MODEL_ENDPOINT")
    api_key = os.getenv("AZURE_DEPLOYED_MODEL_NAME")  # This contains the API key
    model_name = os.getenv("AZURE_DEPLOYED_MODEL_KEY")  # This contains the model name

    if not endpoint:
        raise ValueError("AZURE_DEPLOYED_MODEL_ENDPOINT not set. Please set it in your .env file.")
    if not api_key:
        raise ValueError("AZURE_DEPLOYED_MODEL_NAME (API key) not set. Please set it in your .env file.")
    if not model_name:
        raise ValueError("AZURE_DEPLOYED_MODEL_KEY (model name) not set. Please set it in your .env file.")

    # For V1 API, base_url should include /openai suffix
    # URL format: https://{resource}.openai.azure.com/openai/v1/chat/completions
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
    """Main example demonstrating Azure OpenAI V1 integration."""
    console.print(Panel.fit("[bold blue]nagents Azure V1 Example[/bold blue]"))

    try:
        provider = get_azure_provider()
    except ValueError as e:
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        console.print("\n[yellow]Please ensure your .env file contains:[/yellow]")
        console.print("  AZURE_DEPLOYED_MODEL_ENDPOINT=https://your-resource.openai.azure.com")
        console.print("  AZURE_DEPLOYED_MODEL_NAME=your-api-key")
        console.print("  AZURE_DEPLOYED_MODEL_KEY=your-model-name")
        return

    console.print("[dim]Using Azure OpenAI V1 provider[/dim]")

    # Create session manager
    session_manager = SessionManager(Path("sessions_azure.db"))

    # Use a unique session ID for this run
    session_id = f"azure-v1-session-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

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
        # Initialize the agent
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")
        console.print(f"[blue]HTTP logging to: {log_file}[/blue]")

        # Example prompts that will trigger different behaviors
        query = "Hey, what time is it? Can you order 2 coffees for me from the local shop?"

        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()
        response_text = ""

        async for event in agent.run(
            user_message=query,
            session_id=session_id,
            user_id="azure-v1-example-user",
        ):
            # Handle each event type
            if isinstance(event, ReasoningChunkEvent):
                # Streaming reasoning chunk (from chain-of-thought models)
                # Display in a dim style to distinguish from regular content
                console.print(f"[dim italic]{event.chunk}[/dim italic]", end="")
            elif isinstance(event, TextChunkEvent):
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
    console.print("\n[dim]Azure V1 example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
