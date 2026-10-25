"""Optional ngn profile adapter over the framework's shared A2A runtime."""

from __future__ import annotations

import copy
from collections.abc import AsyncGenerator
from collections.abc import Callable
from contextlib import aclosing
from typing import TYPE_CHECKING

from nagents.channels.dispatcher import ChannelDispatcher
from nagents.events import Event
from nagents.harness.auth import OpenAIAuth
from nagents.harness.runtime import Harness
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskStarted

from .authorization import unavailable_credentials
from .executor import PublicStatus
from .executor import TaskExecutor
from .output_channel import A2AOutputChannel

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig

    from .agent import AgentRuntime
    from .authorization import AuthorizationRegistry
    from .authorization import AuthorizationSession
    from .authorization import CredentialHandler
    from .config import A2AConfig
    from .executor import Execution
    from .store import SQLiteTaskStore

HarnessFactory = Callable[["HarnessConfig", OpenAIAuth], Harness]


def build_harness(config: HarnessConfig, auth: OpenAIAuth) -> Harness:
    return Harness(config, shared_auth=auth)


class HarnessRuntime:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    async def run(self, prompt: str) -> AsyncGenerator[Event | PublicStatus, None]:
        from a2a.types import TaskState

        dispatcher = ChannelDispatcher((A2AOutputChannel(),), workspace=self.harness.workspace)
        with dispatcher.register_tools(self.harness.agent.tool_registry):
            async with aclosing(self.harness.run(prompt)) as events:
                async for event in events:
                    if isinstance(event, Event):
                        yield event
                    elif isinstance(event, TaskStarted):
                        yield PublicStatus(TaskState.TASK_STATE_WORKING, "Delegated work is running.")
                    elif isinstance(event, TaskCompleted):
                        yield PublicStatus(TaskState.TASK_STATE_WORKING, "Delegated work returned.")

    async def close(self) -> None:
        await self.harness.close()


class HarnessAgentExecutor(TaskExecutor):
    def __init__(
        self,
        config: A2AConfig,
        harness_config: HarnessConfig,
        store: SQLiteTaskStore,
        authorizations: AuthorizationRegistry,
        *,
        factory: HarnessFactory = build_harness,
        credential_handler: CredentialHandler = unavailable_credentials,
    ) -> None:
        super().__init__(config, store, authorizations, credential_handler=credential_handler)
        self.harness_config = copy.deepcopy(harness_config)
        self.harness_config.agent = config.agent
        self.harness_config.data_dir = config.data_dir / "harness"
        self.harness_config.validate()
        self.factory = factory
        # One refresh lock per auth store, shared by all root/child runtimes.
        self.auth = OpenAIAuth()

    def build_runtime(self, execution: Execution, session_id: str, authorization: AuthorizationSession) -> AgentRuntime:
        harness = self.factory(copy.deepcopy(self.harness_config), self.auth)
        harness.session_id = session_id
        harness.approval_handler = authorization.approve
        return HarnessRuntime(harness)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.auth.close()
