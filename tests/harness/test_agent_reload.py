"""Definition edits become capabilities at the next accepted model generation."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.extensions import RunContext
from nagents.harness import runtime
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.model_bindings import ModelBinding
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.harness.subagents import TaskInfo
from nagents.harness.types import TaskCompleted
from tests.support.config import connection
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import notifications

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import ApprovalRequest
    from nagents.types import Message


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".data"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))


def definition(name: str, *, native: bool = True, instructions: str = "Follow the assigned task.") -> str:
    flags = "user_invocable: true\nsubagent_available: true\n" if native else ""
    return f"---\nname: {name}\ndescription: General coding agent\n{flags}---\n{instructions}\n"


def catalog(harness: Harness) -> list[str]:
    tool = next(tool for tool in harness.agent.tool_registry.get_all() if tool.name == "delegate")
    result = tool.parameters["properties"]["agent"]["enum"]
    assert isinstance(result, list)
    return result


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_create_rename_and_delegate_without_restart_with_trusted_directory_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native, foreign = workspace / ".ngn/agents", workspace / ".agents/agents"
    native.mkdir(parents=True)
    foreign.mkdir(parents=True)
    trusted = tmp_path / "operator.json"
    trusted.write_text(
        json.dumps(
            {
                "agent_imports": [
                    {"path": str(foreign), "format": "antigravity"},
                    {"path": str(native), "format": "native"},
                ]
            }
        )
    )
    config = load_config(workspace, trusted)
    assert not config.trust_project
    config.data_dir = tmp_path / "state"
    config.providers = connection(auth="api-key")
    (workspace / "evidence.txt").write_text("Mini successfully read workspace evidence.")
    providers: list[FakeProvider] = []
    seen: list[list[str]] = []

    async def rename_agent(previous: str, following: str) -> str:
        for directory, is_native in ((native, True), (foreign, False)):
            old = directory / f"{previous}.md"
            old.rename(directory / f"{following}.md")
            (directory / f"{following}.md").write_text(definition(following, native=is_native))
        return following

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            assert "read_file" in provider.schemas[-1], "The native definition must override Antigravity's empty tools"
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="evidence", name="read_file", arguments={"path": "evidence.txt"})
            else:
                yield TextDoneEvent(text="Mini verified workspace evidence.")
            return
        if notifications(messages):
            yield TextDoneEvent(text="Mini completed the task.")
            return
        seen.append(list(harness.resources.pinned["delegate"].parameters["properties"]["agent"]["enum"]))
        turn = len(provider.requests)
        if turn == 1:
            yield ToolCallEvent(
                id="native", name="write", arguments={"path": ".ngn/agents/mint.md", "content": definition("mint")}
            )
            yield ToolCallEvent(
                id="foreign",
                name="write",
                arguments={"path": ".agents/agents/mint.md", "content": definition("mint", native=False)},
            )
        elif turn == 2:
            yield ToolCallEvent(
                id="rename-one", name="rename_agent", arguments={"previous": "mint", "following": "min"}
            )
        elif turn == 3:
            yield ToolCallEvent(
                id="rename-two", name="rename_agent", arguments={"previous": "min", "following": "mini"}
            )
        elif turn == 4:
            yield ToolCallEvent(
                id="start-mini", name="delegate", arguments={"agent": "mini", "prompt": "Read evidence.txt and report."}
            )
        else:
            yield TextDoneEvent(text="Waiting for Mini.")

    def factory(config: HarnessConfig, login_store: object = None) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(runtime, "HarnessProvider", factory)
    harness = Harness(config)
    harness.agent.compaction_strategy = None
    harness.agent.register_tool(rename_agent)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Create mint, rename it min then mini, and ask mini to verify evidence.")
        assert seen[:4] == [["assistant"], ["assistant", "mint"], ["assistant", "min"], ["assistant", "mini"]]
        assert any(isinstance(event, TaskCompleted) and "Mini verified" in event.result for event in events)
        assert not [event.error for event in events if isinstance(event, ToolResultEvent) and event.error]
        assert not config.trust_project
        assert config.profile("mini").source == str(native / "mini.md")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_catalog_refresh_does_not_mutate_advertised_schema_or_selected_profile(tmp_path: Path) -> None:
    root = tmp_path / ".ngn/agents"
    root.mkdir(parents=True)
    path = root / "mint.md"
    path.write_text(definition("mint", instructions="Original accepted instructions."))
    config = load_config(tmp_path, trust_project=True)
    config.agent = "mint"
    config.demo = True
    config.data_dir = tmp_path / "state"
    harness = Harness(config)
    try:
        await harness.initialize()
        context = RunContext(harness.agent, harness.session_id, "harness")
        await harness.resources.before_generation(context)
        advertised = next(tool for tool in harness.agent.tool_registry.get_all() if tool.name == "delegate")
        previous_revision = harness.profile_revision
        path.unlink()
        (root / "mini.md").write_text(definition("mini"))
        assert harness.refresh_agent_definitions()
        assert harness.profile_revision != previous_revision
        assert config.profile_names == ("assistant", "mini")
        assert advertised.parameters["properties"]["agent"]["enum"] == ["assistant", "mint"]
        assert catalog(harness) == ["assistant", "mint"]
        assert harness.current_profile().instructions == "Original accepted instructions."
        assert harness.mode == "build"
        await harness.resources.before_generation(context)
        assert catalog(harness) == ["assistant", "mini"]
        assert harness.current_profile().instructions == "Original accepted instructions."
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_issued_delegation_retains_profile_then_child_adopts_next_generation_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".ngn/agents"
    root.mkdir(parents=True)
    path = root / "mini.md"
    path.write_text(definition("mini", instructions="Accepted original Mini instructions."))
    (tmp_path / "evidence.txt").write_text("Evidence")
    config = load_config(tmp_path, trust_project=True)
    config.data_dir = tmp_path / "state"
    config.providers = connection(auth="api-key")
    providers: list[FakeProvider] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            prompt = str(messages[0].content)
            assert provider.model == "selected-child-model"
            if len(provider.requests) == 1:
                assert "Accepted original Mini instructions." in prompt
                assert "Replacement Mini instructions." not in prompt
                assert "read_file" in provider.schemas[-1]
                yield ToolCallEvent(id="accepted-read", name="read_file", arguments={"path": "evidence.txt"})
            else:
                assert "Replacement Mini instructions." in prompt
                assert "Accepted original Mini instructions." not in prompt
                assert provider.schemas[-1] == []
                yield TextDoneEvent(text="Updated child completed.")
        elif notifications(messages):
            yield TextDoneEvent(text="Finished.")
        elif len(provider.requests) == 1:
            # The model has received the old profile. The after_model resource
            # refresh observes this edit before executing its returned delegate.
            path.write_text(
                definition("mini", instructions="Replacement Mini instructions.").replace(
                    "description:", "tools: []\ndescription:"
                )
            )
            yield ToolCallEvent(
                id="accepted-delegate",
                name="delegate",
                arguments={"agent": "mini", "model": "selected-child-model", "prompt": "Read evidence and report."},
            )
        else:
            yield TextDoneEvent(text="Waiting.")

    def factory(config: HarnessConfig, login_store: object = None) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(runtime, "HarnessProvider", factory)
    harness = Harness(config)
    harness.agent.compaction_strategy = None
    try:
        events = await collect(harness)
        assert any(isinstance(event, TaskCompleted) and event.result == "Updated child completed." for event in events)
        assert not [event.error for event in events if isinstance(event, ToolResultEvent) and event.error]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_untrusted_workspace_stays_untrusted_and_programmatic_profiles_survive(tmp_path: Path) -> None:
    root = tmp_path / ".ngn/agents"
    root.mkdir(parents=True)
    (root / "mini.md").write_text(definition("mini"))
    harness = Harness(
        HarnessConfig(
            tmp_path, demo=True, profiles={"python": AgentProfile(instructions="Explicit")}, data_dir=tmp_path / "state"
        )
    )
    try:
        harness.refresh_agent_definitions()
        assert list(harness.config.profile_names) == ["assistant", "python"]
        harness.config.profiles["added"] = AgentProfile(description="Added through Python")
        harness.refresh_agent_definitions()
        assert list(harness.config.profile_names) == ["assistant", "added", "python"]
        assert harness.config.profile("python").instructions == "Explicit"
        assert not harness.config.trust_project
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_child_refreshes_definitions_and_inherits_permission_ceiling(tmp_path: Path) -> None:
    root = tmp_path / ".ngn/agents"
    root.mkdir(parents=True)
    config = load_config(tmp_path, trust_project=True)
    config.demo = True
    config.data_dir = tmp_path / "state"
    config.profiles["lead"] = AgentProfile(tools=("read_file", "delegate"))
    config.agent = "lead"
    harness = Harness(config)
    info = TaskInfo(
        "child", "assistant", profile="assistant", child_session_id="child-session", session_id=harness.session_id
    )
    child = harness.tasks._create_child("assistant", info)
    try:
        await child.initialize()
        (root / "mini.md").write_text(definition("mini"))
        await child.resources.before_generation(RunContext(child.agent, child.session_id, "harness"))
        assert "mini" in catalog(child)
        assert {tool.name for tool in child.agent.tool_registry.get_all()} == {"read_file", "delegate"}
        assert not child.tool_enabled("shell")
        harness.config.profiles["lead"] = replace(config.profile("lead"), allowed_agents=())
        assert not child.can_delegate
    finally:
        await child.close()
        await harness.close()


@pytest.mark.asyncio
async def test_named_model_pairs_use_agent_workspace_global_order_not_parent_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    providers = {name: ProviderProfile(kind="openai", auth="api-key") for name in ("global", "work", "agents")}
    store.store("global").save(ProviderRegistry(active="global", providers=providers), expected="0" * 64)
    store.model_store("global").save_binding(ModelBinding("global", "global-model"))
    store.model_store("workspace").save_binding(ModelBinding("work", "workspace-model"))

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def factory(profile: ProviderProfile, config: HarnessConfig, auth: object) -> FakeProvider:
        return FakeProvider(config, 0, script)

    monkeypatch.setattr(runtime, "build_provider", factory)
    config = HarnessConfig(
        tmp_path,
        data_dir=tmp_path / "state",
        providers=providers,
        provider="global",
        agent="lead",
        profiles={"lead": AgentProfile(model="parent-special", provider="agents"), "mini": AgentProfile()},
    )
    harness = Harness(config)
    info = TaskInfo("child", "mini", profile="mini", child_session_id="child-session", session_id=harness.session_id)
    child = harness.tasks._create_child("mini", info)
    try:
        assert (harness.config.provider, harness.agent.provider.model) == ("agents", "parent-special")
        assert (child.config.provider, child.agent.provider.model) == ("work", "workspace-model")
        selected = store.agent_models.save("mini", ModelBinding("agents", "mini-model"), expected="0" * 64)
        assert harness.resolved_model_binding("mini").binding == ModelBinding("agents", "mini-model")
        child.refresh_agent_definitions()
        child.activate_agent_definitions()
        assert (child.config.provider, child.agent.provider.model) == ("work", "workspace-model")
        store.agent_models.save("mini", ModelBinding(), expected=selected.revision)
        assert harness.resolved_model_binding("mini").binding == ModelBinding("work", "workspace-model")
        store.model_store("workspace").save_binding(ModelBinding())
        assert harness.resolved_model_binding("mini").binding == ModelBinding("global", "global-model")
    finally:
        await child.close()
        await harness.close()


def test_config_reads_model_reference_maps_without_splitting_provider(tmp_path: Path) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    providers = {name: ProviderProfile(kind="openai", auth="api-key") for name in ("first", "second")}
    store.store("global").save(ProviderRegistry(active="first", providers=providers), expected="0" * 64)
    path = tmp_path / "operator.json"
    path.write_text(
        json.dumps(
            {
                "model": {"provider": "second", "id": "workspace-model"},
                "provider_id": "first",
                "profiles": {"mini": {"model": {"provider": "first", "id": "mini-model"}}},
            }
        )
    )
    config = load_config(tmp_path, path)
    assert (config.provider, config.model) == ("second", "workspace-model")
    assert (config.profile("mini").provider, config.profile("mini").model) == ("first", "mini-model")


@pytest.mark.asyncio
async def test_legacy_model_id_keeps_workspace_route_but_explicit_pair_does_not_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    providers = {name: ProviderProfile(kind="openai", auth="api-key") for name in ("global", "work")}
    store.store("global").save(ProviderRegistry(active="global", providers=providers), expected="0" * 64)
    store.store("workspace").save(ProviderRegistry(active="work"), expected="0" * 64)
    store.model_store("global").save("existing-model")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def factory(profile: ProviderProfile, config: HarnessConfig, auth: object) -> FakeProvider:
        return FakeProvider(config, 0, script)

    monkeypatch.setattr(runtime, "build_provider", factory)
    config = load_config(tmp_path)
    config.data_dir = tmp_path / "state"
    harness = Harness(config)
    try:
        assert (config.provider, config.model) == ("work", "existing-model")
        assert harness.resolved_model_binding().binding == ModelBinding("work", "existing-model")
        store.model_store("global").save_binding(ModelBinding("global", "new-paired-model"))
        assert harness.resolved_model_binding().binding == ModelBinding("global", "new-paired-model")
        assert (harness.config.provider, harness.agent.provider.model) == ("work", "existing-model")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_model_generation_pins_overlay_and_default_bindings_until_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    providers = {name: ProviderProfile(kind="openai", auth="api-key") for name in ("home", "work")}
    store.global_store.save(ProviderRegistry(active="home", providers=providers), expected="0" * 64)
    store.model_store("global").save_binding(ModelBinding("home", "original-default"))

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def factory(profile: ProviderProfile, config: HarnessConfig, auth: object) -> FakeProvider:
        return FakeProvider(config, 0, script)

    monkeypatch.setattr(runtime, "build_provider", factory)
    config = HarnessConfig(
        tmp_path,
        data_dir=tmp_path / "state",
        provider="home",
        providers=providers,
        profiles={"mini": AgentProfile(), "unavailable": AgentProfile(provider="deleted", model="missing-model")},
    )
    harness = Harness(config)
    try:
        harness.activate_agent_definitions()
        assert harness.delegation_model_binding("mini") == ModelBinding("home", "original-default")
        with pytest.raises(ValueError, match="unavailable provider"):
            harness.delegation_model_binding("unavailable")
        assert harness.delegation_model_binding("unavailable", provider="home", model="explicit-model") == ModelBinding(
            "home", "explicit-model"
        )
        saved = store.agent_models.save("mini", ModelBinding("work", "overlay-model"), expected="0" * 64)
        store.model_store("global").save_binding(ModelBinding("work", "next-default"))
        assert harness.delegation_model_binding("mini") == ModelBinding("home", "original-default")
        assert harness.delegation_model_binding("assistant") == ModelBinding("home", "original-default")
        harness.activate_agent_definitions()
        assert harness.delegation_model_binding("mini") == ModelBinding("work", "overlay-model")
        assert harness.delegation_model_binding("assistant") == ModelBinding("work", "next-default")
        store.agent_models.save("mini", ModelBinding(), expected=saved.revision)
        assert harness.delegation_model_binding("mini") == ModelBinding("work", "overlay-model")
        harness.activate_agent_definitions()
        assert harness.delegation_model_binding("mini") == ModelBinding("work", "next-default")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_runtime_clone_keeps_authoritative_default_after_agent_override_and_inherit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    providers = {name: ProviderProfile(kind="openai", auth="api-key") for name in ("home", "special")}
    store.global_store.save(ProviderRegistry(active="home", providers=providers), expected="0" * 64)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def factory(profile: ProviderProfile, config: HarnessConfig, auth: object) -> FakeProvider:
        return FakeProvider(config, 0, script)

    monkeypatch.setattr(runtime, "build_provider", factory)
    config = HarnessConfig(
        tmp_path,
        data_dir=tmp_path / "state",
        provider="home",
        providers=providers,
        model="startup-model",
        agent="mini",
        profiles={"mini": AgentProfile(provider="special", model="special-model")},
    )
    original = Harness(config)
    assert (original.config.provider, original.agent.provider.model) == ("special", "special-model")
    store.agent_models.save("mini", ModelBinding(), expected="0" * 64)
    clone = Harness(original.clone_configuration())
    try:
        assert clone.default_model_binding().binding == ModelBinding("home", "startup-model")
        assert (clone.config.provider, clone.agent.provider.model) == ("home", "startup-model")
        assert (original.config.provider, original.agent.provider.model) == ("special", "special-model")
    finally:
        await clone.close()
        await original.close()


@pytest.mark.asyncio
async def test_connection_activation_preserves_model_references_but_updates_legacy_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    providers = {name: ProviderProfile(kind="openai", auth="api-key") for name in ("home", "work")}
    store.global_store.save(ProviderRegistry(active="home", providers=providers), expected="0" * 64)
    store.model_store("global").save_binding(ModelBinding("home", "paired-model"))

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def factory(profile: ProviderProfile, config: HarnessConfig, auth: object) -> FakeProvider:
        return FakeProvider(config, 0, script)

    monkeypatch.setattr(runtime, "build_provider", factory)
    harness = Harness(
        HarnessConfig(
            tmp_path, data_dir=tmp_path / "state", provider="home", providers=providers, model="fallback-model"
        )
    )
    try:
        await harness.activate_provider("work", store.workspace_store.load().revision)
        assert store.load().active == "work"
        assert (harness.config.provider, harness.agent.provider.model) == ("home", "paired-model")
        store.agent_models.save("assistant", ModelBinding("work", "agent-model"), expected="0" * 64)
        await harness.set_agent("assistant")
        await harness.activate_provider("home", store.workspace_store.load().revision)
        assert (harness.config.provider, harness.agent.provider.model) == ("work", "agent-model")
        preference = store.agent_models.load("assistant")
        store.agent_models.reset("assistant", expected=preference.revision)
        store.model_store("global").save_binding(ModelBinding())
        await harness.activate_provider("work", store.workspace_store.load().revision)
        assert (harness.config.provider, harness.agent.provider.model) == ("work", "fallback-model")
        assert harness.default_model_binding().binding == ModelBinding("work", "fallback-model")
    finally:
        await harness.close()
