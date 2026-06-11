"""
Manages multiple MCP server connections and exposes their tools
as nagents-compatible ToolDefinitions that can be registered
with a nagents Agent.
"""

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any
from typing import cast

from ..types import JsonSchema
from ..types import ToolCall
from ..types import ToolDefinition
from .client import MCPClient
from .client import MCPError
from .client import MCPServerConfig
from .client import _extract_text_content

logger = logging.getLogger(__name__)


def _mcp_type_to_python(prop: dict[str, Any], name: str) -> type:
    """Map MCP JSON Schema type to a Python type for annotations."""
    _TYPE_MAP: dict[str, type] = {
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
        "string": str,
    }
    return _TYPE_MAP.get(prop.get("type", "string"), str)


def _param_to_mcp_name(safe_name: str, properties: dict[str, Any]) -> str:
    """Map a sanitised Python param name back to the original MCP property name.

    Hyphens in MCP names become underscores in Python.  This reverse-maps
    the underscored name back to the original key in *properties*.
    """
    for orig_name in properties:
        if orig_name.replace("-", "_") == safe_name:
            return orig_name
    return safe_name


class MCPManager:
    """Manages connections to multiple MCP servers.

    Connects to configured MCP servers, discovers their tools,
    and provides wrapper functions that can be registered as
    nagents tools. Tool calls are routed to the appropriate
    MCP server automatically.

    Example:
        configs = [
            MCPServerConfig(name="playwright", command="npx", args=["@playwright/mcp@latest"]),
            MCPServerConfig(name="filesystem", command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]),
        ]
        manager = MCPManager(configs)
        await manager.connect_all()

        # Get tools compatible with nagents Agent
        mcp_tools = await manager.get_tools()

        # Use with Agent
        agent = Agent(provider=provider, session_manager=session, tools=mcp_tools)

        # Clean up
        await manager.disconnect_all()
    """

    def __init__(
        self,
        server_configs: list[MCPServerConfig],
        request_timeout: float = 60.0,
    ) -> None:
        """Initialize the MCP manager.

        Args:
            server_configs: List of MCP server configurations.
            request_timeout: Timeout in seconds for MCP requests.
        """
        self.configs = server_configs
        self.request_timeout = request_timeout
        self._clients: dict[str, MCPClient] = {}
        self._tool_map: dict[str, _MCPToolInfo] = {}

    @property
    def is_connected(self) -> bool:
        """Check if all configured servers are connected."""
        if not self._clients:
            return False
        return all(c.is_connected for c in self._clients.values())

    async def connect_all(self) -> None:
        """Connect to all configured MCP servers.

        Connects to each server in parallel, then discovers tools
        from all connected servers.

        Raises:
            MCPError: If any server fails to connect or initialize.
        """
        if not self.configs:
            logger.warning("No MCP servers configured")
            return

        # Connect to all servers in parallel
        async def _connect_one(config: MCPServerConfig) -> MCPClient:
            client = MCPClient(config, request_timeout=self.request_timeout)
            await client.connect()
            return client

        results = await asyncio.gather(
            *(_connect_one(c) for c in self.configs),
            return_exceptions=True,
        )

        for config, result in zip(self.configs, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "Failed to connect to MCP server '%s': %s",
                    config.name,
                    result,
                )
                # Continue with other servers
            else:
                self._clients[config.name] = cast("MCPClient", result)

        if not self._clients:
            raise MCPError(
                message="Failed to connect to any MCP servers",
                code=-1,
            )

        # Discover tools from all connected servers
        await self._discover_tools()

        logger.info(
            "MCP manager connected to %d/%d servers, discovered %d tools",
            len(self._clients),
            len(self.configs),
            len(self._tool_map),
        )

    async def _discover_tools(self) -> None:
        """Discover tools from all connected MCP servers."""
        self._tool_map.clear()

        for name, client in self._clients.items():
            if not client.is_connected:
                continue

            try:
                tools = await client.list_tools()
            except Exception as e:
                logger.error("Failed to list tools from MCP server '%s': %s", name, e)
                continue

            for tool in tools:
                tool_name = tool.get("name", "")
                if not tool_name:
                    continue

                # Prefix tool name with server name to prevent collisions
                qualified_name = f"mcp__{name}__{tool_name}"

                self._tool_map[qualified_name] = _MCPToolInfo(
                    server_name=name,
                    tool_name=tool_name,
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                    title=tool.get("title"),
                )

                logger.debug(
                    "Discovered MCP tool: %s (%s/%s)",
                    qualified_name,
                    name,
                    tool_name,
                )

    async def get_tools(self) -> list[Callable[..., Any]]:
        """Get all MCP tools as callables suitable for nagents Agent.

        Each tool is wrapped as an async function that calls through
        the MCP client to the appropriate server. The function signature
        is dynamically created from the tool's input schema.

        Returns:
            List of async callable functions that can be passed to
            Agent(tools=...).

        Note:
            If tools have not been discovered yet (connect_all not called),
            returns an empty list.
        """
        tools: list[Callable[..., Any]] = []

        for qualified_name, tool_info in self._tool_map.items():
            wrapper = self._create_tool_wrapper(qualified_name, tool_info)
            tools.append(wrapper)

        return tools

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Get all MCP tools as nagents ToolDefinition objects.

        Returns fully-formed ToolDefinition objects that can be
        inspected for metadata. The definitions include the wrapper
        function as the callable.

        Returns:
            List of ToolDefinition objects.
        """
        from ..tools.registry import ToolRegistry

        registry = ToolRegistry()
        for qualified_name, tool_info in self._tool_map.items():
            wrapper = self._create_tool_wrapper(qualified_name, tool_info)
            registry.register(
                wrapper,
                name=qualified_name,
                description=tool_info.description,
                parameters=cast("JsonSchema", tool_info.input_schema) if tool_info.input_schema else None,
            )

        return registry.get_all()

    async def execute_tool(self, tool_call: ToolCall) -> str:
        """Execute an MCP tool call directly.

        This is the internal dispatch used by the wrapper functions.
        Routes the tool call to the appropriate MCP server.

        Args:
            tool_call: The tool call with name and arguments.

        Returns:
            The tool result as a string.

        Raises:
            MCPError: If the tool is not found or execution fails.
        """
        tool_info = self._tool_map.get(tool_call.name)
        if not tool_info:
            raise MCPError(
                message=f"MCP tool not found: {tool_call.name}",
                code=-32601,
            )

        client = self._clients.get(tool_info.server_name)
        if not client or not client.is_connected:
            raise MCPError(
                message=f"MCP server '{tool_info.server_name}' is not connected",
                code=-1,
            )

        content = await client.call_tool(tool_info.tool_name, tool_call.arguments)
        return _extract_text_content(content)

    def _create_tool_wrapper(self, qualified_name: str, tool_info: "_MCPToolInfo") -> Callable[..., Any]:
        """Create an async wrapper function for an MCP tool with proper signature.

        Dynamically builds a function whose parameter signature matches the
        MCP tool's inputSchema so the ToolRegistry can auto-extract the
        correct JSON schema. All parameters default to None, and the required
        fields are communicated through the inputSchema (passed separately
        to register()).

        Args:
            qualified_name: The qualified tool name (e.g., "mcp__playwright__browser_navigate").
            tool_info: Tool metadata including input_schema.

        Returns:
            An async callable function with proper signature.
        """
        # Build parameter list from input schema
        params: list[inspect.Parameter] = []
        schema = tool_info.input_schema
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])

        if properties:
            for prop_name in sorted(properties.keys()):
                safe_name = prop_name.replace("-", "_")
                # Required params have no default → ToolRegistry marks them required in schema
                # Optional params have None default → ToolRegistry marks them optional
                default = inspect.Parameter.empty if prop_name in required else None
                params.append(
                    inspect.Parameter(
                        safe_name,
                        kind=inspect.Parameter.KEYWORD_ONLY,
                        default=default,
                    )
                )
        else:
            # No properties defined — keep a generic **kwargs signature
            pass

        async def wrapper(**kwargs: Any) -> str:
            """Execute an MCP tool.

            This is a wrapper for an MCP tool. Arguments are passed
            directly to the MCP server.
            """
            # Restore original parameter names (underscores back to hyphens)
            restored_args: dict[str, Any] = {}
            for k, v in kwargs.items():
                if v is not None:
                    # Map underscore names back to hyphenated if needed
                    mcp_key = k
                    for orig_name in properties:
                        if orig_name.replace("-", "_") == k:
                            mcp_key = orig_name
                            break
                    restored_args[mcp_key] = v

            tool_call = ToolCall(
                id="",
                name=qualified_name,
                arguments=restored_args,
            )
            return await self.execute_tool(tool_call)

        # Build a proper signature for type hint extraction
        wrapper.__name__ = qualified_name
        wrapper.__qualname__ = qualified_name
        wrapper.__doc__ = tool_info.description
        wrapper.__module__ = f"mcp.{tool_info.server_name}"

        if params:
            new_sig = inspect.Signature(parameters=params)
            wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
            # Map MCP JSON schema types to Python types so ToolRegistry
            # auto-extracts the correct JSON schema (string vs integer vs number)
            wrapper.__annotations__ = {
                p.name: _mcp_type_to_python(properties.get(_param_to_mcp_name(p.name, properties), {}), p.name)
                for p in params
            }

        return wrapper

    async def refresh_tools(self) -> None:
        """Refresh the tool list from all connected servers.

        Call this if a server sends a tools/list_changed notification.
        """
        logger.info("Refreshing MCP tools")
        await self._discover_tools()

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers gracefully."""
        logger.info("Disconnecting from %d MCP servers", len(self._clients))

        async def _disconnect_one(name: str, client: MCPClient) -> None:
            try:
                await client.disconnect()
                logger.info("Disconnected from MCP server '%s'", name)
            except Exception as e:
                logger.error("Error disconnecting from MCP server '%s': %s", name, e)

        await asyncio.gather(*(_disconnect_one(name, client) for name, client in self._clients.items()))

        self._clients.clear()
        self._tool_map.clear()

    async def __aenter__(self) -> "MCPManager":
        await self.connect_all()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect_all()


class _MCPToolInfo:
    """Internal metadata about an MCP tool."""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        title: str | None = None,
    ) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.description = description
        self.input_schema = input_schema
        self.title = title
