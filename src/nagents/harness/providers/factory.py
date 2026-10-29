"""Deferred credentials and a genuinely offline provider for the core run loop."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.exceptions import ModelListError
from nagents.extensions import CompactionResult
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.reasoning import native_openai_api
from nagents.types import Message

from .model_bindings import ModelBinding
from .registry import PROVIDERS

if TYPE_CHECKING:
    from nagents.extensions import CompactionRequest
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition

    from ..config import HarnessConfig
    from .credentials import ProviderLoginStore


class UnavailableModelProvider(Provider):
    """A repairable model reference with no credentials or usable transport."""

    def __init__(self, binding: ModelBinding) -> None:
        self.binding = binding
        self.reason = (
            f"Model connection {binding.provider!r} is unavailable in this workspace. "
            "Choose an available connection and model in Settings."
        )
        # Every request surface below is local. The inert loopback origin also
        # prevents an inherited transport helper reaching a fallback provider.
        super().__init__(
            ProviderType.OPENAI_COMPATIBLE,
            model=binding.id,
            api_key="unavailable-model-no-transport",
            base_url="http://127.0.0.1:0",
        )

    async def verify_model(self, force: bool = False) -> bool:
        raise ValueError(self.reason)

    async def get_model_list(self) -> list[str]:
        raise ModelListError(self.reason)

    async def _fetch_context_metadata(self) -> None:
        return

    async def generate(
        self,
        messages: list[Message],
        tools: list["ToolDefinition"] | None = None,
        config: "GenerationConfig | None" = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        yield ErrorEvent(message=self.reason, code="MODEL_CONNECTION_UNAVAILABLE", recoverable=False)


class HarnessProvider(Provider):
    def __init__(
        self,
        config: "HarnessConfig",
        login_store: "ProviderLoginStore | None" = None,
        *,
        request_timeout: float = 120.0,
    ) -> None:
        profile = config.provider_profile()
        self.harness_config = config
        # A saved ngn login supplies the key only when the environment does not.
        self.login_store = login_store
        # These GPT-6 models use Responses for tool calls. Respect an explicitly selected
        # contract or compatible endpoint rather than rewriting custom routing.
        self._automatic_model_api = (
            profile.kind in {"openai", "openai_compatible"} and profile.api == "auto" and not profile.base_url
        )
        # Resolve auto before constructing the HTTP client so later model
        # changes keep the same bounded transport used by explicit Responses.
        api = profile.api
        if self._automatic_model_api:
            api = native_openai_api(config.model)
        super().__init__(
            provider_type=PROVIDERS[config.provider_profile().kind],
            api_key="deferred-until-live-request",
            model=config.model,
            base_url=config.provider_profile().base_url or None,
            api_version=config.provider_profile().api_version or None,
            api=api,
            timeout=request_timeout,
            session_id_header=profile.session_id_header,
        )
        self.configure_context_window(profile.context_window, profile.model_context_windows)

    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str) -> None:
        self._model = value
        # Harness profiles, settings and model selection all assign this public
        # attribute. Keep only the default OpenAI auto contract model-aware;
        # explicit APIs and compatible endpoints retain their chosen contract.
        if self._automatic_model_api:
            self.api = native_openai_api(value)

    def credentials(self) -> None:
        if self.harness_config.demo:
            return
        key = os.environ.get(self.harness_config.provider_profile().key_env, "")
        if not key.strip() and self.login_store is not None:
            key = self.login_store.key_for(self.harness_config.provider_profile().kind)
        if not key.strip():
            raise ValueError(
                f"Set {self.harness_config.provider_profile().key_env}, sign in with ngn login, or use offline demo mode "
                "before an API-key request"
            )
        self.api_key = key

    async def verify_model(self, force: bool = False) -> bool:
        # Model-list endpoints are not supported by every compatible service.
        # The actual generation endpoint is authoritative; startup stays local.
        self.credentials()
        return True

    def _prepare_context_usage(self) -> None:
        if not self.harness_config.demo:
            self.credentials()

    async def _fetch_context_metadata(self) -> None:
        if not self.harness_config.demo:
            await super()._fetch_context_metadata()

    async def get_model_list(self) -> list[str]:
        if self.harness_config.demo:
            raise NotImplementedError("Model discovery is unavailable in offline demo mode; enter a model ID manually.")
        try:
            self.credentials()
        except Exception:
            raise ModelListError(
                "API-key credentials are unavailable; configure your provider key before discovery."
            ) from None
        return await super().get_model_list()

    async def generate(
        self,
        messages: list[Message],
        tools: list["ToolDefinition"] | None = None,
        config: "GenerationConfig | None" = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        if not self.harness_config.demo:
            self.credentials()
            async for event in super().generate(messages, tools, config, stream, verify_model):
                yield event
            return

        prompt = next((str(message.content) for message in reversed(messages) if message.role == "user"), "")
        if prompt.startswith("BACKGROUND AGENT UPDATES:"):
            payload = json.loads(prompt.split("\n", 1)[1])
            tasks = payload.get("sessions", [])
            text = "## OFFLINE DEMO / background results\n\nThese are genuine asyncio jobs using scripted offline responses, not model calls.\n\n"
            text += "\n".join(f"- **{task['session_id']}**: {task['status']}." for task in tasks)
            text += "\n\nThe coordinator received the results and resumed through the normal agent loop."
            for offset in range(0, len(text), 48):
                await asyncio.sleep(0.01)
                yield TextChunkEvent(chunk=text[offset : offset + 48])
            yield TextDoneEvent(text=text)
            return
        if (
            prompt.lower().strip() == "demo subagents"
            and messages[-1].role != "tool"
            and any(tool.name == "to_agent" for tool in tools or [])
        ):
            for topic in ("file layout", "available instructions", "documentation"):
                yield ToolCallEvent(
                    id=f"demo-{uuid.uuid4().hex[:12]}",
                    name="to_agent",
                    arguments={"prompt": f"Offline reviewer: inspect {topic}. Do not modify anything."},
                )
            return
        if messages and messages[-1].role != "tool":
            await asyncio.sleep(0.02)
            yield ToolCallEvent(id=f"demo-{uuid.uuid4().hex[:12]}", name="list_files", arguments={"limit": 12})
            if "demo approval" in prompt.lower():
                yield ToolCallEvent(id=f"demo-{uuid.uuid4().hex[:12]}", name="demo_preview", arguments={})
            return

        files: list[str] = []
        approval = ""
        for message in reversed(messages):
            if message.role == "user":
                break
            if message.role != "tool" or not isinstance(message.content, str):
                continue
            try:
                result = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict) and message.name == "list_files":
                files = [str(path) for path in result.get("paths", [])]
            if isinstance(result, dict) and message.name == "demo_preview":
                decision = "approved" if result.get("approved") else "declined"
                approval = f"\n### Approval Preview\nThe sample diff was **{decision}**. This was preview only; no file was written.\n"
        listing = (
            "\n".join(f"- `{path.replace('`', '')}`" for path in files)
            or "No visible paths were returned by the local listing tool."
        )
        text = (
            "## OFFLINE DEMO\n\nNo model or network request was made. This response is a scripted demonstration, not a live coding analysis.\n\n"
            f"### Local Workspace\n{listing}\n{approval}\n"
            "### Try the Coding Loop\n"
            "1. Ask a live build agent to inspect a file and propose one focused change.\n"
            "2. Review the exact diff before approving it. Stale reads are rejected.\n"
            "3. Approve a test command separately. Local shell commands are not sandboxed.\n\n"
            "In this demo, try **demo approval**, switch to **reviewer**, or create a new session and resume this one. "
            "Conversation history is stored locally; workspace writes and shell execution are disabled.\n"
        )
        for offset in range(0, len(text), 48):
            await asyncio.sleep(0.01)
            yield TextChunkEvent(chunk=text[offset : offset + 48])
        yield TextDoneEvent(text=text)


class DemoCompaction:
    async def should_compact(self, request: "CompactionRequest") -> bool:
        return False

    async def compact(self, request: "CompactionRequest") -> CompactionResult:
        prompts = [str(message.content)[:300] for message in request.messages if message.role == "user"]
        summary = "OFFLINE DEMO: local, lossy summary of user prompts; no model was called.\n" + "\n".join(
            prompts[-10:]
        )
        return CompactionResult(messages=[Message(role="compaction_summary", content=summary)], summary=summary)
