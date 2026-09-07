"""Memory-only slash discovery and offline execution contracts."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import FrozenInstanceError
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock

import pytest

from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness.commands import BUILTIN_COMMANDS
from nagents.harness.commands import Command
from nagents.harness.commands import CommandRegistry
from nagents.harness.commands import CommandResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nagents.harness.commands import CommandHandler


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instance = Harness(HarnessConfig(workspace=workspace, data_dir=tmp_path / "state", demo=True))
    monkeypatch.setattr(instance.agent.provider, "generate", AsyncMock(side_effect=AssertionError("No model call")))
    monkeypatch.setattr(
        instance.agent.provider, "verify_model", AsyncMock(side_effect=AssertionError("No verification"))
    )
    return instance


def test_builtin_metadata_and_frozen_command(harness: Harness) -> None:
    registry = CommandRegistry(harness)
    commands = registry.list()
    assert commands == list(BUILTIN_COMMANDS)
    assert commands[0].name == "help"
    assert {command.name for command in commands} == {
        "help",
        "new",
        "sessions",
        "compact",
        "agent",
        "model",
        "login",
        "logout",
        "plugins",
        "context",
        "quit",
        "tasks",
        "dictate",
        "queue",
    }
    assert all(
        command.source == "harness" and command.description and not command.name.startswith("/") for command in commands
    )
    for name in ("agent", "model"):
        command = registry.get(name)
        assert command is not None and command.argument_hint and not command.requires_arguments
    with pytest.raises(FrozenInstanceError):
        commands[0].name = "changed"  # type: ignore[misc]
    commands.clear()
    assert registry.list()[0].name == "help"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/review",
        "Review",
        "review task",
        "1review",
        "_review",
        "-review",
        "review.py",
        "review:setup",
        "skill:audit",
        "a" * 65,
        "caf\u00e9",
        "review\n",
    ],
)
def test_registration_rejects_invalid_names(harness: Harness, name: str) -> None:
    with pytest.raises(ValueError, match="Command names"):
        CommandRegistry(harness).register(name, "description", prompt="test")


@pytest.mark.parametrize("name", ["a", "review", "review_2-fast", "a" * 64])
def test_registration_accepts_valid_names(harness: Harness, name: str) -> None:
    registry = CommandRegistry(harness)
    command = registry.register(name, "description", prompt="test")
    assert command == Command(name, "description")
    assert registry.get(name) == command


def test_builtin_reservations_and_duplicate_registration(harness: Harness) -> None:
    registry = CommandRegistry(harness)
    for command in BUILTIN_COMMANDS:
        with pytest.raises(ValueError, match="reserved"):
            registry.register(command.name, "replacement", prompt="bad")
    registry.register("review", "first", prompt="first")
    with registry.plugin_source("another.plugin:setup"), pytest.raises(ValueError, match="already registered"):
        registry.register("review", "second", prompt="second")
    assert registry.get("review") == Command("review", "first")


def test_exactly_one_prompt_or_async_handler(harness: Harness) -> None:
    registry = CommandRegistry(harness)

    async def callback(instance: Harness, arguments: str) -> CommandResult:
        return CommandResult(message=arguments)

    with pytest.raises(ValueError, match="exactly one"):
        registry.register("empty", "description")
    with pytest.raises(ValueError, match="exactly one"):
        registry.register("both", "description", prompt="prompt", handler=callback)
    registry.register("callback", "description", handler=callback)


def test_sync_callbacks_rejected_without_invocation(harness: Harness) -> None:
    invoked = False

    def sync_callback(instance: Harness, arguments: str) -> CommandResult:
        nonlocal invoked
        invoked = True
        return CommandResult(message="bad")

    async def async_callback(instance: Harness, arguments: str) -> CommandResult:
        return CommandResult(message="good")

    registry = CommandRegistry(harness)
    with pytest.raises(TypeError, match="must be async"):
        registry.register("sync", "description", handler=cast("CommandHandler", sync_callback))
    with pytest.raises(TypeError, match="must be async"):
        registry.register(
            "wrapped", "description", handler=lambda instance, arguments: async_callback(instance, arguments)
        )
    assert not invoked


def test_async_callable_objects_and_partial_handlers(harness: Harness) -> None:
    class Callback:
        async def __call__(self, instance: Harness, arguments: str) -> CommandResult:
            return CommandResult(message=f"object: {arguments}")

    async def callback(prefix: str, instance: Harness, arguments: str) -> CommandResult:
        return CommandResult(message=f"{prefix}: {arguments}")

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        registry.register("object", "description", handler=Callback())
        registry.register("partial", "description", handler=partial(callback, "partial"))
        try:
            assert (await registry.execute("object", "task")).message == "object: task"
            assert (await registry.execute("partial", "task")).message == "partial: task"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("template", "arguments", "expected"),
    [
        ("Review $ARGUMENTS", "file.py", "Review file.py"),
        ("$ARGUMENTS + $ARGUMENTS", "{literal}", "{literal} + {literal}"),
        (
            '{"key": "value"}\n{arguments}\n$ARGUMENTS',
            "${do_not_evaluate}",
            '{"key": "value"}\n{arguments}\n${do_not_evaluate}',
        ),
        ("Review changes", "file.py", "Review changes\n\nfile.py"),
        ("Review changes", "", "Review changes"),
        ("Before $ARGUMENTS after", "", "Before  after"),
        ("$ARGUMENTS", "  keep whitespace\n", "  keep whitespace\n"),
    ],
)
def test_literal_argument_expansion(harness: Harness, template: str, arguments: str, expected: str) -> None:
    async def scenario() -> None:
        registry = CommandRegistry(harness)
        registry.register("review", "Review changes", prompt=template)
        try:
            assert await registry.execute("review", arguments) == CommandResult(prompt=expected)
            assert await harness.history() == []
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_required_arguments_prevent_callback_invocation(harness: Harness) -> None:
    arguments_received: list[str] = []

    async def callback(instance: Harness, arguments: str) -> CommandResult:
        arguments_received.append(arguments)
        return CommandResult(message="accepted")

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        registry.register("review", "Review", handler=callback, argument_hint="<path>", requires_arguments=True)
        registry.register("explain", "Explain", prompt="Explain $ARGUMENTS", requires_arguments=True)
        try:
            for name in ("review", "explain"):
                for arguments in ("", " \n\t"):
                    with pytest.raises(ValueError, match="requires arguments"):
                        await registry.execute(name, arguments)
            assert not arguments_received
            await registry.execute("review", "  file.py  ")
            assert arguments_received == ["  file.py  "]
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("installed.module:setup", "plugin:installed.module"),
        ("/private/home/extensions/review.py:setup", "plugin:review.py"),
        ("./extensions/review.py:setup", "plugin:review.py"),
        ("C:\\private\\extensions\\review.py:setup", "plugin:review.py"),
    ],
)
def test_plugin_source_labels_and_restoration(harness: Harness, reference: str, expected: str) -> None:
    registry = CommandRegistry(harness)
    with registry.plugin_source(reference):
        assert registry.register("outer", "Outer", prompt="test").source == expected
        with pytest.raises(RuntimeError, match="fixture"), registry.plugin_source("nested.module:setup"):
            assert registry.register("inner", "Inner", prompt="test").source == "plugin:nested.module"
            raise RuntimeError("fixture")
        assert registry.register("restored", "Restored", prompt="test").source == expected
        assert registry.register("explicit", "Explicit", prompt="test", source="native").source == "native"
    assert registry.register("native", "Native", prompt="test").source == "harness"


def test_plugin_source_inherited_by_async_setup_tasks_and_isolated(harness: Harness) -> None:
    async def scenario() -> None:
        registry = CommandRegistry(harness)
        other = CommandRegistry(harness)
        release = asyncio.Event()

        async def setup_task(name: str) -> Command:
            await release.wait()
            return registry.register(name, "Task registration", prompt="test")

        with registry.plugin_source("/private/first.py:setup"):
            first = asyncio.create_task(setup_task("first"))
            assert other.register("native", "Native", prompt="test").source == "harness"
        with registry.plugin_source("second.module:setup"):
            second = asyncio.create_task(setup_task("second"))
        release.set()
        assert (await first).source == "plugin:first.py"
        assert (await second).source == "plugin:second.module"
        assert registry.register("native", "Native", prompt="test").source == "harness"

    asyncio.run(scenario())


def test_list_is_live_deduplicated_and_does_not_load_or_call(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = CommandRegistry(harness)
    initialize = AsyncMock(side_effect=AssertionError("Discovery must not initialize"))
    skill = AsyncMock(side_effect=AssertionError("Discovery must not load content"))
    callback = AsyncMock(side_effect=AssertionError("Discovery must not invoke callbacks"))
    monkeypatch.setattr(harness, "initialize", initialize)
    monkeypatch.setattr(harness.tools, "skill", skill)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Discovery attempted disk I/O"))
    registry.register("review", "Custom review", handler=callback)
    harness.tools.skills["review"] = (".agents/skills/review/SKILL.md", "Discovered review skill")
    harness.tools.skills["help"] = (".agents/skills/help/SKILL.md", "Discovered help skill")
    assert registry.get("skill:review") == Command("skill:review", "Discovered review skill", "skill", "[task]")
    assert registry.get("help") == BUILTIN_COMMANDS[0]
    assert registry.get("review") == Command("review", "Custom review")
    registry.register("later", "Later registration", prompt="test")
    harness.tools.skills["later"] = (".ngn/skills/later/SKILL.md", "Later skill")
    commands = registry.list()
    assert len(commands) == len(set(commands)) == len(BUILTIN_COMMANDS) + 5
    assert registry.get("later") is not None and registry.get("skill:later") is not None
    harness.tools.skills.pop("later")
    assert registry.get("skill:later") is None
    assert registry.get("missing") is None
    initialize.assert_not_called()
    skill.assert_not_called()
    callback.assert_not_called()


def test_callbacks_can_use_public_harness_methods_without_model_calls(harness: Harness) -> None:
    async def callback(instance: Harness, arguments: str) -> CommandResult:
        assert instance is harness and not instance._busy
        await instance.new_session()
        await instance.set_agent(arguments)
        return CommandResult(message=f"Switched to {instance.config.agent}")

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        registry.register("switch", "Switch session and profile", handler=callback)
        original = harness.session_id
        try:
            result = await registry.execute("switch", "reviewer")
            assert result == CommandResult(message="Switched to reviewer")
            assert harness.session_id != original
            assert await harness.history() == []
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_busy_entry_rejected_before_initialization(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize = AsyncMock()
    monkeypatch.setattr(harness, "initialize", initialize)
    registry = CommandRegistry(harness)
    registry.register("review", "Review", prompt="Review changes")

    async def scenario() -> None:
        with harness.operation("run"), pytest.raises(RuntimeError, match="busy"):
            await registry.execute("review")
        initialize.assert_not_called()
        await harness.close()

    asyncio.run(scenario())


def test_builtin_and_unknown_commands_are_not_evaluated(harness: Harness) -> None:
    async def scenario() -> None:
        registry = CommandRegistry(harness)
        try:
            for command in BUILTIN_COMMANDS:
                with pytest.raises(ValueError, match="client-handled action"):
                    await registry.execute(command.name)
            for name in ("missing", "/help", "os:system", "./extension.py:setup", "__import__('os')"):
                with pytest.raises(ValueError, match="Unknown command"):
                    await registry.execute(name)
            assert await harness.history() == []
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_callback_cancellation_and_error_propagate(harness: Harness) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    error = RuntimeError("original failure")

    async def waiting(instance: Harness, arguments: str) -> CommandResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return CommandResult()

    async def failing(instance: Harness, arguments: str) -> CommandResult:
        raise error

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        registry.register("waiting", "Wait", handler=waiting)
        registry.register("failing", "Fail", handler=failing)
        task = asyncio.create_task(registry.execute("waiting"))
        try:
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set() and not harness._busy
            with pytest.raises(RuntimeError) as caught:
                await registry.execute("failing")
            assert caught.value is error
            await harness.new_session()
        finally:
            task.cancel()
            await harness.close()

    asyncio.run(scenario())


def test_command_result_validation() -> None:
    assert CommandResult() == CommandResult(message="", prompt="")
    assert CommandResult(message="message").prompt == ""
    assert CommandResult(prompt="prompt").message == ""
    with pytest.raises(ValueError, match="not both"):
        CommandResult(message="message", prompt="prompt")
    with pytest.raises(TypeError, match="must be strings"):
        CommandResult(message=cast("str", 42))


def test_callback_results_validated_at_execution(harness: Harness) -> None:
    async def wrong_type(instance: Harness, arguments: str) -> CommandResult:
        return cast("CommandResult", "not a result")

    async def mutated(instance: Harness, arguments: str) -> CommandResult:
        result = CommandResult(message="message")
        result.prompt = "prompt"
        return result

    async def generator(instance: Harness, arguments: str) -> AsyncIterator[CommandResult]:
        yield CommandResult()

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        registry.register("wrong", "Wrong", handler=wrong_type)
        registry.register("mutated", "Mutated", handler=mutated)
        with pytest.raises(TypeError, match="must be async"):
            registry.register("generator", "Generator", handler=cast("CommandHandler", generator))
        try:
            with pytest.raises(TypeError, match="must return CommandResult"):
                await registry.execute("wrong")
            with pytest.raises(ValueError, match="not both"):
                await registry.execute("mutated")
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_discovered_skill_loads_current_content_on_demand(harness: Harness) -> None:
    folder = harness.workspace / ".agents/skills/audit"
    folder.mkdir(parents=True)
    skill_file = folder / "SKILL.md"
    skill_file.write_text("---\nname: audit\ndescription: Audit changes\n---\nInitial instructions\n")
    marker = harness.workspace / "script-executed"
    (folder / "script.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        try:
            await harness.initialize()
            assert registry.get("skill:audit") == Command("skill:audit", "Audit changes", "skill", "[task]")
            skill_file.write_text("Current instructions with literal {braces} and $ARGUMENTS\n")
            result = await registry.execute("skill:audit", "Review src/app.py")
            assert not result.message
            assert "# Skill Context: audit" in result.prompt
            assert "Current instructions with literal {braces} and $ARGUMENTS" in result.prompt
            assert "# Task Arguments\nReview src/app.py" in result.prompt
            assert "did not execute any scripts" in result.prompt and not marker.exists()
            assert "No additional task arguments" in (await registry.execute("skill:audit")).prompt
            assert await harness.history() == []
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outside_path", [False, True])
def test_skill_execution_preserves_symlink_and_workspace_guards(
    harness: Harness, tmp_path: Path, outside_path: bool
) -> None:
    folder = harness.workspace / ".ngn/skills/audit"
    folder.mkdir(parents=True)
    skill_file = folder / "SKILL.md"
    skill_file.write_text("---\ndescription: Audit\n---\nSafe initial content\n")
    outside = tmp_path / "outside-fixture.md"
    outside.write_text("Must not load outside content")

    async def scenario() -> None:
        registry = CommandRegistry(harness)
        try:
            await harness.initialize()
            if outside_path:
                harness.tools.skills["audit"] = (str(outside), "Audit")
            else:
                skill_file.unlink()
                skill_file.symlink_to(outside)
            assert registry.get("skill:audit") is not None
            with pytest.raises(PermissionError):
                await registry.execute("skill:audit")
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_commands_source_has_no_ui_or_runtime_imports() -> None:
    source = Path(__file__).parents[1] / "src/nagents/harness/commands.py"
    tree = ast.parse(source.read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    names = [name.name for node in imports if isinstance(node, ast.Import) for name in node.names]
    names.extend(node.module or "" for node in imports if isinstance(node, ast.ImportFrom))
    assert not any(name.startswith(("textual", "rich", "nagents.tui", "importlib")) for name in names)
    assert not any(isinstance(node, ast.ImportFrom) and node.module == "runtime" for node in tree.body)
