"""Inspect registered tools and save agent selections in the workspace itself."""

from __future__ import annotations

import sqlite3
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


class ToolApprovalRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: str = Field(min_length=1, max_length=200)
    revision: str = Field(pattern=r"^[0-9a-f]{32}$")


class MCPRetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    server: str = Field(min_length=1, max_length=200)


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
            }
        )
    return {
        "path": ".ngn/tools.yaml",
        "revision": policy.revision,
        "agents": policy.agents,
        "active_agent": harness.config.agent,
        "profiles": [{"id": name, "mode": harness.mode_for_profile(name)} for name in profiles],
        "tools": sorted(tools, key=lambda item: str(item["name"])),
        "tool_approvals": state.tool_approvals.snapshot(),
        "mcp_diagnostics": harness.resources.mcp_diagnostics().snapshot(),
    }


def register(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/tools")
    async def tools() -> dict[str, object]:
        try:
            return snapshot(get())
        except (ValueError, OSError, sqlite3.Error, yaml.YAMLError):
            raise HTTPException(422, "Cannot read tool settings. Check .ngn/tools.yaml in the workspace.") from None

    @app.post("/api/tools")
    async def save(body: ToolSettingsInput) -> dict[str, object]:
        state = get()
        async with state.queued_mutation():
            try:
                state.harness.tool_settings.save(body.agents, body.revision)
                return snapshot(state)
            except FileExistsError:
                raise HTTPException(409, "Tool settings changed on disk. Refresh before saving.") from None
            except (ValueError, OSError, sqlite3.Error, yaml.YAMLError):
                raise HTTPException(
                    422, "Cannot save tool settings. Check the selections and workspace configuration file."
                ) from None

    @app.post("/api/tools/approvals/revoke")
    async def revoke(body: ToolApprovalRevoke) -> dict[str, object]:
        state = get()
        async with state.queued_mutation():
            try:
                if not state.tool_approvals.revoke(body.tool, body.revision):
                    raise HTTPException(409, "This saved permission changed. Refresh tools before trying again.")
                return snapshot(state)
            except sqlite3.Error:
                raise HTTPException(503, "Could not remove the saved permission. Refresh and retry.") from None

    @app.post("/api/tools/mcp/retry")
    async def retry_mcp(body: MCPRetryInput) -> dict[str, object]:
        state = get()
        # This changes only retry eligibility. Do not detach the settings
        # runtime: doing so would initialize another copy of every server.
        # The existing auxiliary lease also owns the accepted harness lifetime:
        # settings may detach it, and shutdown cancels/joins this request.
        async with state.executions.auxiliary_provider():
            primary = state.harness
            with state.idle(allow_running=True):
                if not any(
                    failure.name == body.server for failure in primary.resources.mcp_diagnostics().failed_servers
                ):
                    raise HTTPException(409, "This MCP server is no longer unavailable. Refresh tools before retrying.")
                owners = {primary, *state.executions.owners.values()}
                targets = [
                    owner for owner in owners if primary.resources.mcp_retry_matches(body.server, owner.resources)
                ]
                try:
                    for owner in targets:
                        owner.resources.request_mcp_retry(body.server)
                except (ValueError, RuntimeError):
                    raise HTTPException(409, "MCP discovery changed. Refresh tools before retrying.") from None
                # Retained lanes own their next response/tool boundary or run.
                # In-flight tools keep the connection they were given.
                apply_primary = not state.executions.in_use(primary) and not primary._busy
                queued = len(targets) > 1 or not apply_primary
            # No host mutation lock while a connection starts. New messages can
            # be admitted and use the resource reload lock at their own boundary.
            if apply_primary:
                await primary.resources.reload()
            result = snapshot(state)
            result["mcp_retry"] = {"server": body.server, "status": "queued" if queued else "completed"}
            return result
