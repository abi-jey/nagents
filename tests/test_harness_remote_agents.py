"""Workspace-owned remote-agent configuration and generation refresh."""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.harness.agent_definitions import discover_agents
from nagents.harness.config import AgentProfile
from nagents.harness.remote_credentials import RemoteCredentials
from nagents.harness.remote_profiles import RemoteProfile
from nagents.harness.remote_store import RemoteAgentStore
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Remote-agent private JSON storage requires POSIX")


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


def remote_profile(*, description: str = "Remote review") -> AgentProfile:
    return AgentProfile(
        description=description,
        a2a=RemoteProfile(
            url="https://peer.example",
            rpc_url="https://peer.example/rpc",
            token_env="NGN_PEER_TOKEN",
            ca_file="/etc/ngn/peer-ca.pem",
            scopes=("openid", "ngn:invoke"),
            timeout=45,
        ),
    )


@pytest.mark.parametrize("unsafe_cache", ["symlink", "invalid_json"])
def test_unavailable_presentation_cache_preserves_remote_connections(tmp_path: Path, unsafe_cache: str) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    profile = remote_profile()
    initial = store.save({"peer": profile}, expected=store.load().revision)
    cache = store.metadata_store.path
    if unsafe_cache == "symlink":
        outside = tmp_path / "untrusted-metadata"
        outside.write_text("must never be read")
        cache.symlink_to(outside)
    else:
        cache.write_text("invalid metadata")
        cache.chmod(0o600)

    loaded = store.load()
    assert loaded.revision == initial.revision
    assert loaded.agents["peer"].a2a.fingerprint == profile.a2a.fingerprint
    assert loaded.agents["peer"].description == profile.description
    assert loaded.advertised == {}


def test_remote_store_round_trips_json_and_rejects_stale_revision(tmp_path: Path) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    initial = store.load()
    assert initial.agents == {}
    assert initial.revision == "0" * 64

    saved = store.save({"peer-review": remote_profile()}, expected=initial.revision)

    assert saved.revision != initial.revision
    assert saved.agents["peer-review"].a2a.url == "https://peer.example"
    assert saved.agents["peer-review"].a2a.token_env == "NGN_PEER_TOKEN"
    assert (
        json.loads(store.path.read_text(encoding="utf-8"))["agents"]["peer-review"]["connection"]["token_env"]
        == "NGN_PEER_TOKEN"
    )
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert "token_env" in document["agents"]["peer-review"]["connection"]
    assert "access_token" not in document["agents"]["peer-review"]["connection"]
    with pytest.raises(ValueError, match="changed"):
        store.save({"peer-review": remote_profile(description="stale")}, expected=initial.revision)
    assert store.load() == saved


def test_agent_card_metadata_is_separate_from_local_alias_description_and_trust(tmp_path: Path) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    profile = remote_profile(description="Local editorial description")
    saved = store.save(
        {"stable-alias": profile}, expected=store.load().revision, trust_updates=(("stable-alias", True),)
    )

    assert store.update_metadata(profile.a2a.fingerprint, "Advertised display name", "Card description")
    observed = store.load()

    assert observed.revision == saved.revision
    assert tuple(observed.agents) == ("stable-alias",)
    assert observed.agents["stable-alias"].description == "Local editorial description"
    assert observed.agents["stable-alias"].a2a.fingerprint == profile.a2a.fingerprint
    assert observed.trusted_connections == saved.trusted_connections
    assert observed.advertised["stable-alias"].name == "Advertised display name"
    assert observed.advertised["stable-alias"].description == "Card description"

    shared = remote_profile(description="Shared endpoint")
    before_aliases = store.load()
    store.save({"stable-alias": profile, "second-alias": shared}, expected=before_aliases.revision)
    assert store.update_metadata(profile.a2a.fingerprint, "Shared card", "Shared description")
    shared_metadata = store.load().advertised
    assert shared_metadata["stable-alias"].name == "Shared card"
    assert shared_metadata["second-alias"].name == "Shared card"

    changed = AgentProfile(description="Local editorial description", a2a=RemoteProfile(url="https://other.example"))
    store.save({"stable-alias": changed, "second-alias": shared}, expected=store.load().revision)
    assert store.update_metadata(profile.a2a.fingerprint, "Fresh card", "Fresh description")
    after_binding_change = store.load()
    assert "stable-alias" not in after_binding_change.advertised
    assert after_binding_change.advertised["second-alias"].name == "Fresh card"
    assert changed.a2a.fingerprint not in after_binding_change.trusted_connections

    store.save({"stable-alias": changed}, expected=after_binding_change.revision)
    no_matching_alias = store.load()
    assert tuple(no_matching_alias.agents) == ("stable-alias",)
    assert not no_matching_alias.advertised
    assert not no_matching_alias.trusted_connections
    assert not store.update_metadata(profile.a2a.fingerprint, "Stale card", "Stale description")


@pytest.mark.asyncio
async def test_delegate_catalog_uses_card_labels_without_replacing_local_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="ready")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    store = harness._agent_catalog.remote_store
    profile = remote_profile(description="Local team note")
    store.save({"peer-alias": profile}, expected=store.load().revision)
    store.update_metadata(profile.a2a.fingerprint, "Published Agent Card", "Card purpose")
    assert harness.refresh_agent_definitions()
    try:
        catalog = harness.delegation_catalog()
        assert (
            "peer-alias: Local team note; untrusted remote AgentCard metadata "
            "name='Published Agent Card' description='Card purpose'"
        ) in catalog
        assert "peer-alias" in harness.config.profile_names
        assert harness.config.profile("peer-alias").description == "Local team note"
        current = store.load()
        store.save({"peer-alias": remote_profile(description="")}, expected=current.revision)
        assert harness.refresh_agent_definitions()
        assert (
            "peer-alias: untrusted remote AgentCard metadata name='Published Agent Card' description='Card purpose'"
        ) in harness.delegation_catalog()
    finally:
        await harness.close()


def test_remote_tool_trust_is_connection_bound_and_preserved_only_for_same_binding(tmp_path: Path) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    profile = remote_profile()
    initial = store.load()
    trusted = store.save({"peer-review": profile}, expected=initial.revision, trust_updates=(("peer-review", True),))
    fingerprint = profile.a2a.fingerprint
    assert trusted.trusted_connections == frozenset({fingerprint})
    assert json.loads(store.path.read_text(encoding="utf-8"))["agents"]["peer-review"]["trust_remote_tools"] is True
    isolated_workspace = RemoteAgentStore(tmp_path / "other-workspace" / "connections" / "a2a.json")
    assert isolated_workspace.load().trusted_connections == frozenset()

    edited_description = AgentProfile(description="Updated description", a2a=profile.a2a)
    preserved = store.save({"peer-review": edited_description}, expected=trusted.revision)
    assert preserved.trusted_connections == frozenset({fingerprint})

    changed_binding = AgentProfile(description="Updated endpoint", a2a=RemoteProfile(url="https://other.example"))
    revoked_by_binding_change = store.save({"peer-review": changed_binding}, expected=preserved.revision)
    assert revoked_by_binding_change.trusted_connections == frozenset()
    assert json.loads(store.path.read_text(encoding="utf-8"))["agents"]["peer-review"]["trust_remote_tools"] is False


def test_oauth_credentials_are_shared_but_tool_trust_stays_in_each_workspace(tmp_path: Path) -> None:
    connection = RemoteProfile(
        url="https://peer.example",
        issuer="https://issuer.example",
        client_id="public-client",
        scopes=("openid", "ngn:invoke"),
        timeout=45,
    )
    workspace_a = RemoteAgentStore(tmp_path / "workspace-a" / "settings" / "connections" / "a2a.json")
    workspace_b = RemoteAgentStore(tmp_path / "workspace-b" / "settings" / "connections" / "a2a.json")
    first_agent = AgentProfile(description="Workspace A name", a2a=connection)
    second_agent = AgentProfile(description="A different workspace name", a2a=connection)
    revision_a = workspace_a.load().revision
    trusted_a = workspace_a.save({"review": first_agent}, expected=revision_a, trust_updates=(("review", True),))
    revision_b = workspace_b.load().revision
    workspace_b.save({"audit": second_agent}, expected=revision_b)

    grant_a = RemoteCredentials(connection)
    grant_a.save(
        "cross-workspace-access",
        4102444800.0,
        refresh_token="cross-workspace-refresh",
        token_endpoint="https://issuer.example/token",
    )
    grant_b_after_restart = RemoteCredentials(connection)

    assert grant_a.path == grant_b_after_restart.path
    assert asyncio.run(grant_b_after_restart.token()) == "cross-workspace-access"
    assert trusted_a.trusted_connections == frozenset({connection.fingerprint})
    assert workspace_b.load().trusted_connections == frozenset()
    assert json.loads(workspace_b.path.read_text(encoding="utf-8"))["agents"]["audit"]["trust_remote_tools"] is False


def test_remote_tool_trust_updates_use_revision_cas_and_strict_booleans(tmp_path: Path) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    profile = remote_profile()
    initial = store.save({"peer-review": profile}, expected=store.load().revision)
    with pytest.raises(ValueError, match="boolean"):
        store.save(
            {"peer-review": profile},
            expected=initial.revision,
            trust_updates=(("peer-review", 1),),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="changed"):
        store.save(
            {"peer-review": profile},
            expected="0" * 64,
            trust_updates=(("peer-review", True),),
        )
    assert store.load() == initial


@pytest.mark.parametrize(
    "document",
    [
        "{not-json",
        json.dumps({"version": 2, "agents": {}}),
        json.dumps({"version": 1, "agents": {}, "unexpected": True}),
        json.dumps({"version": 1, "agents": {"assistant": {"connection": {"url": "https://peer.example"}}}}),
        json.dumps(
            {"version": 1, "agents": {"peer": {"connection": {"url": "https://peer.example", "token": "ignored"}}}}
        ),
        json.dumps(
            {
                "version": 1,
                "agents": {"peer": {"connection": {"url": "https://peer.example"}, "trust_remote_tools": 1}},
            }
        ),
        json.dumps({"version": 1, "agents": {"peer": {"connection": {"token_env": "NGN_PEER_TOKEN"}}}}),
        json.dumps({"version": 1, "agents": {"peer": {"connection": {"url": "https://peer.example/path"}}}}),
    ],
)
def test_remote_store_rejects_malformed_unknown_and_unconfigured_profiles(tmp_path: Path, document: str) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    store.save({"peer": remote_profile()}, expected=store.load().revision)
    store.path.write_text(document, encoding="utf-8")
    store.path.chmod(0o600)

    with pytest.raises(ValueError, match="invalid"):
        store.load()


def test_remote_store_rejects_local_profile_overrides_and_bad_connection_fields(tmp_path: Path) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    invalid = [
        AgentProfile(model="local-model", a2a=RemoteProfile(url="https://peer.example")),
        AgentProfile(prompt="local prompt", prompt_present=True, a2a=RemoteProfile(url="https://peer.example")),
        AgentProfile(tools=("shell",), a2a=RemoteProfile(url="https://peer.example")),
    ]
    for profile in invalid:
        with pytest.raises(ValueError):
            store.save({"peer": profile}, expected=store.load().revision)

    for connection in (
        {"url": "http://peer.example"},
        {"url": "https://peer.example", "rpc_url": "https://other.example/rpc"},
        {"url": "https://peer.example", "token_env": "Bearer secret"},
        {"url": "https://peer.example", "timeout": True},
    ):
        store.path.write_text(
            json.dumps({"version": 1, "agents": {"peer": {"connection": connection}}}), encoding="utf-8"
        )
        store.path.chmod(0o600)
        with pytest.raises(ValueError, match="invalid"):
            store.load()


def test_remote_store_accepts_issuer_only_for_host_discovery(tmp_path: Path) -> None:
    store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
    profile = AgentProfile(a2a=RemoteProfile(url="https://peer.example", issuer="https://issuer.example"))

    saved = store.save({"peer": profile}, expected=store.load().revision)
    loaded = store.load()

    assert loaded.revision == saved.revision
    assert loaded.agents["peer"].a2a is not None
    assert loaded.agents["peer"].a2a.issuer == "https://issuer.example"
    assert not loaded.agents["peer"].a2a.client_id


def test_native_markdown_does_not_accept_a2a_configuration(tmp_path: Path) -> None:
    path = tmp_path / ".ngn" / "agents" / "peer.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndescription: Remote peer\na2a:\n  url: https://peer.example\n---\n", encoding="utf-8")

    result = discover_agents(tmp_path)

    assert result.profiles == ()
    assert len(result.diagnostics) == 1
    assert "unsupported native field" in result.diagnostics[0]


@pytest.mark.asyncio
async def test_local_name_collision_keeps_last_good_remote_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        store = harness._agent_catalog.remote_store
        store.save({"peer": remote_profile()}, expected=store.load().revision)
        assert harness.refresh_agent_definitions()
        assert harness.config.profile("peer").a2a.configured

        path = harness.provider_store.agent_models.directory / "peer.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\ndescription: Local collision\n---\n", encoding="utf-8")

        assert not harness.refresh_agent_definitions()
        assert harness.config.profile("peer").description == "Remote review"
        assert harness.config.profile("peer").source_format == "a2a"
        assert any("remote agents could not be loaded" in item.lower() for item in harness.config.agent_diagnostics)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_canonical_remote_save_appears_in_delegate_enum_at_next_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas: list[dict[str, object]] = []
    original_generate = FakeProvider.generate

    async def capture_generate(
        self: FakeProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        schemas.append({tool.name: deepcopy(tool.parameters) for tool in tools or []})
        async for event in original_generate(self, messages, tools, config, stream, verify_model):
            yield event

    monkeypatch.setattr(FakeProvider, "generate", capture_generate)
    calls = 0
    harness_ref: list[object] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        nonlocal calls
        del provider, messages
        calls += 1
        if calls == 1:
            harness = harness_ref[0]
            store = harness._agent_catalog.remote_store  # type: ignore[attr-defined]
            store.save({"peer": remote_profile()}, expected=store.load().revision)
            assert harness.refresh_agent_definitions()  # type: ignore[attr-defined]
        yield TextDoneEvent(text="turn finished")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness_ref.append(harness)
    try:
        await collect(harness, "first generation")
        await collect(harness, "next generation")

        assert len(schemas) == 2
        first_delegate = schemas[0]["to_agent"]
        next_delegate = schemas[1]["to_agent"]
        assert isinstance(first_delegate, dict) and isinstance(next_delegate, dict)
        first_properties = first_delegate["properties"]
        next_properties = next_delegate["properties"]
        assert isinstance(first_properties, dict) and isinstance(next_properties, dict)
        first_agent = first_properties["agent"]
        next_agent = next_properties["agent"]
        assert isinstance(first_agent, dict) and isinstance(next_agent, dict)
        assert "peer" not in first_agent["enum"]
        assert "peer" in next_agent["enum"]
    finally:
        await harness.close()
