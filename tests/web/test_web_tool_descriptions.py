"""Tools catalog metadata reaches the real native request; selection stays scoped."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

from aiohttp import web

from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.provider.openai import OpenAIProvider
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

    import pytest

DESCRIPTION = "Use the exact value supplied.  \nPreserve whitespace.\n\nNever guess a missing argument.\n\nOnly report the returned value."


def described(value: str) -> str:
    """Fallback metadata must not replace the explicit description."""
    return value


def tool_map(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    tools = payload["tools"]
    assert isinstance(tools, list)
    result = {}
    for tool in tools:
        assert isinstance(tool, dict) and isinstance(tool["name"], str)
        result[tool["name"]] = tool
    return result


def test_catalog_and_native_http_request_preserve_descriptions_and_saved_tool_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            profiles={"reviewer": AgentProfile(mode="reviewer")},
        )
        async with (
            endpoint(monkeypatch, handle),
            client_app(tmp_path, config=config) as (_, client, headers, harnesses),
        ):
            harness = harnesses[0]
            harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
            harness.agent.provider = OpenAIProvider(credentials, model="fixture-native")
            harness.agent.register_tool(described, description=DESCRIPTION)

            async def capture() -> dict[str, dict[str, object]]:
                count = len(requests)
                async with asyncio.timeout(HANG_GUARD):
                    response = await client.post(
                        "/api/run",
                        headers=headers,
                        json={"session_id": harness.session_id, "prompt": "Return fixture text only"},
                    )
                assert response.status_code == 200, response.text
                events = [json.loads(line) for line in response.text.splitlines() if line]
                assert not any(event["event"] == "error" for event in events), events
                assert len(requests) == count + 1
                return tool_map(requests[-1])

            catalog = (await client.get("/api/tools", headers=headers)).json()
            ui = tool_map(catalog)
            wire = await capture()
            assert set(wire) == set(ui) - {"wake_up_in"}
            assert harness.agent.tool_registry.get("wake_up_in") is not None
            for name, definition in wire.items():
                assert definition["description"] == ui[name]["description"]
                assert definition["parameters"] == ui[name]["parameters"]
            assert wire["described"]["description"] == DESCRIPTION
            assert "host approval policy" in str(wire["delegate"]["description"])
            assert "human approval" not in str(wire["delegate"]["description"])
            assert "host approval policy" in str(wire["channel_action"]["description"])

            saved = await client.post(
                "/api/tools",
                headers=headers,
                json={
                    "revision": catalog["revision"],
                    "agents": {
                        "assistant": {"shell": False, "schedule_wakeup": False},
                        "reviewer": {"described": False},
                    },
                },
            )
            assert saved.status_code == 200
            assert harness.config.agent == "assistant"
            selected = await capture()
            assert set(selected) == set(wire) - {"shell", "schedule_wakeup", "wake_up_in"}
            assert selected["described"]["description"] == DESCRIPTION
            # The editable catalog retains disabled tools and their full metadata.
            assert set(tool_map(saved.json())) == set(ui)
            assert tool_map(saved.json())["wake_up_in"]["alias_of"] == "schedule_wakeup"

            enabled = await client.post(
                "/api/tools", headers=headers, json={"revision": saved.json()["revision"], "agents": {}}
            )
            assert enabled.status_code == 200
            harness.config.shell_timeout = 7
            harness.config.max_output = 2048
            harness.refresh_instructions()
            refreshed = tool_map((await client.get("/api/tools", headers=headers)).json())
            current = await capture()
            assert set(current) == set(wire)
            assert current["shell"]["description"] == refreshed["shell"]["description"]
            assert "7 seconds" in str(current["shell"]["description"])
            assert "2048 bytes" in str(current["shell"]["description"])
            assert "shell always asks" not in harness.describe()

    asyncio.run(scenario())
