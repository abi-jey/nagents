"""
MCP (Model Context Protocol) integration for nagents.

Provides an MCP client that connects to MCP servers via STDIO transport,
discovers tools, and converts them to nagents ToolDefinition format
for use with nagents Agent.

Example:
    from nagents import Agent, Provider, ProviderType, SessionManager
    from nagents.mcp import MCPServerConfig, MCPManager

    # Configure MCP servers
    mcp_configs = [
        MCPServerConfig(
            name="playwright",
            command="npx",
            args=["@playwright/mcp@latest"],
        ),
        MCPServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed"],
        ),
    ]

    # Create manager and connect to all servers
    manager = MCPManager(mcp_configs)
    await manager.connect_all()

    # Get all MCP tools as nagents ToolDefinitions
    mcp_tools = await manager.get_tools()

    # Use with nagents Agent
    agent = Agent(
        provider=provider,
        session_manager=session,
        tools=mcp_tools,
        system_prompt="You are a helpful assistant.",
    )

    async for event in agent.run("Navigate to example.com and take a screenshot"):
        ...

    # Clean up
    await manager.disconnect_all()
"""

from .client import MCPClient
from .client import MCPError
from .client import MCPServerConfig
from .manager import MCPManager

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPManager",
    "MCPServerConfig",
]
