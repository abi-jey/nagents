"""Workspace-owned tool selections, shared by web and terminal harnesses."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import TYPE_CHECKING

import yaml

from nagents.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.types import ToolDefinition

    from .runtime import Harness

MAX_BYTES = 65536
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class WorkspaceTools:
    def __init__(self, workspace: Path) -> None:
        self.path = workspace / ".ngn" / "tools.yaml"
        self.agents: dict[str, dict[str, bool]] = {}
        self.revision = ""

    def checked_path(self) -> Path:
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("Workspace tool configuration must not use symlinks")
        return self.path

    @staticmethod
    def parse(source: str) -> dict[str, dict[str, bool]]:
        if len(source.encode()) > MAX_BYTES:
            raise ValueError("Tool configuration exceeds 64 KiB")
        for token in yaml.scan(source):
            if isinstance(token, yaml.tokens.AliasToken | yaml.tokens.AnchorToken):
                raise ValueError("Tool configuration does not support YAML aliases")

        def unique(node: yaml.Node, depth: int = 0) -> None:
            if depth > 4:
                raise ValueError("Tool configuration is too deeply nested")
            if isinstance(node, yaml.MappingNode):
                seen: set[str] = set()
                for key, value in node.value:
                    if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in seen:
                        raise ValueError("Tool configuration keys must be unique strings")
                    seen.add(key.value)
                    unique(value, depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                raise ValueError("Tool configuration uses mappings, not lists")

        node = yaml.compose(source)
        if node is not None:
            unique(node)
        document = yaml.safe_load(source)
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "agents"}
            or type(document["version"]) is not int
            or document["version"] != 1
        ):
            raise ValueError("Tool configuration requires version: 1 and an agents mapping")
        agents = document["agents"]
        if not isinstance(agents, dict) or len(agents) > 128:
            raise ValueError("Invalid tool configuration agents")
        result: dict[str, dict[str, bool]] = {}
        for agent, tools in agents.items():
            if (
                not isinstance(agent, str)
                or not PROFILE_NAME.fullmatch(agent)
                or not isinstance(tools, dict)
                or len(tools) > 512
            ):
                raise ValueError("Invalid tool configuration agent or tool mapping")
            result[agent] = {}
            for name, enabled in tools.items():
                if not isinstance(name, str) or not NAME.fullmatch(name) or type(enabled) is not bool:
                    raise ValueError("Tool selections must map tool names to true or false")
                result[agent][name] = enabled
        return result

    def load(self) -> None:
        path = self.checked_path()
        if not path.exists():
            self.agents, self.revision = {}, ""
            return
        with path.open("r", encoding="utf-8") as stream:
            source = stream.read(MAX_BYTES + 1)
        agents = self.parse(source)
        self.agents = agents
        self.revision = hashlib.sha256(source.encode()).hexdigest()

    def enabled(self, agent: str, name: str) -> bool:
        tools = self.agents.get(agent, {})
        canonical = "schedule_wakeup" if name == "wake_up_in" else name
        return tools.get(name, True) and tools.get(canonical, True)

    def save(self, agents: dict[str, dict[str, bool]], revision: str) -> None:
        source = yaml.safe_dump({"version": 1, "agents": agents}, sort_keys=True)
        self.parse(source)
        self.load()
        if revision != self.revision:
            raise FileExistsError("Tool settings changed on disk. Refresh before saving.")
        path = self.checked_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self.load()


class HarnessToolRegistry(ToolRegistry):
    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness

    def get_all(self) -> list[ToolDefinition]:
        policy = self.harness.tool_settings
        policy.load()
        return [tool for tool in super().get_all() if policy.enabled(self.harness.config.agent, tool.name)]
