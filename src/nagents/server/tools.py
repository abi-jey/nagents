"""Built-in tools for the agent server."""

import hashlib
import json
import mimetypes
import os
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nagents import docker_run

from .scheduler import wake_up_in

# ── Attachment store (in-memory registry, files on disk) ──────────────────────


@dataclass
class Attachment:
    id: str
    path: Path
    description: str
    content_type: str
    created_at: float
    fetch_count: int = 0
    last_fetched_at: float | None = None


_attachments: dict[str, Attachment] = {}
_pending_attachments: list[str] = []


def _reset_pending() -> None:
    _pending_attachments.clear()


def _collect_pending() -> list[str]:
    return list(_pending_attachments)


def _make_attachment_id(path: Path) -> str:
    return hashlib.sha256(f"{path.resolve()}{time.time()}".encode()).hexdigest()[:12]


# ── Tool functions ────────────────────────────────────────────────────────────


def run_shell_command(command: str) -> str:
    """Run a shell command inside an isolated Docker sandbox and return output."""
    return docker_run(command)


def read_file(path: str) -> str:
    """Read a file."""
    return Path(path).read_text()


def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Written to {path}"


def list_directory(path: str = ".") -> str:
    """List directory contents."""
    return "\n".join(str(p) for p in Path(path).iterdir())


def attach_file(file_path: str, description: str = "") -> str:
    """Attach an existing file so it can be served via /attachments/{id}."""
    p = Path(file_path)
    if not p.exists():
        return f"Error: File not found: {file_path}"
    if not p.is_file():
        return f"Error: Not a regular file: {file_path}"

    aid = _make_attachment_id(p)
    mime, _ = mimetypes.guess_type(p.name)

    _attachments[aid] = Attachment(
        id=aid,
        path=p.resolve(),
        description=description or p.name,
        content_type=mime or "application/octet-stream",
        created_at=time.time(),
    )
    _pending_attachments.append(aid)

    size = p.stat().st_size
    return f"File attached: /attachments/{aid}\nName: {p.name}\nSize: {size} bytes"


# ── Runtime MCP bridge ───────────────────────────────────────────────────────
# Async handler, installed by the app, that connects an MCP server into the
# live agent immediately. Signature: (name, command, args) -> new tool names.
_MCPConnectHandler = Callable[[str, str, list[str]], Awaitable[list[str]]]
_mcp_connect_handler: _MCPConnectHandler | None = None


def set_mcp_connect_handler(handler: _MCPConnectHandler) -> None:
    """Install the immediate-connect handler used by add_mcp_server."""
    global _mcp_connect_handler
    _mcp_connect_handler = handler


async def add_mcp_server(name: str, command: str, args: str = "") -> str:
    """Add an MCP server and connect it immediately, mid-conversation.

    The server's tools are registered with the live agent right away and can
    be called in the NEXT tool round of this same conversation — no need to
    wait for the next chat turn. The server is also persisted to the MCP
    config file so it survives restarts.

    Args:
        name: A unique name for this MCP server (e.g., "playwright", "filesystem")
        command: The command to run the MCP server (e.g., "npx", "playwright-mcp")
        args: Space-separated arguments for the command (e.g., "@modelcontextprotocol/server-filesystem /tmp")
    """
    mcp_config = os.getenv("NAGENTS_MCP_CONFIG", "/data/mcp.json")
    config_path = Path(mcp_config)

    # Parse existing configs to check for duplicates
    existing_names: set[str] = set()
    if config_path.is_file():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    existing_names.add(json.loads(line)["name"])
                except (json.JSONDecodeError, KeyError):
                    continue

    if name in existing_names:
        return f"Error: MCP server '{name}' already exists. Use a different name or remove it from {mcp_config} first."

    # Build the config entry
    arg_list = args.split() if args else []
    entry = {"name": name, "command": command, "args": arg_list}

    # Append to config file (JSON lines format)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    # Ensure MCP is enabled
    if os.getenv("NAGENTS_MCP_ENABLED", "").lower() not in ("1", "true", "yes"):
        return (
            f"MCP server '{name}' added to {mcp_config}, but NAGENTS_MCP_ENABLED is not set. "
            f"It will be loaded once MCP is enabled."
        )

    # Connect immediately so the tools are callable in this same conversation
    if _mcp_connect_handler is not None:
        try:
            tool_names = await _mcp_connect_handler(name, command, arg_list)
        except Exception as e:
            return (
                f"MCP server '{name}' saved to {mcp_config}, but immediate connection failed: {e}\n"
                f"It will be retried automatically on the next chat turn."
            )
        listed = "\n".join(f"- {t}" for t in tool_names) if tool_names else "(server reported no tools)"
        return (
            f"MCP server '{name}' connected. The following tools are available NOW — "
            f"you can call them right away in this conversation:\n{listed}"
        )

    return (
        f"MCP server '{name}' added to {mcp_config}.\n"
        f"Command: {command} {' '.join(arg_list)}\n"
        f"It will be auto-loaded on the next chat turn."
    )


BASE_TOOLS = [
    run_shell_command,
    read_file,
    write_file,
    list_directory,
    attach_file,
    add_mcp_server,
    wake_up_in,
]
