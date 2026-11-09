"""Explicit @tool exports from personal/workspace `.agents/tools/*.py` modules."""

from __future__ import annotations

import hashlib
import inspect
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

from nagents.tools.decorators import tool_options
from nagents.tools.registry import ToolRegistry

from ..integrations.plugin_loader import load_tool_module

if TYPE_CHECKING:
    from nagents.types import ToolDefinition

    from ..integrations.plugin_loader import PluginModule

_MODULE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}\Z")
_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
TOOL_ADDED_MESSAGE_NAME = "ngn_tools_added"


def added_tool_names(text: str) -> set[str]:
    """Read our bounded one-line tool-name notice, never arbitrary instructions."""
    for prefix in ("Tool added: ", "Tools added: "):
        if text.startswith(prefix):
            names = text[len(prefix) :].split(", ")
            return set(names) if names and all(_TOOL.fullmatch(name) for name in names) else set()
    return set()


@dataclass(frozen=True)
class ToolSource:
    prefix: str
    path: Path


@dataclass(frozen=True)
class ToolDiagnostic:
    path: Path
    error_type: str
    message: str
    tool: str = ""


@dataclass(frozen=True)
class ToolSources:
    modules: tuple[ToolSource, ...] = ()
    diagnostics: tuple[ToolDiagnostic, ...] = ()
    failed_directories: tuple[Path, ...] = ()


def discover_tool_sources(workspace: Path, *, trust_project: bool = False, read_only: bool = False) -> ToolSources:
    """Discover personal code and explicitly trusted workspace code only.

    Imports execute arbitrary Python before invocation approval. Read-only
    agents must not even scan these sources. Personal tools are user-installed
    code; when the workspace is the home directory it still needs project trust.
    """
    if read_only:
        return ToolSources()
    modules: dict[str, ToolSource] = {}
    diagnostics: list[ToolDiagnostic] = []
    failed: list[Path] = []
    personal = (Path.home() / ".agents/tools").resolve()
    project = (workspace / ".agents/tools").resolve()
    directories = dict.fromkeys((personal, project) if trust_project else (personal,) if personal != project else ())
    for directory in directories:
        try:
            if not directory.exists():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix != ".py" or path.name.startswith("_"):
                    continue
                if not _MODULE.fullmatch(path.stem):
                    diagnostics.append(
                        ToolDiagnostic(
                            path, "InvalidName", "Module names must use ASCII letters, digits, underscores or hyphens."
                        )
                    )
                    continue
                try:
                    if not path.is_file():
                        continue
                except (OSError, RuntimeError):
                    # Loading reports the individual file error and can retain
                    # its old version without skipping later directory entries.
                    pass
                modules[path.stem] = ToolSource(path.stem, path)
        except (OSError, RuntimeError) as error:
            failed.append(directory)
            diagnostics.append(
                ToolDiagnostic(
                    directory,
                    type(error).__name__,
                    "Cannot scan the tool directory; its previously loaded modules are retained.",
                )
            )
    return ToolSources(tuple(modules[name] for name in sorted(modules)), tuple(diagnostics), tuple(failed))


@dataclass(eq=False)
class DiscoveredToolModule:
    """Reference-counted namespace retained while accepted calls still use it."""

    source: ToolSource
    owner: PluginModule
    definitions: tuple[ToolDefinition, ...]
    diagnostics: tuple[ToolDiagnostic, ...] = ()
    _references: int = field(default=1, repr=False)

    def retain(self) -> DiscoveredToolModule:
        if self._references == 0:
            raise RuntimeError("Tool module is closed")
        self._references += 1
        return self

    def close(self) -> None:
        if self._references:
            self._references -= 1
            if self._references == 0:
                self.owner.close()

    def unchanged(self) -> bool:
        """Check all owned imports, including same-size edits and relative helpers."""
        try:
            return all(
                hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                for path, digest in self.owner.sources.items()
            )
        except (OSError, RuntimeError):
            return False


def load_discovered_module(source: ToolSource) -> DiscoveredToolModule:
    owner = load_tool_module(source.path)
    try:
        registry = ToolRegistry()
        diagnostics: list[ToolDiagnostic] = []
        for attribute, function in sorted(vars(owner.module).items()):
            if not inspect.isfunction(function) or function.__module__ != owner.module.__name__:
                continue
            options = tool_options(function)
            if options is None:
                continue
            name = f"{source.prefix}_{options.name or attribute}"
            if not _TOOL.fullmatch(name):
                diagnostics.append(
                    ToolDiagnostic(
                        source.path,
                        "InvalidName",
                        "Qualified tool names must fit 64 ASCII letters, digits, underscores or hyphens.",
                        name,
                    )
                )
                continue
            if registry.get(name) is not None:
                diagnostics.append(
                    ToolDiagnostic(
                        source.path,
                        "DuplicateName",
                        "Another decorated function in this module already owns this tool name.",
                        name,
                    )
                )
                continue
            try:
                registry.register(function, name=name)
            except (Exception, SystemExit) as error:
                diagnostics.append(
                    ToolDiagnostic(
                        source.path,
                        type(error).__name__,
                        "This decorated function could not be registered; other tools remain available.",
                        name,
                    )
                )
        return DiscoveredToolModule(source, owner, tuple(registry.get_all()), tuple(diagnostics))
    except BaseException:
        owner.close()
        raise
