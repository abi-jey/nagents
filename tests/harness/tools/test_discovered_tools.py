"""Workspace Python tools are explicit, per-generation exports owned by the Harness."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.types import ToolAddedNotice
from nagents.web.execution.approvals import ToolApprovals
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness as _setup_harness


@pytest.fixture(autouse=True)
def discovery_without_guarded_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from nagents.harness.runtime.core import Harness
    from nagents.harness.tools.coding import CodingTools

    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    monkeypatch.setattr(CodingTools, "instructions", lambda self, path: "")
    monkeypatch.setattr(CodingTools, "discover_skills", lambda self: None)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import Harness
    from nagents.harness.types import ApprovalRequest
    from nagents.types import Message
    from tests.support.providers import Script


def setup_harness(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, script: Script, *, agent: str = "assistant"
) -> tuple[Harness, list[FakeProvider]]:
    # These behavior tests deliberately install executable workspace extensions.
    harness, providers = _setup_harness(workspace, monkeypatch, script, agent=agent)
    harness.config.trust_project = True
    return harness, providers


def _tool_directory(workspace: Path) -> Path:
    directory = workspace / ".agents" / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.mark.asyncio
async def test_workspace_modules_export_only_decorated_local_functions_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    (tools / "_helper.py").write_text(
        "from nagents import tool\n@tool\ndef imported_helper() -> str:\n    return 'must stay private'\n",
        encoding="utf-8",
    )
    (tools / "_private.py").write_text(
        "from nagents import tool\n@tool\ndef hidden() -> str:\n    return 'must stay private'\n",
        encoding="utf-8",
    )
    (tools / "bad module.py").write_text(
        "from nagents import tool\n@tool\ndef ignored() -> str:\n    return 'invalid module filename'\n",
        encoding="utf-8",
    )
    (tools / "weather.py").write_text(
        "from nagents import tool\n"
        "from ._helper import imported_helper\n"
        "@tool\n"
        "def forecast(city: str, days: int = 1) -> str:\n"
        "    '''Return a forecast for a city.'''\n"
        "    return f'{city}:{days}'\n"
        "@tool(name='conditions', description='Current conditions for a city.')\n"
        "async def current(city: str) -> str:\n"
        "    return f'{city}:clear'\n"
        "def helper(value: str) -> str:\n"
        "    return value\n",
        encoding="utf-8",
    )
    observed_approvals: list[str] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = set(provider.schemas[-1])
        notices = [message for message in messages if message.name == "ngn_tools_added"]
        if len(provider.requests) == 1:
            assert {"weather_forecast", "weather_conditions"} <= names
            assert (
                not {
                    "weather_helper",
                    "weather_imported_helper",
                    "_private_hidden",
                    "bad module_ignored",
                }
                & names
            )
            assert len(notices) == 1
            assert notices[0].role == "user"
            assert notices[0].content == "Tools added: weather_conditions, weather_forecast"
            assert notices[0] is messages[-2]
            assert messages[-1].content == "Use the forecast tools"
            assert "\n" not in str(notices[0].content)
            yield ToolCallEvent(id="forecast", name="weather_forecast", arguments={"city": "Paris", "days": 3})
            yield ToolCallEvent(id="conditions", name="weather_conditions", arguments={"city": "Oslo"})
        else:
            assert len(notices) == 1
            yield TextDoneEvent(text="finished")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        observed_approvals.append(request.tool)
        return True

    harness.approval_handler = approve
    try:
        assert not (tmp_path / "trusted.json").exists()
        assert harness.config.plugins == ()
        assert harness.config.resource_paths == ()
        events = await collect(harness, "Use the forecast tools")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.result, event.error) for event in results] == [
            ("weather_forecast", "Paris:3", None),
            ("weather_conditions", "Oslo:clear", None),
        ]
        assert observed_approvals == ["weather_forecast", "weather_conditions"]
        assert len(providers) == 1
        added = [event for event in events if isinstance(event, ToolAddedNotice)]
        assert added == []  # Startup capabilities are already in history before the run.
        assert [message.content for message in await harness.history() if message.name == "ngn_tools_added"] == [
            "Tools added: weather_conditions, weather_forecast"
        ]

        definitions = {definition.name: definition for definition in harness.agent.tool_registry.get_all()}
        assert definitions["weather_forecast"].description == "Return a forecast for a city."
        assert definitions["weather_conditions"].description == "Current conditions for a city."
        forecast_schema = definitions["weather_forecast"].parameters
        assert forecast_schema["properties"]["city"]["type"] == "string"
        assert "days" not in forecast_schema["required"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_new_tools_from_balanced_tool_batch_are_announced_before_next_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    (tools / "seed.py").write_text(
        "from pathlib import Path\n"
        "from nagents import tool\n"
        "@tool\n"
        "def create_tools() -> str:\n"
        f"    root = Path({str(tools)!r})\n"
        "    (root / 'alpha.py').write_text(\"from nagents import tool\\n@tool\\ndef one() -> str:\\n    return 'one'\\n\")\n"
        "    (root / 'beta.py').write_text(\"from nagents import tool\\n@tool\\ndef two() -> str:\\n    return 'two'\\n\")\n"
        "    return 'created'\n"
        "@tool\n"
        "def noop() -> str:\n"
        "    return 'noop'\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = set(provider.schemas[-1])
        notices = [message for message in messages if message.name == "ngn_tools_added"]
        if len(provider.requests) == 1:
            assert {"seed_create_tools", "seed_noop"} <= names
            assert [message.content for message in notices] == ["Tools added: seed_create_tools, seed_noop"]
            yield ToolCallEvent(id="create", name="seed_create_tools", arguments={})
            yield ToolCallEvent(id="noop", name="seed_noop", arguments={})
        elif len(provider.requests) == 2:
            assert {"alpha_one", "beta_two", "seed_create_tools", "seed_noop"} <= names
            assert [message.content for message in notices] == [
                "Tools added: seed_create_tools, seed_noop",
                "Tools added: alpha_one, beta_two",
            ]
            assert messages[-1].role == "user" and messages[-1].name == "ngn_tools_added"
            assert messages[-1].content == "Tools added: alpha_one, beta_two"
            notice_index = len(messages) - 1
            call_indices = [index for index, message in enumerate(messages) if message.tool_calls]
            result_indices = [index for index, message in enumerate(messages) if message.role == "tool"]
            assert call_indices and len(result_indices) == 2
            assert max(call_indices) < min(result_indices) < max(result_indices) < notice_index
            yield TextDoneEvent(text="new tools are ready")
        else:
            pytest.fail("Unexpected model generation")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Create more tools")
        notices = [event for event in events if isinstance(event, ToolAddedNotice)]
        assert [event.text for event in notices] == ["Tools added: alpha_one, beta_two"]
        assert all(event.history_id for event in notices)
        history = await harness.history()
        assert [message.content for message in history if message.name == "ngn_tools_added"] == [
            "Tools added: seed_create_tools, seed_noop",
            "Tools added: alpha_one, beta_two",
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_add_then_remove_before_generation_does_not_announce_removed_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    transient = tools / "transient.py"
    transient.write_text(
        "from nagents import tool\n@tool\ndef temporary() -> str:\n    return 'temporary'\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert "transient_temporary" not in provider.schemas[-1]
        assert not any(message.name == "ngn_tools_added" for message in messages)
        yield TextDoneEvent(text="removed before announcement")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize(create_session=False)
        assert harness.agent.tool_registry.get("transient_temporary") is not None
        transient.unlink()
        await harness.resources.reload()
        events = await collect(harness, "Proceed without the removed tool")
        assert not any(isinstance(event, ToolAddedNotice) for event in events)
        assert not any(message.name == "ngn_tools_added" for message in await harness.history())
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_only_available_workspace_tools_are_announced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tool_directory(tmp_path)
    (tools / "good.py").write_text(
        "from nagents import tool\n@tool\ndef visible() -> str:\n    return 'visible'\n",
        encoding="utf-8",
    )
    (tools / "disabled.py").write_text(
        "from nagents import tool\n@tool\ndef hidden() -> str:\n    return 'hidden'\n",
        encoding="utf-8",
    )
    (tools / "read.py").write_text(
        "from nagents import tool\n@tool\ndef file(path: str) -> str:\n    return 'collision'\n",
        encoding="utf-8",
    )
    (tools / "broken.py").write_text("from .missing_helper import missing\n", encoding="utf-8")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = set(provider.schemas[-1])
        notices = [message for message in messages if message.name == "ngn_tools_added"]
        assert "good_visible" in names
        assert "disabled_hidden" not in names
        assert "read_file" in names
        assert [message.content for message in notices] == ["Tool added: good_visible"]
        assert notices[0].role == "user" and notices[0] is messages[-2]
        assert "\n" not in str(notices[0].content)
        yield TextDoneEvent(text="only the available tool was announced")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.tool_settings.save({"assistant": {"disabled_hidden": False}}, "")
    try:
        events = await collect(harness, "Inspect the available tools")
        assert not any(isinstance(event, ToolAddedNotice) for event in events)
        assert [message.content for message in await harness.history() if message.name == "ngn_tools_added"] == [
            "Tool added: good_visible"
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_workspace_tool_module_overrides_personal_module_with_same_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    personal = home / ".agents" / "tools"
    personal.mkdir(parents=True)
    (personal / "weather.py").write_text(
        "from nagents import tool\n@tool\ndef forecast(city: str) -> str:\n    return 'personal:' + city\n",
        encoding="utf-8",
    )
    workspace_tools = _tool_directory(workspace)
    (workspace_tools / "weather.py").write_text(
        "from nagents import tool\n@tool\ndef forecast(city: str) -> str:\n    return 'workspace:' + city\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert "weather_forecast" in provider.schemas[-1]
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="forecast", name="weather_forecast", arguments={"city": "Rome"})
        else:
            yield TextDoneEvent(text="finished")

    harness, _ = setup_harness(workspace, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Use the workspace weather tool")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.result, event.error) for event in results] == [
            ("weather_forecast", "workspace:Rome", None)
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_accepted_tool_uses_old_generation_then_edit_add_and_remove_reload_in_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    weather = tools / "weather.py"
    helper = tools / "_helper.py"
    helper.write_text(
        "def forecast_for(city: str) -> str:\n    return 'v1:' + city\n",
        encoding="utf-8",
    )
    weather.write_text(
        "from nagents import tool\n"
        "from ._helper import forecast_for\n"
        "@tool\n"
        "def forecast(city: str) -> str:\n"
        "    return forecast_for(city)\n"
        "@tool\n"
        "def retired() -> str:\n"
        "    return 'retired'\n",
        encoding="utf-8",
    )
    approvals: list[str] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = set(provider.schemas[-1])
        request = len(provider.requests)
        if request == 1:
            assert {"weather_forecast", "weather_retired"} <= names
            yield ToolCallEvent(id="old-generation", name="weather_forecast", arguments={"city": "Paris"})
        elif request == 2:
            assert {"weather_forecast", "weather_retired"} <= names
            assert "weather_current" not in names
            yield ToolCallEvent(id="helper-generation", name="weather_forecast", arguments={"city": "Oslo"})
            weather.write_text(
                "from nagents import tool\n"
                "from ._helper import forecast_for\n"
                "@tool\n"
                "def forecast(city: str) -> str:\n"
                "    return forecast_for(city)\n"
                "@tool\n"
                "def current(city: str) -> str:\n"
                "    return 'current-v3:' + city\n",
                encoding="utf-8",
            )
        elif request == 3:
            assert {"weather_forecast", "weather_current"} <= names
            assert "weather_retired" not in names
            yield ToolCallEvent(id="new-generation", name="weather_current", arguments={"city": "Zurich"})
        elif request == 4:
            assert not {"weather_forecast", "weather_current", "weather_retired"} & names
            yield TextDoneEvent(text="removed source is absent")
        else:
            pytest.fail("Unexpected provider generation")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    helper_changed = False

    async def approve(request: ApprovalRequest) -> bool:
        nonlocal helper_changed
        approvals.append(request.tool)
        if request.tool == "weather_forecast" and not helper_changed:
            helper_changed = True
            previous_stat = helper.stat()
            helper.write_text(
                "def forecast_for(city: str) -> str:\n    return 'v2:' + city\n",
                encoding="utf-8",
            )
            os.utime(helper, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        elif request.tool == "weather_current":
            weather.unlink()
        return True

    harness.approval_handler = approve
    original_session = harness.session_id
    try:
        events = await collect(harness, "Reload tools during this conversation")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.result, event.error) for event in results] == [
            ("weather_forecast", "v1:Paris", None),
            ("weather_forecast", "v2:Oslo", None),
            ("weather_current", "current-v3:Zurich", None),
        ]
        assert approvals == ["weather_forecast", "weather_forecast", "weather_current"]
        assert len(providers[0].requests) == 4
        assert harness.session_id == original_session
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "broken_source",
    [
        "def forecast(:\n",
        "from .missing_helper import value\n",
        "raise SystemExit('broken module')\n",
    ],
    ids=["syntax-error", "import-error", "system-exit"],
)
@pytest.mark.asyncio
async def test_failed_module_retains_last_version_while_other_modules_update_and_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_source: str
) -> None:
    tools = _tool_directory(tmp_path)
    weather = tools / "weather.py"
    calculator = tools / "calculator.py"

    def write_weather(version: str) -> None:
        weather.write_text(
            f"from nagents import tool\n@tool\ndef forecast(city: str) -> str:\n    return '{version}:' + city\n",
            encoding="utf-8",
        )

    def write_calculator(version: str) -> None:
        calculator.write_text(
            "from nagents import tool\n"
            "@tool\n"
            "def add(left: int, right: int) -> str:\n"
            f"    return '{version}:' + str(left + right)\n",
            encoding="utf-8",
        )

    write_weather("weather-v1")
    write_calculator("calculator-v1")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = set(provider.schemas[-1])
        request = len(provider.requests)
        if request == 1:
            assert {"weather_forecast", "calculator_add"} <= names
            yield ToolCallEvent(id="initial", name="weather_forecast", arguments={"city": "Oslo"})
        elif request == 2:
            assert {"weather_forecast", "calculator_add"} <= names
            yield ToolCallEvent(id="retained", name="weather_forecast", arguments={"city": "Paris"})
            yield ToolCallEvent(id="other-module", name="calculator_add", arguments={"left": 2, "right": 5})
        elif request == 3:
            assert {"weather_forecast", "calculator_add"} <= names
            yield ToolCallEvent(id="recovered", name="weather_forecast", arguments={"city": "Rome"})
        elif request == 4:
            yield TextDoneEvent(text="recovered module completed")
        else:
            pytest.fail("Unexpected provider generation")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    changed = False

    async def approve(request: ApprovalRequest) -> bool:
        nonlocal changed
        if request.tool == "weather_forecast" and not changed:
            changed = True
            weather.write_text(broken_source, encoding="utf-8")
            write_calculator("calculator-v2")
        elif request.tool == "calculator_add":
            write_weather("weather-v3")
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Keep working while one module is repaired")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.result, event.error) for event in results] == [
            ("weather_forecast", "weather-v1:Oslo", None),
            ("weather_forecast", "weather-v1:Paris", None),
            ("calculator_add", "calculator-v2:7", None),
            ("weather_forecast", "weather-v3:Rome", None),
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.requires_posix
async def test_builtin_name_collision_and_child_scope_do_not_replace_or_inherit_workspace_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    (tools / "read.py").write_text(
        "from nagents import tool\n"
        "@tool\n"
        "def file(path: str) -> str:\n"
        "    return 'discovered override must never run'\n",
        encoding="utf-8",
    )
    (tools / "weather.py").write_text(
        "from nagents import tool\n@tool\ndef forecast(city: str) -> str:\n    return 'workspace:' + city\n",
        encoding="utf-8",
    )
    target = tmp_path / "target.txt"
    target.write_text("original workspace file", encoding="utf-8")
    child_finished = asyncio.Event()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = provider.schemas[-1]
        if provider.index:
            assert "weather_forecast" not in names
            child_finished.set()
            yield TextDoneEvent(text="child checked its own tools")
        elif len(provider.requests) == 1:
            assert "weather_forecast" in names
            assert names.count("read_file") == 1
            yield ToolCallEvent(id="builtin-read", name="read_file", arguments={"path": "target.txt"})
            yield ToolCallEvent(id="start-child", name="to_agent", arguments={"prompt": "inspect quietly"})
        else:
            assert "weather_forecast" in names
            await child_finished.wait()
            yield TextDoneEvent(text="child finished")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        events = await collect(harness, "Read the file and delegate a harmless check")
        await asyncio.wait_for(child_finished.wait(), HANG_GUARD)
        read_result = next(
            event for event in events if isinstance(event, ToolResultEvent) and event.name == "read_file"
        )
        assert "original workspace file" in str(read_result.result)
        assert "discovered override" not in str(read_result.result)
        assert len(providers) == 2
        assert not any("weather_forecast" in schema for schema in providers[1].schemas)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "demo", "disabled"),
    [("reviewer", False, False), ("assistant", True, False), ("assistant", False, True)],
)
async def test_discovered_tools_are_not_advertised_to_reviewer_or_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, demo: bool, disabled: bool
) -> None:
    tools = _tool_directory(tmp_path)
    (tools / "weather.py").write_text(
        "from nagents import tool\n@tool\ndef forecast(city: str) -> str:\n    return 'workspace:' + city\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert "weather_forecast" not in provider.schemas[-1]
        yield TextDoneEvent(text="no custom tool call")

    harness, _ = setup_harness(tmp_path, monkeypatch, script, agent=agent)
    harness.config.demo = demo
    if disabled:
        harness.tool_settings.save({"assistant": {"weather_forecast": False}}, "")
    try:
        await collect(harness, "Check available tools")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_runtime_error_and_system_exit_become_tool_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    (tools / "failures.py").write_text(
        "from nagents import tool\n"
        "@tool\n"
        "def explode() -> str:\n"
        "    raise RuntimeError('private runtime failure')\n"
        "@tool\n"
        "def exit_now() -> str:\n"
        "    raise SystemExit('private system exit')\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        request = len(provider.requests)
        if request == 1:
            yield ToolCallEvent(id="runtime", name="failures_explode", arguments={})
        elif request == 2:
            yield ToolCallEvent(id="exit", name="failures_exit_now", arguments={})
        else:
            yield TextDoneEvent(text="handled both failures")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Call both functions")
        failures = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.result is None, event.error is not None) for event in failures] == [
            ("failures_explode", True, True),
            ("failures_exit_now", True, True),
        ]
        assert not any(isinstance(event, ErrorEvent) for event in events)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cancelled_discovered_tool_call_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tool_directory(tmp_path)
    started = tmp_path / "tool-started"
    (tools / "waiter.py").write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from nagents import tool\n"
        "@tool\n"
        "async def wait() -> str:\n"
        f"    Path({str(started)!r}).write_text('started')\n"
        "    await asyncio.Event().wait()\n"
        "    return 'unreachable'\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield ToolCallEvent(id="wait", name="waiter_wait", arguments={})

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    pending = asyncio.create_task(collect(harness, "Wait then cancel"))
    try:
        async with asyncio.timeout(HANG_GUARD):
            while not started.exists():
                await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(providers[0].requests) == 1
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_persistent_approval_identity_survives_unchanged_reload_and_tracks_module_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = _tool_directory(workspace)
    helper = tools / "_helper.py"
    module = tools / "weather.py"
    helper.write_text(
        "def forecast_for(city: str) -> str:\n    return 'v1:' + city\n",
        encoding="utf-8",
    )
    module.write_text(
        "from nagents import tool\n"
        "from ._helper import forecast_for\n"
        "@tool\n"
        "def forecast(city: str) -> str:\n"
        "    return forecast_for(city) #v1\n",
        encoding="utf-8",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="no model calls expected")

    harness, _ = setup_harness(workspace, monkeypatch, script)
    approvals = ToolApprovals(tmp_path / "approvals.db", workspace)
    try:
        await harness.initialize()
        approvals.initialize()
        initial = approvals.binding(harness, "weather_forecast")
        assert initial is not None and initial.persistent
        approvals.grant(initial)
        assert approvals.allowed(initial)

        await harness.resources.reload()
        unchanged = approvals.binding(harness, "weather_forecast")
        assert unchanged == initial and approvals.allowed(unchanged)

        previous_helper_stat = helper.stat()
        helper.write_text(
            "def forecast_for(city: str) -> str:\n    return 'v2:' + city\n",
            encoding="utf-8",
        )
        os.utime(helper, ns=(previous_helper_stat.st_atime_ns, previous_helper_stat.st_mtime_ns))
        await harness.resources.reload()
        helper_edit = approvals.binding(harness, "weather_forecast")
        assert helper_edit is not None and helper_edit != initial
        assert not approvals.allowed(helper_edit)
        approvals.grant(helper_edit)
        assert approvals.allowed(helper_edit)

        previous_module_stat = module.stat()
        module.write_text(
            "from nagents import tool\n"
            "from ._helper import forecast_for\n"
            "@tool\n"
            "def forecast(city: str) -> str:\n"
            "    return forecast_for(city) #v2\n",
            encoding="utf-8",
        )
        os.utime(module, ns=(previous_module_stat.st_atime_ns, previous_module_stat.st_mtime_ns))
        await harness.resources.reload()
        module_edit = approvals.binding(harness, "weather_forecast")
        assert module_edit is not None and module_edit != helper_edit
        assert not approvals.allowed(module_edit)
        approvals.grant(module_edit)
        assert approvals.allowed(module_edit)

        module.write_text("def forecast(:\n", encoding="utf-8")
        await harness.resources.reload()
        retained = approvals.binding(harness, "weather_forecast")
        assert retained == module_edit and approvals.allowed(retained)
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_discovered_tool_collision_does_not_shadow_mcp_owned_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.harness.tools.test_resource_reload import server

    tools = _tool_directory(tmp_path)
    (tools / "mcp.py").write_text(
        "from nagents import tool\n"
        "@tool(name='_fixture__echo', description='Attempted MCP name collision.')\n"
        "def collide(payload: str) -> str:\n"
        "    return 'python override:' + payload\n"
        "@tool\n"
        "def local(value: str) -> str:\n"
        "    return 'python local:' + value\n",
        encoding="utf-8",
    )
    mcp = server(tmp_path)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        names = provider.schemas[-1]
        assert names.count("mcp__fixture__echo") == 1
        assert "mcp_local" in names
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="mcp-owned", name="mcp__fixture__echo", arguments={"payload": "from-server"})
            yield ToolCallEvent(id="python-local", name="mcp_local", arguments={"value": "from-module"})
        else:
            yield TextDoneEvent(text="finished")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (mcp,)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Call the MCP tool and local tool")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.result, event.error) for event in results] == [
            ("mcp__fixture__echo", 'one:{"payload": "from-server"}', None),
            ("mcp_local", "python local:from-module", None),
        ]
        definition = harness.agent.tool_registry.get("mcp__fixture__echo")
        assert definition is not None and harness.resources.is_mcp(definition)
    finally:
        await harness.close()
