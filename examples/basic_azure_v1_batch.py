"""
Example demonstrating batch processing with Azure OpenAI V1 API.

Azure OpenAI Batch API offers 50% cost discount compared to real-time API.
Batch requests are processed asynchronously with 24-hour target turnaround.

REQUIREMENTS:
1. You must create a Global-Batch deployment type in Azure OpenAI Studio
   - Standard deployments (GlobalStandard) do NOT support batch processing
   - Go to Azure OpenAI Studio > Deployments > Create new > Select "Global-Batch" type
2. The deployment name must be used as the model name
3. Check Azure's current Global-Batch model availability for your region before deploying.

Error "invalid_deployment_type" means you need to create a Global-Batch deployment.
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


def get_azure_batch_provider() -> Provider:
    """Create a Provider configured for Azure OpenAI V1 API with batch support."""
    endpoint = os.getenv("AZURE_DEPLOYED_MODEL_ENDPOINT")
    api_key = os.getenv("AZURE_DEPLOYED_MODEL_KEY")  # API key
    model_name = os.getenv("AZURE_NEW_FOUNDRY_PROJECT_MODEL_NAME")  # Model/deployment name

    if not endpoint:
        raise ValueError("AZURE_DEPLOYED_MODEL_ENDPOINT not set. Please set it in your .env file.")
    if not api_key:
        raise ValueError("AZURE_DEPLOYED_MODEL_KEY (API key) not set. Please set it in your .env file.")
    if not model_name:
        raise ValueError("AZURE_DEPLOYED_MODEL_NAME (model name) not set. Please set it in your .env file.")

    # For V1 API, base_url should include /openai suffix
    # URL format: https://{resource}.openai.azure.com/openai/v1/chat/completions
    base_url = f"{endpoint.rstrip('/')}/openai"

    console.print(f"[dim]Azure V1 endpoint: {base_url}[/dim]")
    console.print(f"[dim]Model (deployment): {model_name}[/dim]")

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
        provider = get_azure_batch_provider()
    except ValueError as e:
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        return

    session_manager = SessionManager(Path("sessions_azure_v1_batch.db"))
    session_id = f"azure-v1-batch-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    log_file = Path("logs") / f"{session_id}.txt"

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_time],
        system_prompt="You are a helpful assistant.",
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

        query = "What is the time?"
        console.print(Panel(f"[bold]User:[/bold] {query}", border_style="green"))
        console.print()

        async for event in agent.run(user_message=query, session_id=session_id):
            if isinstance(event, TextDoneEvent):
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
