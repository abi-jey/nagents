"""Safe MCP discovery metadata; foreign configuration is detected, never read."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from .resource_config import ignored_project

if TYPE_CHECKING:
    from pathlib import Path

    from .config import HarnessConfig
    from .resource_config import ResourceConfiguration


@dataclass(frozen=True)
class MCPConfigSource:
    path: str
    trusted: bool
    status: Literal["read", "missing", "ignored", "unavailable", "not_read"]


@dataclass(frozen=True)
class MCPDiagnostics:
    status: Literal["not_loaded", "loaded", "retained", "disabled"]
    configured_servers: int
    connected_servers: int
    registered_tools: int
    advertised_tools: int
    effective_source: str
    sources: tuple[MCPConfigSource, ...]
    ignored_configs: tuple[str, ...]
    error: str = ""

    def snapshot(self) -> dict[str, object]:
        return asdict(self)

    def describe(self) -> list[str]:
        lines = [
            f"MCP: {self.configured_servers} configured servers, {self.connected_servers} connected, "
            f"{self.registered_tools} registered tools, {self.advertised_tools} available tools ({self.status})",
            f"MCP effective configuration: {self.effective_source}",
            "MCP ngn configuration sources (last successful read):",
        ]
        if not self.sources:
            lines.append("  Programmatic configuration; no config files are read by this harness.")
        for source in self.sources:
            lines.append(f"  {source.path}: {source.status}; {'trusted' if source.trusted else 'not trusted'}")
        lines.extend(
            f"Ignored foreign MCP configuration: {path}; ngn does not import this file."
            for path in self.ignored_configs
        )
        if self.status == "retained":
            lines.append(
                "MCP reload failed; the counts and configuration sources above describe the retained generation."
            )
        if self.status == "disabled":
            lines.append(
                "MCP subprocess startup is disabled for this harness; configured declarations were not executed."
            )
        return lines


def _presence(path: Path) -> Literal["present", "missing", "unavailable"]:
    try:
        path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    return "present"


def _trusted_location(path: Path, config: HarnessConfig) -> bool:
    for trusted in config.resource_paths:
        if trusted == path:
            return True
        try:
            if trusted.resolve() == path.resolve():
                return True
        except (OSError, RuntimeError):
            continue
    return False


def ignored_foreign_configs(config: HarnessConfig) -> tuple[str, ...]:
    """Only inspect directory entries, including broken links; never open content."""
    return tuple(
        str(path)
        for path in (config.workspace / ".vscode" / "mcp.json", config.workspace / "opencode.json")
        if _presence(path) == "present" and not _trusted_location(path, config)
    )


def configuration_sources(config: HarnessConfig, configured: ResourceConfiguration) -> tuple[MCPConfigSource, ...]:
    # ResourceConfiguration records what the successful generation actually read.
    # Before discovery (including --demo), load_config's startup provenance is the
    # only available evidence; do not open files just to populate a diagnostics UI.
    checked = {item.path: item for item in configured.sources}
    sources: list[MCPConfigSource] = []
    paths = tuple(checked) if checked else config.resource_paths
    for path in paths:
        source = checked.get(path)
        if source is not None:
            status: Literal["read", "missing", "ignored", "unavailable", "not_read"] = (
                "read" if source.present else "missing"
            )
        elif path in config.config_paths:
            status = "read"
        else:
            presence = _presence(path)
            status = "not_read" if presence == "present" else presence
        sources.append(MCPConfigSource(str(path), True, status))
    project = config.workspace / ".ngn" / "config.json"
    if config.resource_paths and project not in config.resource_paths and not config.trust_project:
        presence = _presence(project)
        # An explicit trusted symlink to this project file already grants trust.
        if presence != "present" or ignored_project(config):
            sources.append(MCPConfigSource(str(project), False, "ignored" if presence == "present" else presence))
    return tuple(sources)
