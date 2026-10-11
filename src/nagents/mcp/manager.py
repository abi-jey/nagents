"""
Manages multiple MCP server connections and exposes their tools
as nagents-compatible ToolDefinitions that can be registered
with a nagents Agent.
"""

import asyncio
import inspect
import keyword
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import cast

from .._async import finish_on_cancel
from ..types import JsonSchema
from ..types import ToolCall
from ..types import ToolDefinition
from .client import MCPClient
from .client import MCPError
from .client import MCPServerConfig
from .client import _extract_text_content
from .client import _failure_category

logger = logging.getLogger(__name__)


@dataclass
class _Connection:
    """A client remains alive until all advertised generations release it."""

    client: MCPClient
    references: int = 1

    async def release(self) -> None:
        self.references -= 1
        if self.references == 0:
            await finish_on_cancel(self.client.disconnect())


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
    kind = prop.get("type", "string")
    return _TYPE_MAP.get(kind, str) if isinstance(kind, str) else object


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
        self._connections: dict[str, _Connection] = {}
        self._tool_map: dict[str, _MCPToolInfo] = {}
        self._tool_revisions: dict[str, int] = {}

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
            self._clients[config.name] = client
            self._connections[config.name] = _Connection(client)
            return client

        try:
            results = await asyncio.gather(
                *(_connect_one(c) for c in self.configs),
                return_exceptions=True,
            )
        except BaseException:
            await finish_on_cancel(self.disconnect_all())
            raise

        for config, result in zip(self.configs, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "MCP startup failed: server=%s error_type=%s",
                    config.name,
                    type(result).__name__,
                )
                # Continue with other servers

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

    async def add_server(self, config: MCPServerConfig, *, strict: bool = False) -> list[Callable[..., Any]]:
        """Connect a single MCP server at runtime and return its tools.

        Unlike connect_all(), this adds one server without disturbing any
        existing connections — used when a server is registered while an
        agent run is in progress (e.g. via the add_mcp_server tool).

        Args:
            config: The server configuration to connect.

        Returns:
            The server's tools as callables, ready for ToolRegistry.register().

        Raises:
            MCPError: If a server with this name is already connected, or
                      the connection itself fails.
        """
        if config.name in self._clients:
            raise MCPError(
                message=f"MCP server '{config.name}' is already connected",
                code=-1,
            )

        client = MCPClient(config, request_timeout=self.request_timeout)
        try:
            await client.connect()
        except BaseException as e:
            await finish_on_cancel(client.disconnect())
            if not isinstance(e, Exception):
                raise
            raise MCPError(
                message=f"Failed to connect to MCP server '{config.name}' ({type(e).__name__})",
                code=-1,
                stage="mcp_start",
                category=_failure_category(e, client),
            ) from e

        self._clients[config.name] = client
        self._connections[config.name] = _Connection(client)
        if all(c.name != config.name for c in self.configs):
            self.configs.append(config)

        # Discover only this server's tools. On failure the connection is
        # kept (refresh_tools() can rediscover later) — mirroring connect_all's
        # per-server error tolerance.
        try:
            wrappers = await self.refresh_server_tools(config.name)
        except BaseException as e:
            if strict or not isinstance(e, Exception):
                await finish_on_cancel(self.remove_server(config.name))
            if not isinstance(e, Exception):
                raise
            logger.error("MCP discovery failed: server=%s error_type=%s", config.name, type(e).__name__)
            if strict:
                raise MCPError(
                    "MCP tool discovery failed", code=-1, stage="mcp_discover", category=_failure_category(e, client)
                ) from None
            return []

        logger.info("MCP server '%s' added at runtime with %d tools", config.name, len(wrappers))
        return wrappers

    def server_tools_changed(self, name: str) -> bool:
        return self._tool_revisions.get(name, -1) != getattr(self._clients[name], "tools_revision", 0)

    async def refresh_server_tools(self, name: str) -> list[Callable[..., Any]]:
        """Rediscover one server without replacing its connection or other maps."""
        client = self._clients[name]
        revision = getattr(client, "tools_revision", 0)
        try:
            tools = await client.list_tools()
            discovered: dict[str, _MCPToolInfo] = {}
            wrappers: list[Callable[..., Any]] = []
            for tool in tools:
                tool_name = tool.get("name", "")
                if not tool_name:
                    continue
                qualified_name = f"mcp__{name}__{tool_name}"
                tool_info = _MCPToolInfo(
                    server_name=name,
                    tool_name=tool_name,
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                    title=tool.get("title"),
                )
                discovered[qualified_name] = tool_info
                wrappers.append(self._create_tool_wrapper(qualified_name, tool_info))
        except Exception as error:
            raise MCPError(
                "MCP tool discovery failed", stage="mcp_discover", category=_failure_category(error, client)
            ) from None
        self._tool_map = {key: value for key, value in self._tool_map.items() if value.server_name != name}
        self._tool_map.update(discovered)
        self._tool_revisions[name] = revision
        return wrappers

    def retain_server(self, owner: "MCPManager", name: str) -> None:
        """Share an unchanged discovered connection with another generation.

        The operation has no await point; neither generation can lose ownership
        during the transfer. Each keeps its own immutable advertised tool map.
        """
        if name in self._clients:
            raise ValueError("MCP server is already registered in this manager")
        connection = owner._connections[name]
        if not connection.client.is_connected:
            raise ValueError("Cannot retain a disconnected MCP server")
        connection.references += 1
        self._connections[name] = connection
        self._clients[name] = connection.client
        self.configs.append(connection.client.config)
        self._tool_map.update({key: tool for key, tool in owner._tool_map.items() if tool.server_name == name})
        self._tool_revisions[name] = owner._tool_revisions.get(name, -1)

    async def remove_server(self, name: str) -> int:
        """Disconnect a single server and drop its tools from the tool map.

        Args:
            name: The server name to remove.

        Returns:
            The number of tools removed.
        """
        self._clients.pop(name, None)
        connection = self._connections.pop(name, None)
        if connection is not None:
            try:
                await connection.release()
            except Exception as e:
                logger.error("MCP disconnect failed: server=%s error_type=%s", name, type(e).__name__)

        self.configs = [c for c in self.configs if c.name != name]
        self._tool_revisions.pop(name, None)

        removed = [q for q, info in self._tool_map.items() if info.server_name == name]
        for q in removed:
            del self._tool_map[q]

        logger.info("MCP server '%s' removed (%d tools dropped)", name, len(removed))
        return len(removed)

    async def _discover_tools(self) -> None:
        """Discover tools from all connected MCP servers."""
        for name, client in self._clients.items():
            if not client.is_connected:
                self._tool_map = {key: tool for key, tool in self._tool_map.items() if tool.server_name != name}
                self._tool_revisions.pop(name, None)
                continue

            try:
                await self.refresh_server_tools(name)
            except Exception as e:
                logger.error("MCP discovery failed: server=%s error_type=%s", name, type(e).__name__)

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

    async def get_tool_definitions(self, *, server_name: str = "") -> list[ToolDefinition]:
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
            if server_name and tool_info.server_name != server_name:
                continue
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

        if (
            properties
            and len({name.replace("-", "_") for name in properties}) == len(properties)
            and all(name.replace("-", "_").isidentifier() and not keyword.iskeyword(name) for name in properties)
        ):
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
            for key, value in kwargs.items():
                # Keep explicit null and original JSON property names intact.
                original = key if key in properties else _param_to_mcp_name(key, properties)
                restored_args[original] = value

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

        async def _disconnect_one(name: str, connection: _Connection) -> None:
            try:
                await connection.release()
                logger.info("Disconnected from MCP server '%s'", name)
            except Exception as e:
                logger.error("MCP disconnect failed: server=%s error_type=%s", name, type(e).__name__)

        connections, self._connections = self._connections, {}
        self._clients.clear()
        self._tool_map.clear()
        self._tool_revisions.clear()
        await asyncio.gather(*(_disconnect_one(name, connection) for name, connection in connections.items()))

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
