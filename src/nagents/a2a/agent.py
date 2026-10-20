"""Direct framework Agent integration, independent of ngn configuration."""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import AsyncGenerator
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol

from nagents.agent import Agent
from nagents.tools import ToolExecutor

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.events import Event
    from nagents.events import ToolResultEvent
    from nagents.types import ToolCall

    from .auth import Principal
    from .authorization import AuthorizationSession
    from .executor import PublicStatus


@dataclass(frozen=True)
class AgentContext:
    """Server-issued execution identity passed only to a trusted agent factory.

    Use ``session_id`` and ``principal.user_name`` for Agent.run routing; never
    replace these with message metadata. The factory supplies ordinary nagents
    providers, instructions, plugins, tools and durable SessionManager storage.
    Return a fresh Agent owned by this execution; it is closed on termination.
    """

    profile: str
    task_id: str
    context_id: str
    session_id: str
    principal: Principal
    data_dir: Path


AgentFactory = Callable[[AgentContext], Agent]


class AgentRuntime(Protocol):
    def run(self, prompt: str) -> AsyncGenerator[Event | PublicStatus, None]: ...

    async def close(self) -> None: ...


class ApprovedToolExecutor(ToolExecutor):
    """Additional remote approval, preserving the host's existing executor.

    Host-declared read-only tools may skip this extra human gate. The caller
    cannot change that policy. Even approved operations still pass through the
    application's original ToolExecutor and its own permission checks.
    """

    def __init__(self, agent: Agent, authorization: AuthorizationSession, read_only_tools: frozenset[str]) -> None:
        super().__init__(agent.tool_registry)
        self.agent = agent
        self.inner = agent.tool_executor
        self.authorization = authorization
        self.read_only_tools = read_only_tools

    async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
        call = copy.deepcopy(tool_call)
        tool = self.agent.tool_registry.get(call.name)
        if tool is None or tool.func is None:
            raise PermissionError("The requested tool is unavailable")
        function, parameters, description = tool.func, copy.deepcopy(tool.parameters), tool.description
        inspect.signature(function).bind(**call.arguments)
        json.dumps(call.arguments, allow_nan=False)
        if call.name not in self.read_only_tools and not await self.authorization.action(
            call.name, call.arguments, description
        ):
            if self.authorization.failure is not None:
                raise self.authorization.failure
            raise PermissionError("The pending operation was not approved")
        if self.agent.tool_registry.get(call.name) is not tool or (
            tool.func is not function or tool.parameters != parameters or tool.description != description
        ):
            raise PermissionError("Tool definition changed; a fresh request and approval are required")
        # Preserve the embedding application's custom executor and its rules.
        return await self.inner.execute(call)


class NativeAgentRuntime:
    def __init__(
        self,
        agent: Agent,
        context: AgentContext,
        authorization: AuthorizationSession,
        read_only_tools: frozenset[str],
    ) -> None:
        self.agent = agent
        self.context = context
        agent.tool_executor = ApprovedToolExecutor(agent, authorization, read_only_tools)

    async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
        async with aclosing(
            self.agent.run(prompt, session_id=self.context.session_id, user_id=self.context.principal.user_name)
        ) as events:
            async for event in events:
                yield event

    async def close(self) -> None:
        await self.agent.close()
