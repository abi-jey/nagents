"""Versioned, declarative agent definitions. Loading never starts resources."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from nagents.harness.config import API_NAMES
from nagents.harness.config import PROVIDERS

BUILTINS = ("read_file", "list_files", "find", "search", "edit", "write", "shell")
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Secret(Definition):
    source: Literal["env", "saved"] = "env"
    name: str = Field(min_length=1, max_length=128)


class ProviderDefinition(Definition):
    type: str = "openai"
    model: str = Field(min_length=1)
    base_url: str = ""
    api: str = "auto"
    api_version: str = ""
    secret: str = ""
    auth: Literal["api-key", "chatgpt"] = "api-key"

    @model_validator(mode="after")
    def supported(self) -> ProviderDefinition:
        if self.type not in PROVIDERS or self.api not in API_NAMES or self.api == "completions":
            raise ValueError("Unsupported conversational provider/API")
        if self.auth == "chatgpt" and (
            self.type not in {"openai", "openai_compatible"} or self.base_url or self.api != "auto"
        ):
            raise ValueError("ChatGPT authentication requires the default OpenAI endpoint and auto API")
        return self


class Instructions(Definition):
    text: str = ""
    file: str = ""


class ToolSelection(Definition):
    ref: str
    description: str = ""

    @model_validator(mode="after")
    def registered_definition(self) -> ToolSelection:
        if self.description:
            raise ValueError(
                "Tool definitions are read-only; remove the description override and use the registered tool documentation"
            )
        return self


class MCPDefinition(Definition):
    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class MCPSelection(Definition):
    server: str
    tools: list[ToolSelection] = Field(default_factory=list)


class Invocation(Definition):
    agent: str
    description: str = ""
    source_port: Literal["top", "right", "bottom", "left"] = "right"
    target_port: Literal["top", "right", "bottom", "left"] = "left"
    source_offset: float = Field(default=0.5, ge=0, le=1)
    target_offset: float = Field(default=0.5, ge=0, le=1)


class Generation(Definition):
    temperature: float = Field(default=1, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1)
    top_p: float = Field(default=1, ge=0, le=1)
    stop: list[str] = Field(default_factory=list)


class AgentDefinition(Definition):
    name: str = ""
    provider: str = ""
    instructions: Instructions = Field(default_factory=Instructions)
    tools: list[ToolSelection] = Field(default_factory=list)
    mcp: list[MCPSelection] = Field(default_factory=list)
    invokes: list[Invocation] = Field(default_factory=list)
    max_tool_rounds: int = Field(default=30, ge=1, le=1000)
    generation: Generation = Field(default_factory=Generation)


class Defaults(Definition):
    provider: str
    max_subagent_depth: int = Field(default=2, ge=0, le=8)


class Position(Definition):
    x: float = 100
    y: float = 100


class Design(Definition):
    version: Literal[1] = 1
    id: str
    entrypoint: str
    defaults: Defaults
    secrets: dict[str, Secret] = Field(default_factory=dict)
    providers: dict[str, ProviderDefinition]
    mcp_servers: dict[str, MCPDefinition] = Field(default_factory=dict)
    agents: dict[str, AgentDefinition]
    layout: dict[str, Position] = Field(default_factory=dict)
    channels: dict[str, str] = Field(default_factory=dict)

    def document(self) -> dict[str, object]:
        """Materialize editor defaults without enabling unset generation options."""
        data = self.model_dump(mode="json")
        for name, agent in self.agents.items():
            data["agents"][name]["generation"] = agent.generation.model_dump(exclude_unset=True)
        return data

    @model_validator(mode="after")
    def references(self) -> Design:
        for name in (self.id, *self.agents, *self.providers, *self.secrets, *self.mcp_servers):
            if not IDENTIFIER.fullmatch(name):
                raise ValueError(f"Invalid identifier: {name}")
        if not 1 <= len(self.agents) <= 64:
            raise ValueError("A design requires 1 to 64 agents")
        if self.entrypoint not in self.agents or self.defaults.provider not in self.providers:
            raise ValueError("Unknown entrypoint or default provider")
        for provider in self.providers.values():
            if provider.auth == "api-key" and provider.secret not in self.secrets:
                raise ValueError(f"Unknown secret: {provider.secret}")
        for server in self.mcp_servers.values():
            if any(ref not in self.secrets for ref in server.secrets.values()):
                raise ValueError("MCP server references an unknown secret")
        for agent in self.agents.values():
            if agent.provider and agent.provider not in self.providers:
                raise ValueError(f"Unknown provider: {agent.provider}")
            if any(tool.ref.removeprefix("builtin.") not in BUILTINS for tool in agent.tools):
                raise ValueError("Unknown built-in tool")
            if len({tool.ref for tool in agent.tools}) != len(agent.tools):
                raise ValueError("Duplicate tool selection")
            if any(edge.agent not in self.agents for edge in agent.invokes):
                raise ValueError("Unknown delegation target")
            if len({edge.agent for edge in agent.invokes}) != len(agent.invokes):
                raise ValueError("Duplicate delegation target")
            if any(selection.server not in self.mcp_servers for selection in agent.mcp):
                raise ValueError("Unknown MCP server")
        if any(name not in self.agents for name in self.layout):
            raise ValueError("Layout references an unknown agent")
        for connection, agent_id in self.channels.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", connection) or agent_id not in self.agents:
                raise ValueError("Channels must map connection IDs to known agents")
        return self

    def resolved(self, workspace: Path) -> Design:
        result = self.model_copy(deep=True)
        for agent in result.agents.values():
            relative = agent.instructions.file
            if relative:
                path = workspace / relative
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    raise ValueError("Instruction files must be workspace-relative")
                if not path.resolve().is_relative_to(workspace.resolve()) or not path.is_file():
                    raise ValueError("Instruction file is unavailable in this workspace")
                with path.open("rb") as stream:
                    content = stream.read(65537)
                if len(content) > 65536:
                    raise ValueError("Instruction file exceeds 64 KiB")
                agent.instructions.text = "\n\n".join(filter(None, (content.decode("utf-8"), agent.instructions.text)))
                agent.instructions.file = ""
        return result


def parse(source: str) -> Design:
    if len(source.encode()) > 48 * 1024:
        raise ValueError("Design exceeds 48 KiB")
    # Aliases introduce shared/cyclic structures and duplicate keys hide edits.
    for token in yaml.scan(source):
        if isinstance(token, yaml.tokens.AliasToken | yaml.tokens.AnchorToken):
            raise ValueError("YAML anchors and aliases are not supported")
    node = yaml.compose(source)

    def unique(current: yaml.Node, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("YAML nesting exceeds 32 levels")
        if isinstance(current, yaml.MappingNode):
            keys: set[str] = set()
            for key, value in current.value:
                if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in keys:
                    raise ValueError("YAML keys must be unique strings")
                keys.add(key.value)
                unique(value, depth + 1)
        elif isinstance(current, yaml.SequenceNode):
            for child in current.value:
                unique(child, depth + 1)

    if node is not None:
        unique(node)
    return Design.model_validate(yaml.safe_load(source))


def serialize(design: Design) -> str:
    return str(yaml.safe_dump(design.model_dump(mode="json", exclude_unset=True), sort_keys=False, allow_unicode=True))


STARTER = """version: 1
id: my-team
entrypoint: assistant
defaults:
  provider: primary
secrets:
  primary_key:
    source: env
    name: OPENAI_API_KEY
providers:
  primary:
    type: openai
    model: gpt-4.1
    secret: primary_key
agents:
  assistant:
    name: Assistant
    instructions:
      text: You are a helpful assistant.
    tools: []
    invokes: []
"""
