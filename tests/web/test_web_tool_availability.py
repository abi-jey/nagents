"""Effective capabilities reach native requests while the Tools catalog stays editable."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiohttp import web

from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.provider.openai import OpenAIProvider
from nagents.tools.registry import ToolRegistry
from tests.providers.test_openai_provider import completion
from tests.providers.test_openai_provider import credentials
from tests.providers.test_openai_provider import endpoint
from tests.providers.test_openai_provider import sse
from tests.providers.test_openai_provider import text_item
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path


def custom_read() -> str:
    """A custom tool is not a builtin merely because its description says read-only."""
    raise AssertionError("Only fixture text should be generated")


@pytest.mark.parametrize("initial_agent", ["assistant", "reviewer"])
@pytest.mark.parametrize("initial_depth", [0, 2])
def test_native_requests_follow_profile_depth_scheduler_and_workspace_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_agent: str, initial_depth: int
) -> None:
    requests: list[dict[str, object]] = []

    async def handle(request: web.Request) -> web.Response:
        requests.append(cast("dict[str, object]", await request.json()))
        return web.Response(text=sse([completion([text_item("Fixture response")])]), content_type="text/event-stream")

    async def scenario() -> None:
        config = HarnessConfig(
            tmp_path,
            data_dir=tmp_path / "data",
            providers=connection(auth="api-key"),
            agent=initial_agent,
            max_subagent_depth=initial_depth,
            profiles={"reviewer": AgentProfile(mode="reviewer")},
        )
        async with (
            endpoint(monkeypatch, handle),
            client_app(tmp_path, config=config) as (_, client, headers, harnesses),
        ):
            harness = harnesses[0]
            harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
            harness.agent.provider = OpenAIProvider(credentials, model="fixture-native")
            harness.agent.register_tool(custom_read)
            definitions = {tool.name: tool for tool in ToolRegistry.get_all(harness.agent.tool_registry)}
            scheduler = harness.wakeup_handler
            assert scheduler is not None
            shadowed = False

            async def capture() -> set[str]:
                count = len(requests)
                async with asyncio.timeout(HANG_GUARD):
                    response = await client.post(
                        "/api/run",
                        headers=headers,
                        json={"session_id": harness.session_id, "prompt": "Return fixture text"},
                    )
                assert response.status_code == 200, response.text
                assert not any(json.loads(line)["event"] == "error" for line in response.text.splitlines() if line)
                assert len(requests) == count + 1
                tools = requests[-1]["tools"]
                assert isinstance(tools, list)
                wire = {tool["name"] for tool in tools}
                if not shadowed:
                    assert "wake_up_in" not in wire
                    assert harness.agent.tool_registry.get("wake_up_in") is definitions["wake_up_in"]
                catalog = (await client.get("/api/tools", headers=headers)).json()
                assert {tool["name"] for tool in catalog["tools"]} == set(definitions)
                assert all(harness.agent.tool_registry.get(name) is tool for name, tool in definitions.items())
                assert {"edit", "write", "shell", "delegate", "schedule_wakeup", "wake_up_in"} <= set(definitions)
                assert ("delegate" in wire) == (harness.can_delegate and not (shadowed and harness.mode == "reviewer"))
                if harness.mode == "reviewer":
                    assert not {"edit", "write", "shell", "custom_read"} & wire
                    assert not any(name.startswith("channel_") for name in wire)
                else:
                    assert {"edit", "write", "shell", "custom_read"} <= wire
                instructions = requests[-1]["instructions"]
                assert isinstance(instructions, str)
                # Match orchestration guidance to native contracts in this actual
                # provider request, not just familiar names in the editable catalog.
                if not shadowed and {"schedule_wakeup", "wake_up_in"} & wire:
                    assert "only when a scheduler tool is advertised in this request" in instructions
                    assert "Scheduling acknowledges immediately" in instructions
                    assert "Scheduling and waking grant no additional tool permissions" in instructions
                    assert "No native wake-up scheduler is advertised" not in instructions
                else:
                    assert "No native wake-up scheduler is advertised" in instructions
                    assert "do not promise delayed self-follow-up" in instructions
                    assert "Scheduling acknowledges immediately" not in instructions
                    assert "Delayed self-follow-up is available only when" not in instructions
                if not shadowed and "delegate" in wire:
                    assert "use delegate(prompt, agent='assistant')" in instructions
                    assert "Children cannot exceed your permission ceiling" in instructions
                else:
                    assert "cannot delegate using native tools in this request" in instructions
                    assert "use delegate(" not in instructions
                return wire

            async def settings(*, agent: str, depth: int) -> None:
                current = (await client.get("/api/settings", headers=headers)).json()
                values = {**current["values"], "agent": agent, "max_subagent_depth": depth}
                response = await client.post(
                    "/api/settings", headers=headers, json={"revision": current["revision"], "values": values}
                )
                assert response.status_code == 200, response.text

            assert "schedule_wakeup" in await capture()
            for agent, depth in (("reviewer", 0), ("assistant", 2), ("reviewer", 2), ("assistant", 0)):
                await settings(agent=agent, depth=depth)
                await capture()

            harness.wakeup_handler = None
            assert not {"schedule_wakeup", "wake_up_in"} & await capture()
            assert "use schedule_wakeup" not in str(requests[-1]["instructions"])
            harness.wakeup_handler = scheduler
            assert "schedule_wakeup" in await capture()

            catalog = (await client.get("/api/tools", headers=headers)).json()
            response = await client.post(
                "/api/tools",
                headers=headers,
                json={"revision": catalog["revision"], "agents": {"assistant": {"schedule_wakeup": False}}},
            )
            assert response.status_code == 200
            assert not {"schedule_wakeup", "wake_up_in"} & await capture()
            response = await client.post(
                "/api/tools", headers=headers, json={"revision": response.json()["revision"], "agents": {}}
            )
            assert response.status_code == 200
            assert "schedule_wakeup" in await capture()

            # Names never confer builtin authority on replacement plugin tools.
            for name in ("read_file", "schedule_wakeup", "wake_up_in", "delegate"):
                definitions[name] = harness.agent.register_tool(custom_read, name=name)
            shadowed = True
            harness.wakeup_handler = None
            build = await capture()
            assert {"read_file", "schedule_wakeup", "wake_up_in"} <= build
            assert "delegate" not in build  # The existing executor reserves this name at the depth limit.
            await settings(agent="assistant", depth=2)
            assert "delegate" in await capture()  # Its custom contract must not inherit native lifecycle guidance.
            await settings(agent="reviewer", depth=2)
            # This reviewer has no native delegate anymore, so all four shadows must disappear.
            assert not {"read_file", "schedule_wakeup", "wake_up_in", "delegate"} & await capture()

    asyncio.run(scenario())


@pytest.mark.parametrize("replace_prompt", [False, True])
def test_setup_plugin_instructions_survive_runs_and_scheduler_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_prompt: bool
) -> None:
    requests: list[dict[str, object]] = []
    marker = "Trusted setup owns these instructions."
    plugin = tmp_path / "prompt_plugin.py"
    operation = repr(marker) if replace_prompt else f"(harness.agent.system_prompt or '') + '\\n' + {marker!r}"
    plugin.write_text(f"def setup(harness):\n    harness.agent.system_prompt = {operation}\n")

    async def handle(request: web.Request) -> web.Response:
        requests.append(cast("dict[str, object]", await request.json()))
        return web.Response(text=sse([completion([text_item("Fixture response")])]), content_type="text/event-stream")

    async def scenario() -> None:
        config = HarnessConfig(
            tmp_path, data_dir=tmp_path / "data", providers=connection(auth="api-key"), plugins=(f"{plugin}:setup",)
        )
        async with (
            endpoint(monkeypatch, handle),
            client_app(tmp_path, config=config) as (_, client, headers, harnesses),
        ):
            harness = harnesses[0]
            harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
            harness.agent.provider = OpenAIProvider(credentials, model="fixture-native")
            authored = harness.agent.system_prompt
            assert authored and authored.endswith(marker)
            if replace_prompt:
                assert authored == marker
            scheduler = harness.wakeup_handler
            assert scheduler is not None
            for callback in (None, scheduler, None, scheduler):
                harness.wakeup_handler = callback
                count = len(requests)
                async with asyncio.timeout(HANG_GUARD):
                    response = await client.post(
                        "/api/run",
                        headers=headers,
                        json={"session_id": harness.session_id, "prompt": "Return fixture text"},
                    )
                assert response.status_code == 200, response.text
                assert not any(json.loads(line)["event"] == "error" for line in response.text.splitlines() if line)
                assert len(requests) == count + 1
                assert str(harness.agent.system_prompt).count(marker) == 1
                if replace_prompt:
                    assert harness.agent.system_prompt == authored
                assert str(requests[-1]["instructions"]).count(marker) == 1
                tools = requests[-1]["tools"]
                assert isinstance(tools, list)
                advertised = {tool["name"] for tool in tools}
                assert ("schedule_wakeup" in advertised) == (callback is not None)
                assert "wake_up_in" not in advertised
                assert harness.agent.tool_registry.get("wake_up_in") is not None
                if not replace_prompt:
                    expected = (
                        "only when a scheduler tool is advertised in this request"
                        if callback
                        else "No native wake-up scheduler is advertised"
                    )
                    assert expected in str(requests[-1]["instructions"])

    asyncio.run(scenario())
