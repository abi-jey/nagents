"""Native harness delegation through the real SDK client to a local A2A peer."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest

from nagents.a2a import client as a2a_client
from nagents.a2a.application import create_app
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote_profiles import RemoteProfile
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness
from tests.test_a2a_auth import AUDIENCE
from tests.test_a2a_auth import IssuerState
from tests.test_a2a_auth import access_claims
from tests.test_a2a_auth import local_issuer
from tests.test_a2a_auth import make_token
from tests.test_a2a_auth import signing_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.a2a.agent import AgentContext
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Harness and A2A SQLite fixtures require POSIX")


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    config_home.mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    config_home.chmod(0o700)
    data_home.chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


class DeterministicPeerProvider(Provider):
    def __init__(self, observed: list[str]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "fixture-only-key", "fixture-peer")
        self.observed = observed

    async def verify_model(self, force: bool = False) -> bool:
        del force
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        del tools, config, stream, verify_model
        self.observed.append(str(messages[-1].content))
        yield TextChunkEvent(chunk="Remote peer answer.")
        yield TextDoneEvent(text="Remote peer answer.")
        yield DoneEvent(final_text="Remote peer answer.")


@asynccontextmanager
async def peer_application(tmp_path: Path) -> AsyncIterator[tuple[object, str, list[str]]]:
    key = signing_key("RS256", "harness-peer")
    async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
        token = make_token(
            key,
            access_claims(
                issuer,
                aud=AUDIENCE,
                scope="ngn:access ngn:invoke ngn:read ngn:cancel",
                jti="harness-peer-token",
            ),
        )
        observed: list[str] = []
        config = A2AConfig(
            public_url="https://peer.example",
            agent="assistant",
            issuer=IssuerConfig(issuer=issuer, audience=AUDIENCE),
            data_dir=(tmp_path / "peer-state").resolve(),
            loopback_http=True,
        )

        def factory(context: AgentContext) -> Agent:
            return Agent(
                DeterministicPeerProvider(observed),
                SessionManager(context.data_dir / "sessions.db"),
                system_prompt="Deterministic A2A peer fixture.",
                streaming=True,
                compaction_strategy=None,
                save_tool_outputs=False,
            )

        app = create_app(config=config, agent_factory=factory)
        async with app.router.lifespan_context(app):
            yield app, token, observed


@pytest.mark.asyncio
async def test_configure_then_delegate_uses_remote_sdk_and_preserves_remote_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with peer_application(tmp_path) as (peer_app, token, observed):
        monkeypatch.setenv("NGN_TEST_REMOTE_TOKEN", token)
        actual_remote_agent = a2a_client.RemoteAgent

        def with_asgi_transport(
            url: str,
            tokens: object = None,
            **kwargs: object,
        ) -> a2a_client.RemoteAgent:
            kwargs["transport"] = httpx.ASGITransport(app=peer_app)  # type: ignore[arg-type]
            return actual_remote_agent(url, tokens, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(a2a_client, "RemoteAgent", with_asgi_transport)
        schemas: list[dict[str, object]] = []
        original_generate = FakeProvider.generate
        round_number = 0

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            nonlocal round_number
            round_number += 1
            if round_number == 1:
                yield ToolCallEvent(
                    id="configure-remote",
                    name="remote_agent_configure",
                    arguments={
                        "name": "peer",
                        "url": "https://peer.example",
                        "description": "Deterministic remote peer",
                        "token_env": "NGN_TEST_REMOTE_TOKEN",
                    },
                )
            elif round_number == 2:
                yield ToolCallEvent(
                    id="delegate-remote",
                    name="delegate",
                    arguments={"agent": "peer", "prompt": "Return the deterministic peer answer."},
                )
            else:
                yield TextDoneEvent(text="Remote work was delegated.")

        async def capture_generate(
            self: FakeProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            schemas.append({tool.name: dict(tool.parameters) for tool in tools or []})
            async for event in original_generate(self, messages, tools, config, stream, verify_model):
                yield event

        monkeypatch.setattr(FakeProvider, "generate", capture_generate)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def approve(request: object) -> bool:
            del request
            return True

        harness.approval_handler = approve
        try:
            await collect(harness, "Configure and use the peer in this conversation.")

            assert round_number >= 3
            assert len(schemas) >= 3
            delegate = schemas[1]["delegate"]
            assert isinstance(delegate, dict)
            properties = delegate["properties"]
            assert isinstance(properties, dict)
            agent = properties["agent"]
            assert isinstance(agent, dict)
            assert "peer" in agent["enum"]
            assert observed == ["Return the deterministic peer answer."]
            delegated = next(info for info in harness.tasks.list() if info.profile == "peer")
            worker = harness.tasks._workers[delegated.id]
            await worker
            child_history = await harness.tasks.history(delegated.id)
            assert delegated.status == "completed"
            assert any("Remote peer answer." in str(message.content) for message in child_history)
            assert sum(len(provider.requests) for provider in providers) == round_number
        finally:
            await harness.close()


@pytest.mark.asyncio
async def test_set_agent_runs_remote_without_local_provider_and_records_matching_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with peer_application(tmp_path) as (peer_app, token, observed):
        monkeypatch.setenv("NGN_TEST_REMOTE_TOKEN", token)
        actual_remote_agent = a2a_client.RemoteAgent

        def with_asgi_transport(url: str, tokens: object = None, **kwargs: object) -> a2a_client.RemoteAgent:
            kwargs["transport"] = httpx.ASGITransport(app=peer_app)  # type: ignore[arg-type]
            return actual_remote_agent(url, tokens, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(a2a_client, "RemoteAgent", with_asgi_transport)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider, messages
            yield TextDoneEvent(text="Local provider must not run")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        store = harness._agent_catalog.remote_store
        store.save(
            {
                "peer": AgentProfile(
                    description="Configured peer",
                    a2a=RemoteProfile(url="https://peer.example", token_env="NGN_TEST_REMOTE_TOKEN"),
                )
            },
            expected=store.load().revision,
        )
        assert harness.refresh_agent_definitions()
        try:
            await harness.set_agent("peer")
            events = await collect(harness, "Run as the peer.")
            completed = [event for event in events if isinstance(event, TextDoneEvent)]
            metadata = [event for event in events if isinstance(event, ResponseMetadataEvent)]

            assert len(completed) == 1
            assert completed[0].text == "Remote peer answer."
            assert len(metadata) == 1
            assert metadata[0].history_id
            assert metadata[0].response_id == completed[0].extra["response_id"]
            assert observed == ["Run as the peer."]
            assert sum(len(provider.requests) for provider in providers) == 0
        finally:
            await harness.close()
