"""Offline harness contracts. Credential-looking fixtures exist only under tmp_path."""

from __future__ import annotations

import asyncio
import copy
import os
import shlex
import signal
import stat
import sys
from contextlib import aclosing
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.agent import Agent
from nagents.events import CompactionDoneEvent
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.extensions import AgentPlugin
from nagents.extensions import CompactionResult
from nagents.harness import ApprovalRequest
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import Notice
from nagents.harness import ToolOutput
from nagents.harness import load_config
from nagents.harness.config import PROVIDERS
from nagents.harness.config import AgentProfile
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message
from nagents.types import ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.extensions import CompactionRequest
    from nagents.extensions import RunContext
    from nagents.types import GenerationConfig
    from nagents.types import ToolArguments
    from nagents.types import ToolDefinition


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NGN_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def config(tmp_path: Path) -> HarnessConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessConfig(
        workspace=workspace, data_dir=tmp_path / "data", api_key_env="NGN_TEST_KEY", model="fake-model"
    )


class ScriptedProvider(Provider):
    def __init__(self, rounds: list[list[Event]]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "not-a-real-key", "fake-model")
        self.rounds = rounds
        self.requests: list[list[Message]] = []
        self.schemas: list[list[ToolDefinition]] = []

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
        self.requests.append(copy.deepcopy(messages))
        self.schemas.append(
            [replace(tool, parameters=copy.deepcopy(tool.parameters), func=None) for tool in tools or []]
        )
        for event in self.rounds[len(self.requests) - 1]:
            await asyncio.sleep(0)
            yield event


async def approve(request: ApprovalRequest) -> bool:
    return True


def test_config_ignores_entire_untrusted_project_and_does_not_import(config: HarnessConfig, tmp_path: Path) -> None:
    project = config.workspace / ".ngn"
    project.mkdir()
    marker = tmp_path / "imported"
    (project / "extension.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (project / "config.toml").write_text(
        'model = "injected"\nprovider = "anthropic"\nbase_url = "https://untrusted.invalid"\n'
        'api_key_env = "UNRELATED_SECRET"\nplugins = ["extension.py:setup"]\n'
    )
    with pytest.warns(UserWarning, match="Ignoring untrusted project"):
        loaded = load_config(config.workspace)
    assert loaded.provider == "openai"
    assert loaded.model == "gpt-4.1"
    assert loaded.api_key_env == "OPENAI_API_KEY"
    assert not loaded.base_url and not loaded.plugins
    assert loaded.diagnostics and not marker.exists()
    (project / "config.toml").write_text("not even valid TOML [")
    with pytest.warns(UserWarning):
        assert load_config(config.workspace).model == "gpt-4.1"


def test_config_precedence_origins_profiles_and_xdg(
    config: HarnessConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NGN_MODEL", "environment-model")
    monkeypatch.setenv("NGN_PROVIDER", "gemini")
    monkeypatch.setenv("NGN_API_KEY_ENV", "NGN_TEST_KEY")
    monkeypatch.setenv("NGN_DEMO", "true")
    assert load_config(config.workspace).model == "environment-model"
    user = tmp_path / "config/ngn"
    user.mkdir(parents=True)
    (user / "config.toml").write_text('model = "user-model"\nplugins = ["./extension.py:setup"]\n')
    loaded = load_config(config.workspace)
    assert loaded.model == "user-model"
    assert loaded.plugins == (f"{user / 'extension.py'}:setup",)
    assert loaded.data_dir == tmp_path / "data/ngn"
    project = config.workspace / ".ngn"
    project.mkdir()
    project_config = project / "config.toml"
    project_config.write_text(
        'model = "project-model"\nplugins = ["../extension.py:setup", "installed.module:setup"]\n'
        'data_dir = "local-state"\nagent = "audit"\n'
        '[profiles.audit]\nmode = "reviewer"\ninstructions = "Report regressions"\nmodel = "audit-model"\n'
    )
    loaded = load_config(config.workspace, trust_project=True)
    assert loaded.model == "project-model" and loaded.provider == "gemini" and loaded.demo
    assert loaded.plugins == (f"{config.workspace / 'extension.py'}:setup", "installed.module:setup")
    assert loaded.data_dir == project / "local-state"
    assert loaded.profile("audit") == AgentProfile("reviewer", "Report regressions", "audit-model")
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('model = "explicit-model"\n')
    assert load_config(config.workspace, explicit, trust_project=True).model == "explicit-model"
    assert load_config(config.workspace, project_config).plugins == loaded.plugins
    assert load_config(config.workspace, user / "config.toml", trust_project=True).model == "user-model"


@pytest.mark.parametrize(
    "toml",
    [
        'api_key = "never-store-this"',
        "unknown = 1",
        'demo = "false"',
        "model = 3",
        'plugins = "extension.py:setup"',
        'plugins = ["extension.py"]',
        "plugins = [3]",
        "shell_timeout = false",
        "max_output = true",
        'provider = "missing"',
        'base_url = "https://user:secret@example.invalid"',
        'base_url = "https://example.invalid?key=secret"',
        'api_key_env = "literal-secret-key"',
        '[profiles.build]\nmode = "build"',
        '[profiles.audit]\nmode = "permissive"',
        "[profiles.audit]\ninstructions = 3",
        "model = [",
    ],
)
def test_strict_config_rejects_invalid_and_secret_fields(config: HarnessConfig, tmp_path: Path, toml: str) -> None:
    path = tmp_path / "explicit.toml"
    path.write_text(toml)
    with pytest.raises(ValueError):
        load_config(config.workspace, path)


def test_provider_aliases_and_missing_explicit_config(config: HarnessConfig, tmp_path: Path) -> None:
    assert set(ProviderType) <= set(PROVIDERS.values())
    assert PROVIDERS["openai"] is ProviderType.OPENAI_COMPATIBLE
    with pytest.raises(FileNotFoundError):
        load_config(config.workspace, tmp_path / "missing.toml")


@pytest.mark.requires_posix
def test_initialize_is_local_and_missing_credentials_are_deferred(
    config: HarnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> bool:
        pytest.fail("Network/model verification attempted at startup")

    async def scenario() -> None:
        harness = Harness(config)
        monkeypatch.setattr(harness.agent.provider, "verify_model", forbidden)
        try:
            await harness.initialize()
            assert isinstance(harness.agent, Agent)
            assert not harness.agent.save_tool_outputs
            assert await harness.history() == []
            assert "$NGN_TEST_KEY" in harness.describe()
            assert "deferred-until-live-request" not in harness.describe()
        finally:
            await harness.close()
        harness = Harness(config)
        try:
            await harness.initialize()
            with pytest.raises(ValueError, match="Set NGN_TEST_KEY"):
                _ = [event async for event in harness.run("hello")]
            assert harness._worker is None
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        ".git/config",
        ".env",
        ".env.production",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".netrc",
        ".npmrc",
        "server.pem",
        "tls.key",
        "secrets.yaml",
        "service-account.json",
        ".config/gcloud/application_default_credentials.json",
        "client_secret_fixture.json",
        "infra.tfstate",
    ],
)
@pytest.mark.requires_posix
def test_file_boundaries_and_credentials(config: HarnessConfig, path: str) -> None:
    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            event = await harness.agent.tool_executor.execute(ToolCall("read", "read_file", {"path": path}))
            assert event.error
            event = await harness.agent.tool_executor.execute(
                ToolCall("write", "write", {"path": path, "content": "bad"})
            )
            assert event.error
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_symlinks_hardlinks_special_files_and_absolute_paths(config: HarnessConfig, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside fixture")
    (config.workspace / "link").symlink_to(outside)
    (config.workspace / "folder").symlink_to(tmp_path, target_is_directory=True)
    (config.workspace / "hardlink").hardlink_to(outside)
    os.mkfifo(config.workspace / "pipe")

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            for path in (
                str(outside),
                "link",
                "folder/outside.txt",
                "hardlink",
                "pipe",
                str(harness.agent.session.db_path),
            ):
                event = await harness.agent.tool_executor.execute(ToolCall("read", "read_file", {"path": path}))
                assert event.error, path
            (config.workspace / "normal.txt").write_text("normal")
            result = await harness.tools.read_file(str(config.workspace / "normal.txt"))
            assert result["content"] == "1: normal"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_bounded_read_literal_search_globs_ignores_and_instructions(config: HarnessConfig) -> None:
    root = config.workspace
    (root / "AGENTS.md").write_text("Root policy")
    (root / ".gitignore").write_text("ignored/\n*.log\n!keep.log\n")
    for directory in ("src", "ignored", ".git", ".venv", "node_modules"):
        (root / directory).mkdir()
        (root / directory / "hidden.py").write_text("needle .*\n")
    (root / "src/AGENTS.md").write_text("Nested policy")
    (root / "src/.gitignore").write_text("hidden.py\n")
    (root / "src/app.py").write_text("first\nneedle .*\nlast\n")
    (root / "ignored.log").write_text("needle .*\n")
    (root / "keep.log").write_text("needle .*\n")
    (root / "plain.py").write_text("plain\n")
    (root / ".env").write_text("fixture only, not a credential")
    (root / "long.txt").write_text("x" * 2000)

    async def scenario() -> None:
        harness = Harness(replace(config, max_output=1024))
        try:
            await harness.initialize()
            result = await harness.tools.read_file("src/app.py", start_line=2, limit=1)
            assert result["content"] == "2: needle .*"
            assert result["total_lines"] == 3 and result["truncated"]
            assert "Root policy" in str(result["instructions"]) and "Nested policy" in str(result["instructions"])
            assert "Nested policy" in str(harness.agent.system_prompt)
            assert len(str((await harness.tools.read_file("long.txt"))["content"])) <= 1024
            result = await harness.tools.find("**/*.py")
            assert result["paths"] == ["plain.py", "src/app.py"]
            result = await harness.tools.list_files()
            assert "src/" in str(result["paths"]) and "src/app.py" not in str(result["paths"])
            assert ".env" not in str(result["paths"])
            result = await harness.tools.search(".*")
            assert result["matches"] == ["keep.log:1: needle .*", "src/app.py:2: needle .*"]
            assert (await harness.tools.search(".*", limit=1))["truncated"]
            assert (await harness.tools.search("needle", path="src"))["matches"] == ["src/app.py:2: needle .*"]
            assert (await harness.tools.find(limit=1))["truncated"]
            with pytest.raises(ValueError, match="start_line"):
                await harness.tools.read_file("plain.py", start_line=0)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_exact_edit_diff_mode_and_no_silent_overwrite(config: HarnessConfig) -> None:
    path = config.workspace / "app.py"
    path.write_bytes(b"print('old')\r\n")
    path.chmod(0o751)
    requests: list[ApprovalRequest] = []

    async def record(request: ApprovalRequest) -> bool:
        requests.append(request)
        assert path.read_bytes() == b"print('old')\r\n"
        return True

    async def scenario() -> None:
        harness = Harness(config)
        harness.approval_handler = record
        try:
            await harness.initialize()
            with pytest.raises(ValueError, match="not read"):
                await harness.tools.edit("app.py", "old", "new")
            await harness.tools.read_file("app.py")
            result = await harness.tools.edit("app.py", "old", "new")
            assert result["diff"] == requests[0].preview
            assert "--- a/app.py" in requests[0].preview and "+print('new')" in requests[0].preview
            assert path.read_bytes() == b"print('new')\r\n"
            assert stat.S_IMODE(path.stat().st_mode) == 0o751
            with pytest.raises(FileExistsError, match="File exists"):
                await harness.tools.write("app.py", "overwrite")
            harness.approval_handler = approve
            created = await harness.tools.write("created.txt", "new content")
            assert "--- /dev/null" in str(created["diff"])
            assert (config.workspace / "created.txt").read_text() == "new content"
            assert not list(config.workspace.glob(".ngn-*.tmp"))
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_default_denial_ambiguous_replace_and_stale_reads(config: HarnessConfig) -> None:
    path = config.workspace / "app.txt"
    path.write_text("old old\n")

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            await harness.tools.read_file("app.txt")
            for old in ("old", "", "absent"):
                with pytest.raises(ValueError, match="exactly once"):
                    await harness.tools.edit("app.txt", old, "new")
            with pytest.raises(PermissionError, match="Approval denied"):
                await harness.tools.edit("app.txt", "old old", "new")
            path.write_text("user change\n")
            with pytest.raises(ValueError, match="changed since read"):
                await harness.tools.edit("app.txt", "user change", "bad")
            with pytest.raises(PermissionError, match="Approval denied"):
                await harness.tools.write("new.txt", "new")
            assert path.read_text() == "user change\n" and not (config.workspace / "new.txt").exists()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["content", "mode", "symlink", "parent_symlink", "instructions"])
@pytest.mark.requires_posix
def test_edit_rechecks_after_awaiting_approval(config: HarnessConfig, tmp_path: Path, mutation: str) -> None:
    folder = config.workspace / "src"
    folder.mkdir()
    path = folder / "app.txt"
    path.write_text("old\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.txt").write_text("outside\n")
    waiting = asyncio.Event()
    proceed = asyncio.Event()

    async def pause(request: ApprovalRequest) -> bool:
        assert "-old" in request.preview and "+new" in request.preview
        waiting.set()
        await proceed.wait()
        return True

    async def scenario() -> None:
        harness = Harness(config)
        harness.approval_handler = pause
        try:
            await harness.initialize()
            await harness.tools.read_file("src/app.txt")
            task = asyncio.create_task(harness.tools.edit("src/app.txt", "old", "new"))
            await asyncio.wait_for(waiting.wait(), 2)
            if mutation == "content":
                path.write_text("user change\n")
            elif mutation == "mode":
                path.chmod(0o700)
            elif mutation == "symlink":
                path.unlink()
                path.symlink_to(outside / "app.txt")
            elif mutation == "parent_symlink":
                folder.rename(config.workspace / "moved")
                folder.symlink_to(outside, target_is_directory=True)
            else:
                (folder / "AGENTS.md").write_text("New applicable instructions")
            proceed.set()
            with pytest.raises((ValueError, PermissionError, OSError)):
                await task
            assert (outside / "app.txt").read_text() == "outside\n"
            if mutation == "content":
                assert path.read_text() == "user change\n"
            if mutation == "mode":
                assert path.read_text() == "old\n" and stat.S_IMODE(path.stat().st_mode) == 0o700
            assert not list(config.workspace.rglob(".ngn-*.tmp"))
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_new_file_race_preserves_user_file(config: HarnessConfig) -> None:
    path = config.workspace / "new.txt"

    async def raced(request: ApprovalRequest) -> bool:
        await asyncio.sleep(0)
        path.write_text("user created this")
        return True

    async def scenario() -> None:
        harness = Harness(config)
        harness.approval_handler = raced
        try:
            await harness.initialize()
            with pytest.raises(FileExistsError):
                await harness.tools.write("new.txt", "model content")
            assert path.read_text() == "user created this"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_new_nested_instructions_are_returned_before_writing(config: HarnessConfig) -> None:
    (config.workspace / "src").mkdir()
    (config.workspace / "src/AGENTS.md").write_text("Use a specific style")

    async def scenario() -> None:
        harness = Harness(config)
        harness.approval_handler = approve
        try:
            await harness.initialize()
            with pytest.raises(ValueError, match="Use a specific style"):
                await harness.tools.write("src/new.py", "pass\n")
            assert not (config.workspace / "src/new.py").exists()
            await harness.tools.write("src/new.py", "pass\n")
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_post_plugin_validation_and_custom_tools_share_core_loop(config: HarnessConfig) -> None:
    called: list[str] = []
    requests: list[ApprovalRequest] = []

    async def custom(value: str) -> str:
        called.append(value)
        return f"custom {value}"

    class Transform(AgentPlugin):
        async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
            return replace(call, arguments={"value": "transformed"})

    async def record(request: ApprovalRequest) -> bool:
        requests.append(request)
        request.arguments["value"] = "approval mutation must not affect execution"
        return True

    async def scenario() -> None:
        harness = Harness(config)
        provider = ScriptedProvider(
            [
                [ToolCallEvent(id="custom-1", name="custom", arguments={"value": "original"})],
                [TextDoneEvent(text="complete")],
            ]
        )
        harness.agent.provider = provider
        harness.agent.plugins.append(Transform())
        harness.agent.register_tool(custom)
        harness.approval_handler = record
        try:
            events = [event async for event in harness.run("custom tool")]
            assert called == ["transformed"]
            assert '"transformed"' in requests[0].preview
            assert any(isinstance(event, ToolResultEvent) and event.result == "custom transformed" for event in events)
            assert any(isinstance(event, DoneEvent) for event in events)
            assert all(
                "_save_to" not in tool.parameters.get("properties", {}) for tools in provider.schemas for tool in tools
            )
            bad = await harness.agent.tool_executor.execute(ToolCall("bad", "custom", {"value": 3}))
            assert bad.error and "must be string" in bad.error
            unknown = await harness.agent.tool_executor.execute(ToolCall("unknown", "missing", {}))
            assert unknown.error and "does not exist" in unknown.error
            assert requests[-1].tool == "missing"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_builtin_override_is_not_automatically_trusted(config: HarnessConfig) -> None:
    invoked = False

    async def unsafe(path: str) -> str:
        nonlocal invoked
        invoked = True
        return "custom"

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            harness.agent.register_tool(unsafe, name="read_file")
            result = await harness.agent.tool_executor.execute(ToolCall("read", "read_file", {"path": "anything"}))
            assert result.error and "Approval denied" in result.error
            assert not invoked
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_transformed_builtin_paths_and_reserved_save_outputs_cannot_bypass_gate(
    config: HarnessConfig, tmp_path: Path
) -> None:
    saved = tmp_path / "must-not-save.txt"

    class Redirect(AgentPlugin):
        async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
            return replace(call, arguments={**call.arguments, "path": "../outside.txt"})

    async def scenario() -> None:
        harness = Harness(config)
        harness.agent.plugins.append(Redirect())
        harness.agent.provider = ScriptedProvider(
            [
                [ToolCallEvent(id="read", name="read_file", arguments={"path": "safe.txt", "_save_to": str(saved)})],
                [TextDoneEvent(text="blocked")],
            ]
        )
        try:
            events = [event async for event in harness.run("read fixture")]
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            assert result.error and "traversal" in result.error
            assert not saved.exists()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_profiles_gate_mutations_shell_and_custom_tools(config: HarnessConfig) -> None:
    (config.workspace / "read.txt").write_text("readable")
    requests: list[ApprovalRequest] = []

    async def record(request: ApprovalRequest) -> bool:
        requests.append(request)
        return True

    async def custom() -> str:
        return "not called"

    async def scenario() -> None:
        harness = Harness(replace(config, profiles={"audit": AgentProfile("reviewer", "Audit only", "audit-model")}))
        harness.approval_handler = record
        harness.agent.register_tool(custom)
        try:
            await harness.initialize()
            await harness.set_agent("audit")
            assert harness.mode == "reviewer" and harness.agent.provider.model == "audit-model"
            assert "Audit only" in str(harness.agent.system_prompt)
            calls: list[tuple[str, ToolArguments]] = [
                ("shell", {"command": "exit 0"}),
                ("write", {"path": "new", "content": "bad"}),
                ("edit", {"path": "read.txt", "old": "readable", "new": "bad"}),
                ("custom", {}),
            ]
            for name, arguments in calls:
                result = await harness.agent.tool_executor.execute(ToolCall("call", name, arguments))
                assert result.error and "reviewer" in result.error
            assert requests == []
            assert not (
                await harness.agent.tool_executor.execute(ToolCall("read", "read_file", {"path": "read.txt"}))
            ).error
            await harness.set_agent("build")
            await harness.set_model("another-model")
            assert harness.config.model == harness.agent.provider.model == "another-model"
            with pytest.raises(ValueError, match="Unknown agent"):
                await harness.set_agent("does-not-exist")
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("async_setup", [False, True])
@pytest.mark.requires_posix
def test_trusted_python_plugins_diagnostics_and_library_access(
    config: HarnessConfig, tmp_path: Path, async_setup: bool
) -> None:
    plugin = tmp_path / "extension.py"
    plugin.write_text(
        "from nagents.extensions import AgentPlugin\n"
        "async def greeting(name: str) -> str:\n    return 'hello ' + name\n"
        f"{'async ' if async_setup else ''}def setup(harness):\n"
        "    harness.agent.register_tool(greeting)\n    return AgentPlugin()\n"
    )

    async def scenario() -> None:
        harness = Harness(replace(config, plugins=(f"{plugin}:setup",)))
        try:
            await harness.initialize()
            assert len(harness.agent.plugins) == 1
            assert "greeting" in harness.agent.tool_registry.names()
            assert "Loaded trusted Python plugin" in harness.describe()
            result = await harness.agent.tool_executor.execute(ToolCall("custom", "greeting", {"name": "test"}))
            assert result.error and "Approval denied" in result.error
            harness.approval_handler = approve
            result = await harness.agent.tool_executor.execute(ToolCall("custom", "greeting", {"name": "test"}))
            assert result.result == "hello test"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_installed_plugin_and_explicit_failure(
    config: HarnessConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ngn_fixture_extension.py").write_text("def setup(harness):\n    harness.agent.max_tool_rounds = 5\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    async def scenario() -> None:
        harness = Harness(replace(config, plugins=("ngn_fixture_extension:setup",)))
        try:
            await harness.initialize()
            assert harness.agent.max_tool_rounds == 5
        finally:
            await harness.close()
        plugin = tmp_path / "broken.py"
        plugin.write_text("def setup(harness):\n    raise ValueError('fixture failure')\n")
        harness = Harness(replace(config, plugins=(f"{plugin}:setup",)))
        try:
            with pytest.raises(RuntimeError, match="fixture failure"):
                await harness.initialize()
            assert "fixture failure" in harness.describe()
            with pytest.raises(RuntimeError, match="initialization failed"):
                await harness.initialize()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_skill_discovery_is_text_only_and_on_demand(config: HarnessConfig) -> None:
    skill = config.workspace / ".agents/skills/audit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: audit\ndescription: "Review changes"\n---\nOn-demand body.\n')
    marker = config.workspace / "script-ran"
    (skill / "script.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            assert "Review changes" in harness.describe()
            assert "On-demand body" not in str(harness.agent.system_prompt)
            result = await harness.tools.skill("audit")
            assert "On-demand body" in str(result["content"])
            assert not marker.exists()
            with pytest.raises(ValueError, match="Unknown skill"):
                await harness.tools.skill("absent")
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_shell_always_asks_and_default_denies(config: HarnessConfig) -> None:
    async def scenario() -> None:
        harness = Harness(config)
        requests: list[ApprovalRequest] = []

        async def deny(request: ApprovalRequest) -> bool:
            requests.append(request)
            return False

        harness.approval_handler = deny
        try:
            await harness.initialize()
            result = await harness.agent.tool_executor.execute(
                ToolCall("shell", "shell", {"command": "touch must-not-exist"})
            )
            assert result.error and "Approval denied" in result.error
            assert len(requests) == 1 and "NOT SANDBOXED" in requests[0].description
            assert str(config.workspace) in requests[0].preview
            assert not (config.workspace / "must-not-exist").exists()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_shell_streams_bounded_output_and_timeout(config: HarnessConfig) -> None:
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote('import os; print(os.getcwd()); print(chr(120) * 10000)')}"
    )

    async def scenario() -> None:
        harness = Harness(replace(config, max_output=1024, shell_timeout=1.0))
        harness.approval_handler = approve
        harness.agent.provider = ScriptedProvider(
            [
                [ToolCallEvent(id="shell-call", name="shell", arguments={"command": command})],
                [TextDoneEvent(text="checked")],
            ]
        )
        try:
            events = [event async for event in harness.run("run the fixture")]
            chunks = [event for event in events if isinstance(event, ToolOutput)]
            assert chunks and all(event.call_id == "shell-call" for event in chunks)
            assert str(config.workspace) in "".join(event.text for event in chunks)
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            assert isinstance(result.result, dict) and result.result["truncated"]
            assert len(str(result.result["output"]).encode()) <= 1024
            assert events.index(chunks[0]) < events.index(result)
            timeout = await harness.tools.shell("sleep 10", timeout=0.05)
            assert timeout["timed_out"] and timeout["exit_code"] != 0
            with pytest.raises(ValueError, match="timeout"):
                await harness.tools.shell("exit 0", timeout=2)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_consumer", [False, True])
@pytest.mark.requires_posix
def test_run_aclose_and_cancellation_kill_shell_and_await_plugins(config: HarnessConfig, cancel_consumer: bool) -> None:
    output_ready = asyncio.Event()
    cleanup_finished = asyncio.Event()
    pid = 0

    class Cleanup(AgentPlugin):
        async def after_run(self, context: RunContext) -> None:
            await asyncio.sleep(0.01)
            cleanup_finished.set()

    async def scenario() -> None:
        nonlocal pid
        harness = Harness(config)
        harness.approval_handler = approve
        harness.agent.plugins.append(Cleanup())
        harness.agent.provider = ScriptedProvider(
            [[ToolCallEvent(id="shell-call", name="shell", arguments={"command": "printf '%s\\n' $$; exec sleep 30"})]]
        )

        async def consume() -> None:
            nonlocal pid
            async with aclosing(harness.run("long command")) as events:
                async for event in events:
                    if isinstance(event, ToolOutput):
                        pid = int(event.text.strip())
                        output_ready.set()
                        if not cancel_consumer:
                            break

        try:
            task = asyncio.create_task(consume())
            await asyncio.wait_for(output_ready.wait(), 3)
            if cancel_consumer:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            assert cleanup_finished.is_set()
            assert harness._worker is None and not harness._busy
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            history = await harness.history()
            assert any(message.role == "tool" for message in history)
        finally:
            if pid:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_concurrent_runs_and_session_mutations_rejected(config: HarnessConfig) -> None:
    entered = asyncio.Event()

    class Paused(AgentPlugin):
        async def before_run(self, context: RunContext, message: Message) -> Message:
            entered.set()
            await asyncio.Event().wait()
            return message

    async def scenario() -> None:
        harness = Harness(config)
        harness.agent.provider = ScriptedProvider([])
        harness.agent.plugins.append(Paused())

        async def consume() -> None:
            _ = [event async for event in harness.run("first")]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(RuntimeError, match="busy"):
                _ = [event async for event in harness.run("second")]
            for mutation in (
                harness.new_session(),
                harness.resume("id"),
                harness.compact(),
                harness.set_agent("reviewer"),
                harness.set_model("model"),
            ):
                with pytest.raises(RuntimeError, match="busy"):
                    await mutation
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await harness.new_session()
        finally:
            task.cancel()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_cancellation_while_shell_spawns_reaps_process(config: HarnessConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real_spawn(
            "/bin/sh",
            "-c",
            "exec sleep 30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)

    async def scenario() -> None:
        harness = Harness(config)
        harness.approval_handler = approve
        task = asyncio.create_task(harness.tools.shell("fixture"))
        try:
            await asyncio.wait_for(spawned.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert processes[0].returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(processes[0].pid, 0)
        finally:
            release.set()
            for process in processes:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_demo_cancellation_during_approval_does_not_write(config: HarnessConfig) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pause(request: ApprovalRequest) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return True

    async def scenario() -> None:
        harness = Harness(replace(config, demo=True))
        harness.approval_handler = pause

        async def consume() -> None:
            _ = [event async for event in harness.run("demo approval")]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set() and harness._worker is None
            assert not (config.workspace / "example.py").exists()
            await harness.new_session()
        finally:
            task.cancel()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_workspace_sessions_titles_resume_and_custom_compaction(config: HarnessConfig, tmp_path: Path) -> None:
    class LocalCompaction:
        async def should_compact(self, request: CompactionRequest) -> bool:
            return False

        async def compact(self, request: CompactionRequest) -> CompactionResult:
            assert request.force and all(message.role != "system" for message in request.messages)
            return CompactionResult(
                [Message(role="compaction_summary", content="Local test summary")], "Local test summary"
            )

    async def scenario() -> None:
        harness = Harness(config)
        harness.agent.provider = ScriptedProvider([[TextDoneEvent(text="reply")], [TextDoneEvent(text="follow-up")]])
        harness.agent.compaction_strategy = LocalCompaction()
        try:
            _ = [event async for event in harness.run("First   customer prompt")]
            original = harness.session_id
            _ = [event async for event in harness.run("Second prompt")]
            assert (await harness.list_sessions())[0].title == "First customer prompt"
            result = await harness.compact()
            assert isinstance(result, CompactionDoneEvent) and result.summary_text == "Local test summary"
            assert (await harness.history())[0].content is not None
            await harness.new_session()
            assert await harness.history() == []
            assert (await harness.list_sessions())[0].id == original
            await harness.resume(original)
            assert len(await harness.history()) == 1
        finally:
            await harness.close()
        resumed = Harness(config)
        other_path = tmp_path / "other-workspace"
        other_path.mkdir()
        other = Harness(replace(config, workspace=other_path))
        try:
            await resumed.resume(original)
            assert (await resumed.list_sessions())[0].title == "First customer prompt"
            await other.initialize()
            assert all(session.id != original for session in await other.list_sessions())
            with pytest.raises(ValueError, match="this workspace"):
                await other.resume(original)
        finally:
            await resumed.close()
            await other.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.requires_posix
def test_offline_demo_approval_sessions_and_no_plugins(config: HarnessConfig, tmp_path: Path, accepted: bool) -> None:
    marker = tmp_path / "plugin-imported"
    plugin = tmp_path / "demo-plugin.py"
    plugin.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (config.workspace / "sample.txt").write_text("local fixture")
    requests: list[ApprovalRequest] = []

    async def record(request: ApprovalRequest) -> bool:
        requests.append(request)
        return accepted

    async def scenario() -> None:
        harness = Harness(replace(config, demo=True, plugins=(f"{plugin}:setup",)))
        harness.approval_handler = record
        try:
            events = [event async for event in harness.run("demo approval")]
            assert not marker.exists()
            assert len(requests) == 1 and "preview only" in requests[0].description
            assert "--- a/example.py" in requests[0].preview
            assert any(isinstance(event, Notice) and "OFFLINE DEMO" in event.text for event in events)
            assert any(isinstance(event, TextChunkEvent) for event in events)
            final = next(event.final_text for event in events if isinstance(event, DoneEvent))
            assert "OFFLINE DEMO" in final and "sample.txt" in final
            assert ("**approved**" if accepted else "**declined**") in final
            assert not (config.workspace / "example.py").exists()
            session = harness.session_id
            await harness.new_session()
            await harness.resume(session)
            assert any(
                message.role == "user" and message.content == "demo approval" for message in await harness.history()
            )
            result = await harness.compact()
            assert "OFFLINE DEMO" in result.summary_text
            calls: list[tuple[str, ToolArguments]] = [
                ("shell", {"command": "touch forbidden"}),
                ("write", {"path": "new", "content": "bad"}),
            ]
            for name, arguments in calls:
                result_event = await harness.agent.tool_executor.execute(ToolCall("blocked", name, arguments))
                assert result_event.error and "OFFLINE DEMO" in result_event.error
        finally:
            await harness.close()

    asyncio.run(scenario())
