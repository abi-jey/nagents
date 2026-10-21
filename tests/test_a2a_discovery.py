"""A2A URL-only discovery and workspace configuration use advertised OIDC metadata."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

from nagents.a2a import discovery
from nagents.a2a.application import agent_card
from nagents.a2a.client import RemoteAgentError
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.remote_config import RemoteAgentConfig
from nagents.events import Event
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.a2a.discovery import RemoteDiscovery
    from nagents.types import Message


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Harness remote discovery test storage requires POSIX")


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


def card_config(
    tmp_path: Path, *, origin: str = "https://peer.example", public_client: bool = True, secret: bool = False
) -> A2AConfig:
    return A2AConfig(
        public_url=origin,
        agent="assistant",
        issuer=IssuerConfig(issuer="https://id.example/realms/agents", audience="peer-api"),
        data_dir=(tmp_path / "peer-state").resolve(),
        browser_client_id="ngn-public-cli" if public_client else "",
        browser_client_secret_env="NGN_BROWSER_CLIENT_SECRET" if secret else "",
        browser_scopes=("openid", "profile", "email"),
    )


def responses_for_card(
    card: object, *, metadata_url: str, issuer: str = "https://id.example/realms/agents"
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    card_json = MessageToDict(card)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=card_json, request=request)
        if request.url == httpx.URL(metadata_url):
            return httpx.Response(200, json={"issuer": issuer}, request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handle), seen


@pytest.mark.asyncio
async def test_discovery_uses_oidc_url_and_public_pkce_hint_without_sending_credentials(tmp_path: Path) -> None:
    metadata_url = "https://id.example/custom/oidc-document"
    card = agent_card(card_config(tmp_path))
    card.security_schemes["oidc"].open_id_connect_security_scheme.open_id_connect_url = metadata_url
    transport, seen = responses_for_card(card, metadata_url=metadata_url)

    result = await discovery.discover_remote(RemoteAgentConfig(url="https://peer.example"), transport=transport)

    assert result.name == "ngn"
    assert result.connection.rpc_url == "https://peer.example/rpc"
    assert result.connection.metadata_url == metadata_url
    assert result.connection.issuer == "https://id.example/realms/agents"
    assert result.connection.client_id == "ngn-public-cli"
    assert result.connection.scopes == ("openid", "profile", "email")
    assert result.authentication == "oauth"
    assert result.client_id_advertised
    assert [request.url for request in seen] == [
        httpx.URL("https://peer.example/.well-known/agent-card.json"),
        httpx.URL(metadata_url),
    ]
    assert all("authorization" not in request.headers for request in seen)
    assert all(request.url.scheme == "https" for request in seen)


@pytest.mark.asyncio
async def test_discovery_retains_host_setup_required_when_no_public_client_is_advertised(tmp_path: Path) -> None:
    metadata_url = "https://id.example/realms/agents/.well-known/openid-configuration"
    card = agent_card(card_config(tmp_path, public_client=False))
    transport, _ = responses_for_card(card, metadata_url=metadata_url)

    result = await discovery.discover_remote(RemoteAgentConfig(url="https://peer.example"), transport=transport)

    assert result.connection.issuer == "https://id.example/realms/agents"
    assert result.connection.client_id == ""
    assert not result.client_id_advertised
    assert result.authentication == "oauth"


@pytest.mark.asyncio
async def test_confidential_browser_client_is_not_published_as_a_public_hint(tmp_path: Path) -> None:
    metadata_url = "https://id.example/realms/agents/.well-known/openid-configuration"
    card = agent_card(card_config(tmp_path, secret=True))
    assert all(extension.uri != "urn:nagents:a2a:oauth-client:v1" for extension in card.capabilities.extensions)
    transport, _ = responses_for_card(card, metadata_url=metadata_url)

    result = await discovery.discover_remote(RemoteAgentConfig(url="https://peer.example"), transport=transport)

    assert result.connection.client_id == ""
    assert not result.client_id_advertised


@pytest.mark.asyncio
async def test_discovery_ignores_foreign_rpc_interfaces_and_never_contacts_them(tmp_path: Path) -> None:
    card = agent_card(card_config(tmp_path))
    card.supported_interfaces[0].url = "https://attacker.example/rpc"
    card_json = MessageToDict(card)
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=card_json, request=request)

    with pytest.raises(RemoteAgentError, match="one A2A"):
        await discovery.discover_remote(
            RemoteAgentConfig(url="https://peer.example"), transport=httpx.MockTransport(handle)
        )

    assert requests == ["https://peer.example/.well-known/agent-card.json"]


@pytest.mark.asyncio
async def test_ambiguous_same_origin_rpc_interfaces_require_operator_selection(tmp_path: Path) -> None:
    card = agent_card(card_config(tmp_path))
    card.supported_interfaces.add(url="https://peer.example/rpc-v2", protocol_binding="JSONRPC", protocol_version="1.0")
    transport, _ = responses_for_card(
        card, metadata_url="https://id.example/realms/agents/.well-known/openid-configuration"
    )

    with pytest.raises(RemoteAgentError, match="one A2A"):
        await discovery.discover_remote(RemoteAgentConfig(url="https://peer.example"), transport=transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_target", ["https://attacker.example/card", "https://attacker.example/metadata"])
async def test_discovery_does_not_follow_card_or_metadata_redirects(tmp_path: Path, redirect_target: str) -> None:
    metadata_url = "https://id.example/realms/agents/.well-known/openid-configuration"
    card = agent_card(card_config(tmp_path))
    card.security_schemes["oidc"].open_id_connect_security_scheme.open_id_connect_url = metadata_url
    card_json = MessageToDict(card)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/.well-known/agent-card.json" and redirect_target.endswith("/card"):
            return httpx.Response(302, headers={"Location": redirect_target}, request=request)
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=card_json, request=request)
        if request.url == httpx.URL(metadata_url):
            return httpx.Response(302, headers={"Location": redirect_target}, request=request)
        return httpx.Response(404, request=request)

    with pytest.raises(RemoteAgentError):
        await discovery.discover_remote(
            RemoteAgentConfig(url="https://peer.example"), transport=httpx.MockTransport(handle)
        )

    assert all(request.url.host != "attacker.example" for request in requests)


def test_non_https_remote_discovery_requires_explicit_loopback_development_mode() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteAgentConfig(url="http://peer.example")


@pytest.mark.asyncio
async def test_url_only_configure_tool_discovers_and_saves_host_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_url = "https://id.example/realms/agents/.well-known/openid-configuration"
    card = agent_card(card_config(tmp_path))
    transport, seen = responses_for_card(card, metadata_url=metadata_url)
    actual_discover = discovery.discover_remote

    async def discover_with_mock(connection: RemoteAgentConfig) -> RemoteDiscovery:
        return await actual_discover(connection, transport=transport)

    monkeypatch.setattr(discovery, "discover_remote", discover_with_mock)
    calls = 0

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        nonlocal calls
        del provider, messages
        calls += 1
        if calls == 1:
            yield ToolCallEvent(
                id="configure-url-only",
                name="remote_agent_configure",
                arguments={"name": "peer", "url": "https://peer.example"},
            )

    harness, _ = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: object) -> bool:
        del request
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Connect the remote agent by URL.")
        profile = harness._agent_catalog.remote_store.load().agents["peer"]
        assert profile.a2a.issuer == "https://id.example/realms/agents"
        assert profile.a2a.client_id == "ngn-public-cli"
        assert profile.a2a.metadata_url == metadata_url
        assert profile.a2a.scopes == ("openid", "profile", "email")
        assert profile.description == "An authenticated ngn agent."
        assert len(seen) == 2
        result = next(event.result for event in events if isinstance(event, ToolResultEvent))
        assert isinstance(result, dict)
        assert result["sign_in_required"] is True
        assert result["host_setup_required"] is False
    finally:
        await harness.close()
