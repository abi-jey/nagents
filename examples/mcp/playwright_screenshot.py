"""
Example: nagents Agent using Playwright MCP server via OpenRouter.

This example demonstrates the MCP integration by:
1. Connecting to @playwright/mcp@latest via npx
2. Using OpenRouter with a Kimi model to drive a real browser
3. Having the LLM navigate to google.com and take a screenshot

Requirements:
    - Node.js + npx installed
    - OPENROUTER_API_KEY set in .env or environment
    - chrome/chromium installed (Playwright MCP bundles its own browser)
"""

import asyncio
import logging
import os
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
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig

load_dotenv()

logger = getLogger(__name__)
console = Console()

MCP_SERVER_CONFIGS = [
    MCPServerConfig(
        name="playwright",
        command="npx",
        args=["@playwright/mcp@latest"],
    ),
]


async def main() -> None:
    console.print(
        Panel.fit("[bold blue]Playwright MCP — Screenshot google.com[/bold blue]")
    )

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        console.print(
            "[red]OPENROUTER_API_KEY not set. Set it in .env or environment.[/red]"
        )
        return

    # ── Connect to MCP server ──────────────────────────────────────────
    console.print("[dim]Starting Playwright MCP server...[/dim]")
    manager = MCPManager(MCP_SERVER_CONFIGS, request_timeout=120.0)

    try:
        await manager.connect_all()
        mcp_tools = await manager.get_tools()
        console.print(
            f"[green]Connected to Playwright MCP server: {len(mcp_tools)} tools[/green]"
        )
        for tool in mcp_tools:
            console.print(f"  [dim]• {tool.__name__}[/dim]")
    except Exception as e:
        console.print(f"[red]Failed to connect to MCP server: {e}[/red]")
        return

    # ── Set up the provider (OpenRouter → kimi-k2.6) ────────────────────
    provider = Provider(
        provider_type=ProviderType.OPENROUTER,
        api_key=api_key,
        model="moonshotai/kimi-k2.6",
    )

    session_manager = SessionManager(Path("sessions.db"))

    screenshot_path = Path(".playwright-mcp") / "google_screenshot.png"

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=mcp_tools,
        system_prompt=(
            "You are a browser automation agent. You have access to Playwright tools "
            "for controlling a headless browser. "
            "Follow the user's instructions precisely. "
            "When taking screenshots, save them as the full page."
        ),
        streaming=True,
        max_tool_rounds=10,
    )

    try:
        await agent.initialize()
        console.print(
            f"[green]Agent initialized with model: {provider.model}[/green]"
        )

        query = (
            "Navigate to https://google.com, wait for the page to load, "
            f"then take a screenshot and save it to: {screenshot_path}"
        )

        console.print(Panel(f"[bold]Task:[/bold] {query}", border_style="green"))
        console.print()

        async for event in agent.run(
            user_message=query,
            session_id="mcp-playwright-demo",
            user_id="example-user",
        ):
            if isinstance(event, TextChunkEvent):
                console.print(event.chunk, end="")
            elif isinstance(event, TextDoneEvent):
                console.print(event.text)
            elif isinstance(event, ToolCallEvent):
                console.print()
                tool_text = Text()
                tool_text.append("🔧 ", style="bold yellow")
                tool_text.append(event.name, style="cyan")
                if event.arguments:
                    args_str = ", ".join(
                        f"{k}={str(v)[:60]}"
                        for k, v in event.arguments.items()
                    )
                    tool_text.append(f"({args_str})", style="dim")
                console.print(tool_text)
            elif isinstance(event, ToolResultEvent):
                result_text = Text()
                result_text.append("   → ", style="dim")
                if event.error:
                    result_text.append(f"ERROR: {event.error}", style="red")
                else:
                    preview = str(event.result)[:120]
                    result_text.append(preview, style="dim")
                    result_text.append(f" ({event.duration_ms:.0f}ms)", style="dim")
                console.print(result_text)
            elif isinstance(event, ErrorEvent):
                console.print()
                console.print(f"[red]ERROR: {event.message}[/red]")
            elif isinstance(event, DoneEvent):
                console.print()
                done_text = Text()
                done_text.append("Done", style="bold green")
                done_text.append(
                    f" — {event.usage.prompt_tokens}P/{event.usage.completion_tokens}C tokens",
                    style="dim",
                )
                console.print(done_text)

        console.print()

        # Check if screenshot was saved
        if screenshot_path.exists():
            size_kb = screenshot_path.stat().st_size / 1024
            console.print(
                f"[green] Screenshot saved: {screenshot_path} ({size_kb:.1f} KB)[/green]"
            )
        else:
            console.print("[yellow] Screenshot was not saved to disk[/yellow]")

    finally:
        await agent.close()
        await manager.disconnect_all()

    console.print("\n[dim]Example complete.[/dim]")


if __name__ == "__main__":
    basicConfig(
        level="INFO",
        format="[%(name)s] %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    # Reduce verbosity from MCP internals and aiohttp
    logging.getLogger("nagents.mcp").setLevel(logging.WARNING)
    logging.getLogger("nagents.http").setLevel(logging.WARNING)
    asyncio.run(main())
