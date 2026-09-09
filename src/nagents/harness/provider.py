"""Deferred credentials and a genuinely offline provider for the core run loop."""

import ast
import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from nagents.events import Event
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.exceptions import ModelListError
from nagents.extensions import CompactionResult
from nagents.provider import Provider
from nagents.types import Message

from .config import PROVIDERS

if TYPE_CHECKING:
    from nagents.extensions import CompactionRequest
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition

    from .config import HarnessConfig


class HarnessProvider(Provider):
    def __init__(self, config: "HarnessConfig") -> None:
        if config.api == "completions":
            raise ValueError(
                "The coding harness requires conversation roles and tools; the legacy completions API is text-only. "
                "Use api='chat_completions', 'responses', or 'messages', or use Provider directly with one text prompt."
            )
        self.harness_config = config
        super().__init__(
            provider_type=PROVIDERS[config.provider],
            api_key="deferred-until-live-request",
            model=config.model,
            base_url=config.base_url or None,
            api_version=config.api_version or None,
            api=config.api,
        )

    def credentials(self) -> None:
        if self.harness_config.demo:
            return
        key = os.environ.get(self.harness_config.api_key_env, "")
        if not key.strip():
            raise ValueError(
                f"Set {self.harness_config.api_key_env} before an API-key request, or use offline demo mode"
            )
        self.api_key = key

    async def verify_model(self, force: bool = False) -> bool:
        # Model-list endpoints are not supported by every compatible service.
        # The actual generation endpoint is authoritative; startup stays local.
        self.credentials()
        return True

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
        if prompt.startswith("BACKGROUND TASK NOTIFICATION:"):
            payload = json.loads(prompt.split("\n", 1)[1])
            tasks = payload.get("tasks", [])
            text = "## OFFLINE DEMO / background results\n\nThese are genuine asyncio jobs using scripted offline responses, not model calls.\n\n"
            text += "\n".join(f"- **{task['name']}**: {task['status']}." for task in tasks)
            text += "\n\nThe coordinator received the results and resumed through the normal agent loop."
            for offset in range(0, len(text), 48):
                await asyncio.sleep(0.01)
                yield TextChunkEvent(chunk=text[offset : offset + 48])
            yield TextDoneEvent(text=text)
            return
        if (
            prompt.lower().strip() == "demo subagents"
            and messages[-1].role != "tool"
            and any(tool.name == "delegate" for tool in tools or [])
        ):
            for topic in ("file layout", "available instructions", "documentation"):
                yield ToolCallEvent(
                    id=f"demo-{uuid.uuid4().hex[:12]}",
                    name="delegate",
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
                # Core persists tool results as Python literals, not JSON.
                result = ast.literal_eval(message.content)
            except (ValueError, SyntaxError):
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
