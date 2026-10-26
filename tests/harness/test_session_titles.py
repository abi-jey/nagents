"""Renaming selects only human text and a small model on the accepted connection."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness.agents.updates import NOTIFICATION_PREFIX
from nagents.harness.agents.updates import WAKEUP_PREFIX
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.factory import HarnessProvider
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.session_titles import small_model
from nagents.harness.session_titles import user_messages
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import OpenAIProvider
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from tests.support.titles import title_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition

pytestmark = pytest.mark.usefixtures("workspace_discovery_only")


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (["gpt-6.1-sol", "gpt-5.6-luna", "gpt-6-luna"], "gpt-6-luna"),
        (["gpt-6-mini-preview", "gpt-5-mini"], "gpt-5-mini"),
        (["gpt-4o-mini-tts", "text-embedding-small", "anthropic/claude-haiku-4-5"], "anthropic/claude-haiku-4-5"),
        (["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite"], "models/gemini-2.5-flash-lite"),
        (["deepseek/deepseek-v4.1-flash"], "deepseek/deepseek-v4.1-flash"),
    ],
)
def test_small_model_is_selected_from_the_advertised_text_candidates(models: list[str], expected: str) -> None:
    assert small_model(models) == expected


def test_generated_rename_uses_last_ten_users_across_compaction_without_changing_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness = Harness(
            HarnessConfig(
                workspace=tmp_path,
                data_dir=tmp_path / "state",
                demo=True,
                providers={
                    "": ProviderProfile(
                        kind="openai_compatible",
                        base_url="https://provider.example/v1",
                        api="chat_completions",
                        api_key_env="TITLE_TEST_KEY",
                    )
                },
            )
        )
        await harness.initialize()
        stub = title_model(monkeypatch)
        try:
            session = harness.agent.session
            root = harness.session_id
            other = await session.get_or_create_session("foreign", "harness")
            await session.add_message(other, Message(role="user", content="Foreign user input"))
            for index in range(12):
                await session.add_message(root, Message(role="user", content=f"User request {index}"))
                await session.add_message(root, Message(role="assistant", content=f"Agent answer {index}"))
                await session.add_message(root, Message(role="tool", content="Tool result", tool_call_id="tool"))
            await session.replace_context(
                root,
                [
                    Message(role="compaction_summary", content="Agent summary"),
                    *(Message(role="user", content=f"User request {index}") for index in range(8, 12)),
                    Message(role="user", content="Synthetic user context invented by the compactor"),
                ],
            )
            await session.add_message(root, Message(role="user", content=NOTIFICATION_PREFIX + "Child result"))
            await session.add_message(root, Message(role="user", content=WAKEUP_PREFIX + "Timer reason"))
            await session.add_message(
                root,
                Message(
                    role="user",
                    content=[
                        TextContent(text="Latest user request"),
                        ImageContent(base64_data="hidden", media_type="image/png"),
                    ],
                ),
            )
            harness.config.demo = False
            original = harness.agent.provider
            original_model = original.model
            before = await harness.history()
            assert await harness.rename_session() == stub.reply
            assert harness.agent.provider is original and original.model == original_model
            assert await harness.history() == before
            assert len(stub.requests) == 1
            request = stub.requests[0]
            assert request.provider is not original and request.provider.model == "gpt-6-luna"
            assert request.provider.base_url == original.base_url
            assert request.provider.api == original.api == "chat_completions"
            assert request.provider.harness_config.provider_profile().key_env == "TITLE_TEST_KEY"
            assert request.provider in stub.closed
            assert request.tools == [] and request.stream is False
            assert [message.role for message in request.messages] == ["system", "user"]
            assert json.loads(str(request.messages[1].content)) == {
                "user_messages": [*(f"User request {index}" for index in range(3, 12)), "Latest user request"]
            }
            assert next(item for item in await harness.list_sessions() if item.id == root).title == stub.reply
            await harness.rename_session("Explicit title")
            assert len(stub.requests) == 1
            target = await harness.fork_session()
            assert await user_messages(session, target) == [
                *(f"User request {index}" for index in range(3, 12)),
                "Latest user request",
            ]
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("subscription", [True, False])
def test_codex_title_request_preserves_accepted_credentials_and_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subscription: bool
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        await harness.initialize()

        async def credentials() -> CodexCredentials:
            raise AssertionError("No real credential lookup is needed for this transport test")

        active = (
            OpenAIProvider(credentials, model="gpt-6.1-sol")
            if subscription
            else OpenAIProvider(
                api_key="test-only-key", base_url="https://accepted.example/v1", model="gpt-6.1-sol", api="responses"
            )
        )
        await harness.agent.provider.close()
        harness.agent.provider = active

        async def catalog(provider: OpenAIProvider) -> list[str]:
            assert provider is not active
            assert provider.uses_chatgpt_auth == subscription
            assert provider.base_url == active.base_url
            if subscription:
                assert provider._credentials is active._credentials
            else:
                assert provider.api_key == active.api_key and provider.api == active.api
            return ["gpt-6-luna"]

        async def generate(
            provider: OpenAIProvider,
            messages: list[Message],
            *,
            tools: list[ToolDefinition],
            config: GenerationConfig,
            stream: bool,
        ) -> AsyncGenerator[Event, None]:
            assert provider.model == "gpt-6-luna" and tools == []
            yield TextDoneEvent(text="Preserve accepted provider")

        monkeypatch.setattr(OpenAIProvider, "get_model_list", catalog)
        monkeypatch.setattr(OpenAIProvider, "generate", generate)
        harness.config.demo = False
        try:
            await harness.agent.session.add_message(harness.session_id, Message(role="user", content="A user request"))
            assert await harness.rename_session() == "Preserve accepted provider"
            assert active.model == "gpt-6.1-sol"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_cancelled_title_request_closes_auxiliary_provider_and_keeps_saved_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        await harness.initialize()
        stub = title_model(monkeypatch)
        try:
            await harness.rename_session("Keep explicit name")
            await harness.agent.session.add_message(harness.session_id, Message(role="user", content="A request"))
            started, stopped = asyncio.Event(), asyncio.Event()

            async def generate(
                provider: HarnessProvider,
                messages: list[Message],
                *,
                tools: list[ToolDefinition],
                config: GenerationConfig,
                stream: bool,
            ) -> AsyncGenerator[Event, None]:
                try:
                    started.set()
                    await asyncio.Event().wait()
                    yield TextDoneEvent(text="Never completed")
                finally:
                    stopped.set()

            monkeypatch.setattr(HarnessProvider, "generate", generate)
            harness.config.demo = False
            pending = asyncio.create_task(harness.rename_session())
            await asyncio.wait_for(started.wait(), 3)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert stopped.is_set() and len(stub.closed) == 1
            assert not harness._busy
            assert next(item for item in await harness.list_sessions() if item.id == harness.session_id).title == (
                "Keep explicit name"
            )
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_bounded_user_input_and_failed_generation_preserve_saved_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        await harness.initialize()
        stub = title_model(monkeypatch)
        try:
            await harness.rename_session("Keep this title")
            await harness.agent.session.add_message(harness.session_id, Message(role="user", content="x" * 10000))
            assert await user_messages(harness.agent.session, harness.session_id) == ["x" * 2000]
            harness.config.demo = False
            stub.models = ("large-model", "text-embedding-small", "gpt-4o-mini-tts")
            with pytest.raises(ValueError, match="no small text model"):
                await harness.rename_session()
            assert not stub.requests and len(stub.closed) == 1
            stub.models = ("gpt-6-luna",)
            stub.reply = ""
            with pytest.raises(ValueError, match="empty title"):
                await harness.rename_session()
            assert next(item for item in await harness.list_sessions() if item.id == harness.session_id).title == (
                "Keep this title"
            )
        finally:
            await harness.close()

    asyncio.run(scenario())
