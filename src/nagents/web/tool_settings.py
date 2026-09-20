"""Inspect registered tools and save agent selections in the workspace itself."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.harness.tools import MODIFYING_TOOLS
from nagents.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from .service import WebState


class ToolSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(max_length=64)
    agents: dict[str, dict[str, bool]]


def snapshot(state: WebState) -> dict[str, object]:
    harness = state.harness
    policy = harness.tool_settings
    policy.load()
    profiles = harness.config.profile_names
    tools = []
    for tool in ToolRegistry.get_all(harness.agent.tool_registry):
        builtin = tool.func is not None and tool.func == harness.tools.builtins.get(tool.name)
        category = (
            "Files"
            if tool.name in {"read_file", "list_files", "find", "search", "edit", "write"}
            else "Channels"
            if "channel" in tool.name
            else "MCP"
            if tool.name.startswith("mcp__")
            else "Agent"
            if builtin
            else "Extensions"
        )
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "category": category,
                "builtin": builtin,
                "reviewer_allowed": builtin and tool.name not in MODIFYING_TOOLS,
                "alias_of": "schedule_wakeup" if tool.name == "wake_up_in" else "",
            }
        )
    return {
        "path": ".ngn/tools.yaml",
        "revision": policy.revision,
        "agents": policy.agents,
        "active_agent": harness.config.agent,
        "profiles": [{"id": name, "mode": harness.mode_for_profile(name)} for name in profiles],
        "tools": sorted(tools, key=lambda item: str(item["name"])),
    }


def register(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/tools")
    async def tools() -> dict[str, object]:
        try:
            return snapshot(get())
        except (ValueError, OSError, yaml.YAMLError):
            raise HTTPException(422, "Cannot read tool settings. Check .ngn/tools.yaml in the workspace.") from None

    @app.post("/api/tools")
    async def save(body: ToolSettingsInput) -> dict[str, object]:
        state = get()
        with state.idle():
            try:
                state.harness.tool_settings.save(body.agents, body.revision)
                return snapshot(state)
            except FileExistsError:
                raise HTTPException(409, "Tool settings changed on disk. Refresh before saving.") from None
            except (ValueError, OSError, yaml.YAMLError):
                raise HTTPException(
                    422, "Cannot save tool settings. Check the selections and workspace configuration file."
                ) from None
