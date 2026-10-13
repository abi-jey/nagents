"""Durable conversation profiles, independent of workspace defaults and active runs."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import TypedDict

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.harness.reasoning import resolve_effort
from nagents.provider.context import ContextMetadata
from nagents.tools.registry import ToolRegistry

from ._async import finish_on_cancel
from ._async import join_owned
from .routing import RoutingStore
from .session_actions import RootId  # noqa: TC001

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from collections.abc import Callable

    from nagents.harness import Harness

    from .service import Run
    from .service import WebState


class AgentIdentity(TypedDict):
    agent: str
    model: str
    provider: str
    provider_kind: str
    mode: str
    reasoning_effort: str


@dataclass(frozen=True)
class Choice:
    agent: str
    revision: str


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: str = Field(min_length=1, max_length=128)
    agent: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def initialize_markers(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_web_agent_changes ("
        "id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, anchor_history_id INTEGER NOT NULL, "
        "agent TEXT NOT NULL, previous_agent TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_web_agent_changes_clear BEFORE DELETE ON v2_messages BEGIN "
        "DELETE FROM ngn_web_agent_changes WHERE session_id = OLD.session_id "
        "AND anchor_history_id = OLD.id; END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS ngn_web_agent_changes_delete BEFORE DELETE ON v2_sessions BEGIN "
        "DELETE FROM ngn_web_agent_changes WHERE session_id = OLD.id; END"
    )


def marker(row: tuple[object, ...]) -> dict[str, object]:
    identifier, anchor, agent, previous, model, provider = row
    return {
        "role": "agent_changed",
        "event": "agent_changed",
        "history_id": f"agent-change-{identifier}",
        "anchor_history_id": str(anchor),
        "agent": agent,
        "previous_agent": previous,
        "model": model,
        "provider": provider,
        "content": f"Agent: {agent}",
        "name": "",
        "tool_call_id": "",
        "tool_calls": [],
    }


def merge_markers(db: sqlite3.Connection, session_id: str, records: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = db.execute(
        "SELECT id, anchor_history_id, agent, previous_agent, model, provider "
        "FROM ngn_web_agent_changes WHERE session_id = ? ORDER BY anchor_history_id, id",
        (session_id,),
    ).fetchall()
    changes = [marker(tuple(row)) for row in rows]
    result: list[dict[str, object]] = []
    # A change is anchored after the last persisted message before its run.
    # Keep it out of v2_messages: it is never sent to a model or voice seed.
    for record in records:
        identifier = str(record.get("history_id", ""))
        if identifier.isdigit():
            while changes and int(str(changes[0]["anchor_history_id"])) < int(identifier):
                result.append(changes.pop(0))
        result.append(record)
    return [*result, *changes]


class SessionAgents:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.choices: dict[str, Choice] = {}
        self._metadata: dict[tuple[str, str, str], ContextMetadata] = {}

    async def initialize(self) -> None:
        default = self.state.settings.values.agent

        def create(db: sqlite3.Connection) -> dict[str, Choice]:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_session_agents (session_id TEXT PRIMARY KEY, "
                "agent TEXT NOT NULL, revision TEXT NOT NULL, applied_agent TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS ngn_web_session_agents_delete BEFORE DELETE ON v2_sessions BEGIN "
                "DELETE FROM ngn_web_session_agents WHERE session_id = OLD.id; END"
            )
            db.execute(
                "INSERT OR IGNORE INTO ngn_web_session_agents "
                "SELECT h.id, ?, lower(hex(randomblob(32))), ? FROM harness_sessions h "
                "JOIN v2_sessions s ON s.id = h.id",
                (default, default),
            )
            return {
                str(row[0]): Choice(str(row[1]), str(row[2]))
                for row in db.execute("SELECT session_id, agent, revision FROM ngn_web_session_agents")
            }

        self.choices.update(await self.state.channels.store._transaction(create))

    def selected(self, session_id: str) -> str:
        choice = self.choices.get(session_id)
        return choice.agent if choice else self.state.harness.config.agent

    async def ensure(self, session_id: str) -> Choice:
        active = self.state.run_for(session_id)
        default = active.agent if active is not None and active.agent else self.state.settings.values.agent

        def read(db: sqlite3.Connection) -> Choice:
            RoutingStore.root(db, session_id)
            db.execute(
                "INSERT OR IGNORE INTO ngn_web_session_agents VALUES (?, ?, ?, ?)",
                (session_id, default, secrets.token_hex(32), default),
            )
            row = db.execute(
                "SELECT agent, revision FROM ngn_web_session_agents WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert row is not None
            return Choice(str(row[0]), str(row[1]))

        previous = self.choices.get(session_id)
        choice = await self.state.channels.store._transaction(read)
        current = self.choices.get(session_id)
        if current is not None and current != previous:
            return current
        self.choices[session_id] = choice
        return choice

    def reset_draft_in(self, db: sqlite3.Connection, session_id: str) -> tuple[Choice, bool]:
        """Reset a caller-verified empty draft inside its selection transaction."""
        default = self.state.settings.values.agent
        row = db.execute(
            "SELECT agent, revision, applied_agent FROM ngn_web_session_agents WHERE session_id = ?", (session_id,)
        ).fetchone()
        changed = row is not None and (row[0] != default or row[2] != default)
        if row is not None and not changed:
            return Choice(str(row[0]), str(row[1])), False
        choice = Choice(default, secrets.token_hex(32))
        db.execute(
            "INSERT INTO ngn_web_session_agents VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "agent = excluded.agent, revision = excluded.revision, applied_agent = excluded.applied_agent",
            (session_id, choice.agent, choice.revision, choice.agent),
        )
        db.execute("DELETE FROM ngn_web_agent_changes WHERE session_id = ?", (session_id,))
        return choice, changed

    async def notify(self, session_id: str) -> dict[str, object]:
        result = await self.snapshot(session_id)
        self.state.bus.event(
            {
                "event": "agent_selection_changed",
                "schema_version": 1,
                "session_id": session_id,
                "agent_selection": result,
            }
        )
        return result

    def identity(self, name: str) -> AgentIdentity:
        harness = self.state.harness
        if name not in harness.config.profile_names:
            return {
                "agent": name,
                "model": "",
                "provider": "",
                "provider_kind": "",
                "mode": "reviewer",
                "reasoning_effort": "",
            }
        profile = harness.config.profile(name)
        provider_id = profile.provider or harness.providers.active or harness.config.provider
        provider = harness.providers.providers.get(provider_id) or harness.config.provider_profile()
        return {
            "agent": name,
            "model": profile.model or self.state.settings.values.model,
            "provider": provider_id or provider.kind,
            "provider_kind": provider.kind,
            "mode": harness.mode_for_profile(name),
            "reasoning_effort": resolve_effort(profile.reasoning_effort, self.state.settings.values.reasoning_effort),
        }

    @staticmethod
    def actual(harness: Harness) -> AgentIdentity:
        return {
            "agent": harness.config.agent,
            "model": harness.agent.provider.model,
            "provider": harness.config.provider or harness.config.provider_profile().kind,
            "provider_kind": harness.config.provider_profile().kind,
            "mode": harness.mode,
            "reasoning_effort": harness.reasoning_effort(),
        }

    def target(self, session_id: str) -> AgentIdentity:
        active = self.state.run_for(session_id)
        if active is not None:
            if active.harness is not None:
                return self.actual(active.harness)
            identity = self.identity(active.agent or self.selected(session_id))
            if active.read_only:
                identity["mode"] = "reviewer"
            return identity
        return self.identity(self.selected(session_id))

    def unavailable(self, name: str) -> str:
        harness = self.state.harness
        if name not in harness.config.profile_names:
            return "This agent profile is no longer available. Choose another profile."
        profile = harness.config.profile(name)
        if not profile.user_invocable:
            return "This profile is available only for delegation."
        if profile.provider and profile.provider not in harness.providers.providers:
            return "The profile's provider connection is unavailable."
        return ""

    async def snapshot(self, session_id: str) -> dict[str, object]:
        choice = await self.ensure(session_id)
        pinned_agent, source = await self.state.designed_channels.preview(session_id)
        locked = "This conversation uses a pinned Agent Designer route." if source else ""
        active = self.state.run_for(session_id)
        identity = (
            self.identity(choice.agent)
            if not self.unavailable(choice.agent)
            else {"agent": choice.agent, "model": "", "provider": "", "provider_kind": "", "mode": "reviewer"}
        )
        if source:
            from nagents.designer.store import Recorder

            harness, installed = self.state.designed_channels.assemble(session_id, pinned_agent, source, Recorder())
            try:
                identity = self.actual(harness)
            finally:
                await harness.close()
                self.state.channels.unregister_runtime(harness, installed)
        return {
            "session_id": session_id,
            "revision": choice.revision,
            "agent": choice.agent,
            "workspace_default": self.state.settings.values.agent,
            "effective": identity,
            "active": self.target(session_id) if active is not None else None,
            "pending": bool(active is not None and (active.agent or choice.agent) != choice.agent),
            "locked_reason": locked,
            "profiles": [
                {
                    "name": name,
                    "description": self.state.harness.config.profile(name).description,
                    **{key: value for key, value in self.identity(name).items() if key != "agent"},
                    "disabled_reason": locked or self.unavailable(name),
                }
                for name in self.state.harness.config.profile_names
            ],
        }

    async def change(self, session_id: str, body: AgentInput) -> dict[str, object]:
        async with self.state.queued_mutation():
            previous = await self.snapshot(session_id)
            if previous["locked_reason"]:
                raise HTTPException(409, str(previous["locked_reason"]))
            if reason := self.unavailable(body.agent):
                raise HTTPException(422, reason)
            choice = Choice(body.agent, secrets.token_hex(32))
            restrictive = self.identity(choice.agent)["mode"] == "reviewer"
            if restrictive:
                # Detach before commit so tightening is immediate once the
                # selection is saved, without narrowing workspace defaults.
                await self.state.executions.configuration_owner()

            def save(db: sqlite3.Connection) -> None:
                RoutingStore.root(db, session_id)
                changed = db.execute(
                    "UPDATE ngn_web_session_agents SET agent = ?, revision = ? WHERE session_id = ? AND revision = ?",
                    (choice.agent, choice.revision, session_id, body.revision),
                ).rowcount
                if not changed:
                    raise HTTPException(409, "Conversation agent changed. Refresh before choosing again.")

            await self.state.channels.store._transaction(save)
            self.choices[session_id] = choice
            active = self.state.run_for(session_id)
            if restrictive:
                if active is not None:
                    self.tighten(active)
                elif owner := self.state.executions.owners.get(session_id):
                    self.tighten_owner(owner)
            result = await self.notify(session_id)
            self.state.changed()
            return result

    def tighten(self, run: Run) -> None:
        run.read_only = True
        if run.harness is not None:
            self.tighten_owner(run.harness)
        if run.pending is not None and not run.pending.answer.done():
            run.pending.answer.set_result(False)

    @staticmethod
    def tighten_owner(owner: Harness) -> None:
        # Retain the execution's prompt and provider; narrow only future tool
        # authority. Children keep this ceiling even after a later root run.
        # Every manager references the root's flat child map; traversing each
        # child's map recursively would revisit the same children forever.
        for harness in {owner, *owner.tasks._children.values()}:
            harness._permission_ceiling = "reviewer"

    async def apply(self, run: Run, harness: Harness) -> None:
        choice = await self.ensure(run.session_id)
        name = run.agent or choice.agent
        run.agent = name
        if reason := self.unavailable(name):
            raise HTTPException(422, reason)
        # A shared configuration runtime must keep its workspace identity.
        if harness.config.agent != name:
            await harness.set_agent(name)
        if run.read_only:
            await self.state.executions.configuration_owner()
            self.tighten(run)
        identity = self.actual(harness)

        def applied(db: sqlite3.Connection) -> dict[str, object]:
            row = db.execute(
                "SELECT applied_agent FROM ngn_web_session_agents WHERE session_id = ?", (run.session_id,)
            ).fetchone()
            if row is None or row[0] == name:
                return {}
            anchor = int(
                db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM v2_messages WHERE session_id = ?", (run.session_id,)
                ).fetchone()[0]
            )
            inserted = db.execute(
                "INSERT INTO ngn_web_agent_changes (session_id, anchor_history_id, agent, previous_agent, model, provider) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run.session_id, anchor, name, row[0], identity["model"], identity["provider"]),
            ).lastrowid
            db.execute(
                "UPDATE ngn_web_session_agents SET applied_agent = ? WHERE session_id = ?", (name, run.session_id)
            )
            return marker((inserted, anchor, name, row[0], identity["model"], identity["provider"]))

        record = await self.state.channels.store._transaction(applied)
        if record:
            self.state.publish(run, record)

    @asynccontextmanager
    async def view(self, session_id: str, *, next_run: bool = False) -> AsyncIterator[Harness]:
        choice = await self.ensure(session_id)
        current = self.state.harness_for(session_id)
        active = self.state.run_for(session_id)
        if not next_run and active is not None and active.harness is not None:
            yield active.harness
            return
        design_agent, source = await self.state.designed_channels.preview(session_id)
        if source:
            from nagents.designer.store import Recorder

            designed, installed = self.state.designed_channels.assemble(session_id, design_agent, source, Recorder())
            try:
                await self.state.executions.borrow_auth(designed)
                yield designed
            finally:
                await join_owned(asyncio.create_task(designed.close()))
                self.state.channels.unregister_runtime(designed, installed)
            return
        name = active.agent if active is not None and active.agent and not next_run else choice.agent
        if reason := self.unavailable(name):
            raise HTTPException(422, reason)
        expected = self.identity(name)
        restricted = active is not None and active.read_only and not next_run
        if restricted:
            expected["mode"] = "reviewer"
        primary = self.state.harness
        provider_id = primary.config.profile(name).provider or primary.providers.active or primary.config.provider
        accepted_connection = primary.providers.providers.get(provider_id) or primary.config.provider_profile()
        current_connection = (
            current._built_provider_name == provider_id and current._built_provider_profile == accepted_connection
        )
        # The immutable built profile includes endpoint and authentication
        # fields: equal connection IDs/model labels do not prove equal routes.
        # A retained lane may still own children from an older endpoint or auth
        # configuration under the same connection ID/model. Inspect the saved
        # next-run route without mutating that lane or its children.
        if self.actual(current) == expected and current_connection and (active is None or not next_run):
            yield current
            return
        harness = self.state.executions.factory(deepcopy(self.state.harness.config))
        if harness is self.state.harness or harness in self.state.executions.owners.values():
            raise RuntimeError("Session inspection requires an independent runtime")
        try:
            harness._permission_ceiling = self.state.settings.harness._permission_ceiling
            harness.allow_subagents = self.state.harness.allow_subagents
            await self.state.executions.borrow_auth(harness)
            harness.agent.session = self.state.history
            harness.session_id = session_id
            harness.wakeup_handler = self.state.schedule
            # Reuse only registered schemas for inspection; do not initialize
            # extensions, connect MCP servers, or register another host owner.
            for tool in ToolRegistry.get_all(self.state.harness.agent.tool_registry):
                if tool.func is not None and harness.agent.tool_registry.get(tool.name) is None:
                    harness.agent.tool_registry.register(
                        tool.func, name=tool.name, description=tool.description, parameters=deepcopy(tool.parameters)
                    )
            self.state.settings.values.apply(harness)
            await harness.set_agent(name)
            if restricted:
                harness._permission_ceiling = "reviewer"
            harness.load_project_instructions()
            await harness.agent.refresh_skills()
            harness.refresh_instructions()
            key = (session_id, name, repr(harness.config.provider_profile()))
            harness.agent.provider.restore_context_metadata(self._metadata.get(key, ContextMetadata()))
            yield harness
            while len(self._metadata) >= 32:
                self._metadata.pop(next(iter(self._metadata)))
            self._metadata[key] = harness.agent.provider.context_metadata_snapshot()
        finally:
            await join_owned(asyncio.create_task(harness.close()))


def register(app: FastAPI, state: Callable[[], WebState]) -> None:
    @app.get("/api/sessions/{session_id}/agent")
    async def get_agent(session_id: RootId) -> dict[str, object]:
        return await state().session_agents.snapshot(session_id)

    @app.post("/api/sessions/{session_id}/agent")
    async def select_agent(session_id: RootId, body: AgentInput) -> dict[str, object]:
        return await finish_on_cancel(state().session_agents.change(session_id, body))
