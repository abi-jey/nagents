"""
Example demonstrating the nagents module with Azure AI Services.

This example shows how to use the Agent class with Azure-deployed models
via the OpenAI-compatible API endpoint.

Azure AI Services Setup:
------------------------
1. Create an Azure AI Services resource in Azure Portal
2. Deploy a model (e.g., GPT-4o, Kimi-K2.5, etc.)
3. Get your endpoint URL and API key from the Azure Portal
4. Set the following environment variables in your .env file:

   AZURE_DEPLOYED_MODEL_ENDPOINT=https://your-resource.services.ai.azure.com/api/projects/yourProject
   AZURE_DEPLOYED_MODEL_KEY=your-api-key
   AZURE_DEPLOYED_MODEL_NAME=your-deployed-model-name

Note: Azure AI Services uses OpenAI-compatible API, so we use ProviderType.OPENAI_COMPATIBLE
with a custom base_url pointing to your Azure endpoint.
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


def get_azure_provider() -> Provider:
    """
    Create a Provider configured for Azure AI Services.

    Azure AI Services endpoints are OpenAI-compatible, so we use
    ProviderType.OPENAI_COMPATIBLE with the Azure endpoint as base_url.

    The endpoint URL format for Azure AI Services is typically:
    https://<resource>.services.ai.azure.com/api/projects/<project>/openai

    Returns:
        Provider configured for Azure AI Services

    Raises:
        ValueError: If required environment variables are not set
    """
    # Get Azure configuration from environment
    # Note: Variable names may seem counterintuitive based on your Azure setup
    # Adjust as needed for your specific configuration
    endpoint = os.getenv("AZURE_DEPLOYED_MODEL_ENDPOINT")
    api_key = os.getenv("AZURE_DEPLOYED_MODEL_NAME")  # This contains the API key
    model_name = os.getenv("AZURE_DEPLOYED_MODEL_KEY")  # This contains the model name

    if not endpoint:
        raise ValueError("AZURE_DEPLOYED_MODEL_ENDPOINT not set. Please set it in your .env file.")
    if not api_key:
        raise ValueError("AZURE_DEPLOYED_MODEL_NAME (API key) not set. Please set it in your .env file.")
    if not model_name:
        raise ValueError("AZURE_DEPLOYED_MODEL_KEY (model name) not set. Please set it in your .env file.")

    # Construct the OpenAI-compatible base URL for Azure AI Services
    # Azure AI Services uses: {endpoint}/openai for the OpenAI-compatible API
    base_url = f"{endpoint.rstrip('/')}/openai"

    console.print(f"[dim]Azure endpoint: {endpoint}[/dim]")
    console.print(f"[dim]Model: {model_name}[/dim]")

    return Provider(
        provider_type=ProviderType.AZURE_OPENAI_COMPATIBLE,
        api_key=api_key,
        model=model_name,
        base_url=base_url,
        api_version="2024-05-01-preview",  # Specify the API version if required by your Azure setup
    )


async def main() -> None:
    """Main example demonstrating Azure AI Services integration."""
    console.print(Panel.fit("[bold blue]nagents Azure Example[/bold blue]"))

    try:
        provider = get_azure_provider()
    except ValueError as e:
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        console.print("\n[yellow]Please ensure your .env file contains:[/yellow]")
        console.print(
            "  AZURE_DEPLOYED_MODEL_ENDPOINT=https://your-resource.services.ai.azure.com/api/projects/yourProject"
        )
        console.print("  AZURE_DEPLOYED_MODEL_NAME=your-api-key")
        console.print("  AZURE_DEPLOYED_MODEL_KEY=your-model-name")
        return

    console.print("[dim]Using Azure AI Services provider[/dim]")

    # Create session manager
    session_manager = SessionManager(Path("sessions_azure.db"))

    # Create agent with tools
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[order_coffee, get_time],
        system_prompt="You are a helpful assistant with access to coffee ordering and time tools. "
        "Use them when appropriate to answer user questions.",
        streaming=True,  # Enable streaming to see TextChunkEvents
    )

    try:
        # Initialize the agent
        await agent.initialize()
        console.print(f"[green]Agent initialized, model: {provider.model}[/green]")

        # Example prompts that will trigger different behaviors
        query = "Hey, what time is it? Can you order 2 coffees for me from the local shop?"

        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()
        response_text = ""

        async for event in agent.run(
            user_message=query,
            session_id="azure-example-session",
            user_id="azure-example-user",
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
    finally:
        # Always close to release resources
        await agent.close()
    console.print("\n[dim]Azure example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    asyncio.run(main())
