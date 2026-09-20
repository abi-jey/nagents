"""Definition-driven assembly over the existing Agent and task lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import aclosing
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import cast

from nagents.events import Event
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.extensions import AgentPlugin
from nagents.harness.config import PROVIDERS
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.credentials import ProviderLoginStore
from nagents.harness.runtime import Harness
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig
from nagents.observation import observe
from nagents.observation import scope
from nagents.provider import Provider
from nagents.provider.codex import CodexCredentials
from nagents.provider.codex import CodexProvider
from nagents.types import GenerationConfig

from .store import Recorder

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator

    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.harness.types import HarnessEvent
    from nagents.harness.types import TaskCompleted
    from nagents.harness.types import TaskMessage
    from nagents.types import ContentPart
    from nagents.types import Message
    from nagents.types import ToolDefinition

    from .schema import Design


class DesignProvider(Provider):
    def __init__(self, design: Design, name: str, recorder: Recorder, demo: bool) -> None:
        self.definition = design.providers[name]
        self.design = design
        self.recorder = recorder
        self.demo = demo
        definition = self.definition
        super().__init__(
            PROVIDERS[definition.type],
            "deferred",
            definition.model,
            base_url=definition.base_url or None,
            api=definition.api,
            api_version=definition.api_version or None,
        )

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        if self.demo:
            text = "Offline designer demo. Your configured instructions and tool schemas are visible in the context inspector. No model request was sent."
            yield TextChunkEvent(chunk=text)
            yield TextDoneEvent(text=text)
            return
        self.api_key = resolve_secret(
            self.design,
            self.definition.secret,
            self.recorder,
            provider=self.definition.type,
            base_url=self.definition.base_url,
        )
        source = cast("AsyncGenerator[Event, None]", super().generate(messages, tools, config, stream, verify_model))
        async with aclosing(source) as events:
            async for event in events:
                yield event


def resolve_secret(design: Design, name: str, recorder: Recorder, *, provider: str = "", base_url: str = "") -> str:
    reference = design.secrets[name]
    if reference.source == "env":
        value = os.environ.get(reference.name, "")
    else:
        if not provider or base_url or reference.name != provider:
            raise ValueError("Saved credentials require the matching provider's default endpoint")
        store = ProviderLoginStore()
        selection = store.selection()
        if selection is None or selection.base_url:
            raise ValueError("Saved credentials do not belong to this endpoint")
        value = store.key_for(provider)
    if not value:
        raise ValueError(f"Secret reference {name!r} is unavailable")
    recorder.secrets.add(value)
    return value


class ExecutionEvents(AgentPlugin):
    async def on_event(self, context: RunContext, event: Event) -> None:
        observe("agent_event", event=event)

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        observe("context_before_model", messages=request.messages)
        return request


class DesignedHarness(Harness):
    supports_child_custom_tools = True

    def load_project_instructions(self) -> None:
        """Definitions already own their explicit, resolved instructions."""

    @property
    def mode(self) -> str:
        # Definition IDs are names, not implicit coding/reviewer policy presets.
        return self._permission_ceiling

    def generation_config(self) -> GenerationConfig:
        settings = self.definition.generation
        fields = settings.model_fields_set
        return GenerationConfig(
            temperature=settings.temperature if "temperature" in fields else None,
            max_tokens=settings.max_tokens if "max_tokens" in fields else None,
            top_p=settings.top_p if "top_p" in fields else None,
            stop=settings.stop if "stop" in fields else None,
        )

    def __init__(
        self, config: HarnessConfig, design: Design, agent_id: str = "", recorder: Recorder | None = None
    ) -> None:
        self.design = design
        self.agent_id = agent_id or design.entrypoint
        if self.agent_id not in design.agents:
            raise ValueError(f"Unknown design agent: {self.agent_id}")
        self.definition = design.agents[self.agent_id]
        self.recorder = recorder or Recorder()
        self.mcp = MCPManager([])
        self._design_initialized = False
        provider_name = self.definition.provider or design.defaults.provider
        provider = design.providers[provider_name]
        profiles = {name: AgentProfile() for name in design.agents if name != "assistant"}
        configured = replace(
            config,
            agent=self.agent_id,
            profiles=profiles,
            plugins=(),
            auth="api-key",
            provider=provider.type,
            model=provider.model,
            base_url=provider.base_url,
            api=provider.api,
            api_version=provider.api_version,
            max_tool_rounds=self.definition.max_tool_rounds,
            max_subagent_depth=design.defaults.max_subagent_depth,
        )
        super().__init__(configured)
        self._initial_provider = self.agent.provider
        self.agent.provider = (
            CodexProvider(self.codex_credentials, model=provider.model)
            if provider.auth == "chatgpt" and not config.demo
            else DesignProvider(design, provider_name, self.recorder, config.demo)
        )
        self.agent.skill_discoverer = None
        self.agent.tool_registry.clear()
        self.agent.plugins.append(ExecutionEvents())
        for selected in self.definition.tools:
            name = selected.ref.removeprefix("builtin.")
            self.agent.register_tool(self.tools.builtins[name], name=name)
        self.tools.builtins.pop("delegate", None)
        if self.definition.invokes:
            self.tools.builtins["delegate"] = self.delegate
            self.agent.register_tool(self.delegate, description=self.delegation_description())
        self.refresh_instructions()

    def delegation_description(self) -> str:
        targets = "\n".join(f"{edge.agent}: {edge.description}" for edge in self.definition.invokes)
        return (
            "Delegate an independent task. Returns immediately; results arrive after your turn. Allowed targets:\n"
            + targets
        )

    async def codex_credentials(self) -> CodexCredentials:
        credentials = await self.openai_auth.credentials()
        self.recorder.secrets.update((credentials.access_token, credentials.account_id))
        return credentials

    async def delegate(self, prompt: str, agent: str) -> dict[str, str]:
        """Delegate a self-contained task to an allowed agent."""
        if agent not in {edge.agent for edge in self.definition.invokes}:
            raise ValueError("Delegation target is not connected to this agent")
        observe("delegation", target=agent, prompt=prompt)
        return await self.tasks.delegate(prompt, agent)

    def create_defined_child(self, name: str) -> Harness:
        if name not in self.design.agents:
            raise ValueError("Unknown agent definition")
        return DesignedHarness(self.config, self.design, name, self.recorder)

    def refresh_instructions(self) -> None:
        self.agent.system_prompt = self.definition.instructions.text
        if self.definition.invokes:
            self.agent.system_prompt += "\n\n" + self.delegation_description()

    async def initialize(self, *, create_session: bool = True) -> None:
        await super().initialize(create_session=create_session)
        if self._design_initialized:
            return
        try:
            for selection in self.definition.mcp:
                if self.config.demo:
                    continue
                if self.mode == "reviewer":
                    raise PermissionError("Read-only agents cannot start MCP subprocesses")
                server = self.design.mcp_servers[selection.server]
                await self.approve(
                    "mcp_connect",
                    {"server": selection.server},
                    "Start configured MCP subprocess",
                    json.dumps(server.model_dump(), indent=2),
                )
                env = dict(server.env)
                for key, ref in server.secrets.items():
                    env[key] = resolve_secret(self.design, ref, self.recorder)
                config = MCPServerConfig(selection.server, server.command, server.args, env, str(self.workspace))
                await self.mcp.add_server(config)
                available = {tool.name: tool for tool in await self.mcp.get_tool_definitions()}
                for selected in selection.tools:
                    qualified = f"mcp__{selection.server}__{selected.ref}"
                    if qualified not in available:
                        raise ValueError(f"MCP tool is unavailable: {qualified}")
                    tool = available[qualified]
                    if tool.func is not None:
                        self.agent.tool_registry.register(
                            tool.func,
                            name=tool.name,
                            description=tool.description,
                            parameters=tool.parameters,
                        )
            self._design_initialized = True
        except BaseException:
            await self.mcp.disconnect_all()
            raise

    async def _run(
        self,
        prompt: str | list[ContentPart],
        *,
        task_id: str = "",
        trigger: str = "human",
        notifications: tuple[TaskCompleted | TaskMessage, ...] = (),
    ) -> AsyncGenerator[HarnessEvent, None]:
        token = scope.set(
            {
                "agent_id": self.agent_id,
                "task_id": self._task_id,
                "activation": self._activation,
                "session_id": self.session_id,
            }
        )
        try:
            observe(
                "agent_started",
                instructions=self.agent.system_prompt,
                provider=self.config.provider,
                model=self.agent.provider.model,
            )
            async with aclosing(
                super()._run(prompt, task_id=task_id, trigger=trigger, notifications=notifications)
            ) as events:
                async for event in events:
                    yield event
        finally:
            observe("agent_finished")
            scope.reset(token)

    async def close(self) -> None:
        try:
            await super().close()
        finally:

            async def cleanup() -> None:
                try:
                    await self._initial_provider.close()
                finally:
                    await self.mcp.disconnect_all()

            task = asyncio.create_task(cleanup())
            from nagents._async import join_owned as _await_cleanup

            await _await_cleanup(task)
