"""
Low-level MCP client using JSON-RPC 2.0 over STDIO transport.

Communicates with an MCP server subprocess via stdin/stdout using
the JSON-RPC 2.0 protocol. Handles the initialization handshake,
sends requests, and matches responses by request ID.
"""

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import cast

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_REQUEST_TIMEOUT = 60.0


@dataclass
class MCPServerConfig:
    """Configuration for launching an MCP server subprocess.

    Attributes:
        name: Logical name for this server (e.g., "playwright", "filesystem").
        command: The command to run (e.g., "npx", "python", "node").
        args: Arguments to pass to the command.
        env: Optional environment variables to set for the subprocess.
        cwd: Optional working directory for the subprocess.
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None


@dataclass
class _PendingRequest:
    """Tracks a pending JSON-RPC request awaiting a response."""

    method: str
    future: asyncio.Future[dict[str, Any]]


class MCPClient:
    """Low-level MCP client for a single MCP server over STDIO.

    Spawns a subprocess and communicates via JSON-RPC 2.0 over stdin/stdout.
    Handles initialization handshake, request/response matching, and cleanup.

    Example:
        config = MCPServerConfig(
            name="playwright",
            command="npx",
            args=["@playwright/mcp@latest"],
        )
        client = MCPClient(config)
        await client.connect()

        tools = await client.list_tools()
        result = await client.call_tool("browser_navigate", {"url": "https://example.com"})

        await client.disconnect()
    """

    def __init__(self, config: MCPServerConfig, request_timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.config = config
        self.request_timeout = request_timeout

        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._connected = False
        self._server_capabilities: dict[str, Any] = {}
        self._server_info: dict[str, Any] = {}

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected to the MCP server."""
        return self._connected

    @property
    def server_capabilities(self) -> dict[str, Any]:
        """Get the server capabilities negotiated during initialization."""
        return self._server_capabilities

    @property
    def server_info(self) -> dict[str, Any]:
        """Get the server implementation info."""
        return self._server_info

    async def connect(self) -> None:
        """Connect to the MCP server.

        Spawns the subprocess, performs the initialization handshake,
        and starts the response reader loop.

        Raises:
            RuntimeError: If already connected.
            OSError: If the subprocess cannot be started.
            asyncio.TimeoutError: If initialization times out.
            ValueError: If the server rejects the protocol version.
        """
        if self._connected:
            raise RuntimeError(f"Already connected to MCP server '{self.config.name}'")

        env: dict[str, str] | None = None
        if self.config.env:
            env = os.environ.copy()
            env.update(self.config.env)

        logger.info(
            "Starting MCP server '%s': %s %s",
            self.config.name,
            self.config.command,
            " ".join(self.config.args),
        )

        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.config.cwd,
        )

        # Start the response reader
        self._reader_task = asyncio.create_task(self._read_responses())

        # Perform initialization handshake
        await self._initialize()

        self._connected = True
        logger.info("Connected to MCP server '%s'", self.config.name)

    async def _initialize(self) -> None:
        """Perform the MCP initialization handshake."""
        init_response = await self._send_request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "nagents",
                    "version": "0.3.0",
                },
            },
        )

        server_version = init_response.get("protocolVersion")
        if server_version != MCP_PROTOCOL_VERSION:
            logger.warning(
                "Server '%s' protocol version %s differs from client %s",
                self.config.name,
                server_version,
                MCP_PROTOCOL_VERSION,
            )

        self._server_capabilities = init_response.get("capabilities", {})
        self._server_info = init_response.get("serverInfo", {})

        # Send initialized notification
        await self._send_notification("notifications/initialized")

        logger.info(
            "MCP handshake complete with '%s': %s %s",
            self.config.name,
            self._server_info.get("name", "unknown"),
            self._server_info.get("version", ""),
        )

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response.

        Args:
            method: The JSON-RPC method name.
            params: Optional parameters dict.

        Returns:
            The response result dict.

        Raises:
            RuntimeError: If not connected.
            asyncio.TimeoutError: If the request times out.
            Exception: If the server returns a JSON-RPC error.
        """
        if not self._process or self._process.stdin is None:
            raise RuntimeError(f"Not connected to MCP server '{self.config.name}'")

        request_id = self._next_id
        self._next_id += 1

        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = _PendingRequest(method=method, future=future)

        request_json = json.dumps(request) + "\n"
        logger.debug("MCP → %s [id=%d]: %s", self.config.name, request_id, method)
        self._process.stdin.write(request_json.encode())
        await self._process.stdin.drain()

        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as e:
            raise TimeoutError(
                f"Request '{method}' to MCP server '{self.config.name}' timed out after {self.request_timeout}s"
            ) from e
        finally:
            self._pending.pop(request_id, None)

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected).

        Args:
            method: The notification method name.
            params: Optional parameters dict.
        """
        if not self._process or self._process.stdin is None:
            raise RuntimeError(f"Not connected to MCP server '{self.config.name}'")

        notification: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            notification["params"] = params

        msg_json = json.dumps(notification) + "\n"
        self._process.stdin.write(msg_json.encode())
        await self._process.stdin.drain()

    async def _read_responses(self) -> None:
        """Read JSON-RPC responses from stdout and dispatch to pending futures.

        Runs as a background task. Handles both responses (with id) and
        notifications (without id).
        """
        if not self._process or self._process.stdout is None:
            return

        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break

                try:
                    message: dict[str, Any] = json.loads(line.decode())
                except json.JSONDecodeError:
                    logger.warning("MCP ← %s: invalid JSON: %s", self.config.name, line[:200])
                    continue

                msg_id = message.get("id")

                if msg_id is not None and "method" not in message:
                    # This is a response (has id, no method)
                    pending = self._pending.get(msg_id)
                    if pending:
                        if "error" in message:
                            error = message["error"]
                            error_msg = error.get("message", "Unknown JSON-RPC error")
                            error_code = error.get("code", -1)
                            logger.error(
                                "MCP ← %s [id=%d]: ERROR %s (code=%d)",
                                self.config.name,
                                msg_id,
                                error_msg,
                                error_code,
                            )
                            pending.future.set_exception(
                                MCPError(
                                    message=error_msg,
                                    code=error_code,
                                    data=error.get("data"),
                                )
                            )
                        else:
                            result = message.get("result", {})
                            pending.future.set_result(result)
                    else:
                        logger.debug("MCP ← %s [id=%d]: no pending request for this id", self.config.name, msg_id)

                elif msg_id is not None and "method" in message:
                    # This is a request from the server (has both id and method)
                    # We don't currently handle server-initiated requests
                    logger.debug(
                        "MCP ← %s [id=%d]: server request '%s' (ignored)",
                        self.config.name,
                        msg_id,
                        message.get("method"),
                    )

                elif "method" in message:
                    # This is a notification (no id, has method)
                    method = message.get("method", "")
                    logger.debug("MCP ← %s: notification '%s'", self.config.name, method)
                    if method == "notifications/tools/list_changed":
                        logger.info("MCP server '%s' reported tools changed", self.config.name)

                else:
                    logger.debug("MCP ← %s: unrecognized message format", self.config.name)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error reading MCP responses from '%s'", self.config.name)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover available tools from the MCP server.

        Returns:
            List of tool definition dicts with keys: name, description, inputSchema.
        """
        result = await self._send_request("tools/list")
        return cast("list[dict[str, Any]]", result.get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a tool on the MCP server.

        Args:
            name: The tool name (must match exactly).
            arguments: Tool arguments as keyword dict.

        Returns:
            List of content items from the tool result.
            Each item has a "type" field (e.g., "text", "image", "resource").
            Text items have a "text" field with the content.

        Raises:
            MCPError: If the server returns a protocol error.
        """
        result = await self._send_request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )

        # Check for tool execution error (isError flag on result)
        if result.get("isError"):
            content = result.get("content", [])
            error_text = _extract_text_content(content)
            logger.warning(
                "MCP tool '%s' on '%s' returned error: %s",
                name,
                self.config.name,
                error_text,
            )

        return cast("list[dict[str, Any]]", result.get("content", []))

    async def list_resources(self) -> list[dict[str, Any]]:
        """Discover available resources from the MCP server."""
        result = await self._send_request("resources/list")
        return cast("list[dict[str, Any]]", result.get("resources", []))

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        """Read a resource from the MCP server.

        Args:
            uri: The resource URI to read.

        Returns:
            List of content items.
        """
        result = await self._send_request("resources/read", {"uri": uri})
        return cast("list[dict[str, Any]]", result.get("contents", []))

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Discover available prompt templates from the MCP server."""
        result = await self._send_request("prompts/list")
        return cast("list[dict[str, Any]]", result.get("prompts", []))

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        """Get a prompt template from the MCP server.

        Args:
            name: The prompt name.
            arguments: Optional arguments for the prompt template.

        Returns:
            The prompt result with messages.
        """
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        result = await self._send_request("prompts/get", params)
        return result

    async def disconnect(self) -> None:
        """Disconnect from the MCP server gracefully.

        Closes stdin to signal EOF, waits briefly for the process to exit,
        then terminates if still running. Cancels the reader task.
        """
        if not self._connected or not self._process:
            return

        logger.info("Disconnecting from MCP server '%s'", self.config.name)

        self._connected = False

        # Cancel the reader task
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None

        # Fail any pending requests
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(
                    RuntimeError(f"MCP server '{self.config.name}' disconnected")
                )
        self._pending.clear()

        # Close stdin to signal EOF
        if self._process.stdin:
            with contextlib.suppress(Exception):
                self._process.stdin.close()

        # Wait for process to exit gracefully
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
            logger.info("MCP server '%s' exited with code %d", self.config.name, self._process.returncode)
        except TimeoutError:
            # Terminate
            logger.warning("MCP server '%s' did not exit, sending SIGTERM", self.config.name)
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except TimeoutError:
                # Force kill
                logger.warning("MCP server '%s' did not respond to SIGTERM, sending SIGKILL", self.config.name)
                self._process.kill()
                await self._process.wait()

        self._process = None

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()


def _extract_text_content(content: list[dict[str, Any]]) -> str:
    """Extract text from MCP content items.

    Args:
        content: List of MCP content items.

    Returns:
        Concatenated text from all text content items.
    """
    texts = []
    for item in content:
        if item.get("type") == "text":
            texts.append(item.get("text", ""))
    return "\n".join(texts)


class MCPError(Exception):
    """Raised when an MCP server returns a JSON-RPC error response."""

    def __init__(self, message: str, code: int = -1, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"MCP error (code={code}): {message}")
