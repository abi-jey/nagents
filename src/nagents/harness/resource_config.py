"""Trusted, reloadable extension declarations (never infer new trust roots)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from nagents.mcp import MCPServerConfig

if TYPE_CHECKING:
    from .config import HarnessConfig


def parse_mcp_servers(value: object, directory: Path, workspace: Path) -> tuple[MCPServerConfig, ...]:
    if not isinstance(value, dict):
        raise ValueError("mcp_servers must be a mapping of server names to stdio configurations")
    servers: list[MCPServerConfig] = []
    for name, fields in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("MCP server names must contain 1..64 letters, digits, underscores or hyphens")
        if not isinstance(fields, dict) or fields.keys() - {"command", "args", "env", "cwd"}:
            raise ValueError(f"MCP server {name}: expected command, args, env and cwd only (stdio transport)")
        command, args, env, cwd = (
            fields.get("command"),
            fields.get("args", []),
            fields.get("env", {}),
            fields.get("cwd", ""),
        )
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError(f"MCP server {name}: command must be a nonempty string")
        if not isinstance(args, list) or not all(isinstance(arg, str) and "\x00" not in arg for arg in args):
            raise ValueError(f"MCP server {name}: args must be an array of strings")
        if not isinstance(env, dict) or not all(
            isinstance(key, str)
            and key
            and "=" not in key
            and "\x00" not in key
            and isinstance(item, str)
            and "\x00" not in item
            for key, item in env.items()
        ):
            raise ValueError(f"MCP server {name}: env must map environment variable names to strings")
        if not isinstance(cwd, str) or "\x00" in cwd:
            raise ValueError(f"MCP server {name}: cwd must be a path string")
        location = (directory / Path(cwd).expanduser()).resolve() if cwd else workspace
        servers.append(MCPServerConfig(name, command, list(args), dict(env), str(location)))
    return tuple(servers)


def parse_plugins(value: object, directory: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("plugins must be an array of 'path.py:setup' or 'module:setup' strings")
    references: list[str] = []
    for item in value:
        module, separator, entry = item.rpartition(":")
        if not separator or not entry.isidentifier() or not module:
            raise ValueError("Invalid plugin reference; expected path.py:setup or module:setup")
        if module.endswith(".py"):
            module = str((directory / Path(module).expanduser()).resolve())
        elif not all(part.isidentifier() for part in module.split(".")):
            raise ValueError("Invalid installed plugin module")
        references.append(f"{module}:{entry}")
    return tuple(references)


def resource_settings(config: HarnessConfig) -> tuple[tuple[str, ...], tuple[MCPServerConfig, ...]]:
    """Read every previously trusted source on every response, even absent files.

    Only extension declarations are reloaded. Runtime UI provider selections,
    permissions, storage, and the trust roots remain host-owned.
    """
    if not config.resource_paths:
        return config.plugins, config.mcp_servers
    plugins: tuple[str, ...] = ()
    servers: tuple[MCPServerConfig, ...] = ()
    for path in config.resource_paths:
        try:
            with path.open(encoding="utf-8") as stream:
                source = stream.read(1048577)
        except FileNotFoundError:
            continue
        if len(source.encode()) > 1048576:
            raise ValueError("Extension configuration exceeds 1 MiB")
        document = json.loads(source)
        if not isinstance(document, dict):
            raise ValueError("Extension configuration must be a mapping")
        if "plugins" in document:
            plugins = parse_plugins(document["plugins"], path.parent)
        if "mcp_servers" in document:
            servers = parse_mcp_servers(document["mcp_servers"], path.parent, config.workspace)
    return (*plugins, *config.cli_plugins), servers
