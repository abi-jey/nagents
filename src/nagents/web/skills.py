"""Discovered text skills exposed through ordinary, session-owned input queues."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi import Query

from nagents.designer.store import Recorder
from nagents.harness.commands import split_command

from ._async import join_owned

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable

    from fastapi import FastAPI

    from nagents.harness import Harness

    from .service import WebState

# These browser actions are additional to the client-neutral Harness builtins.
WEB_RESERVED = frozenset({"settings", "tools", "channels", "voice", "stop"})


@asynccontextmanager
async def skill_harness(state: WebState, session_id: str) -> AsyncIterator[Harness]:
    """Inspect the specified root without selecting it or initializing plugins."""
    await state.channels.store._transaction(lambda db: state.channels.store.execution_root(db, session_id))
    current = state.harness_for(session_id)
    active = state.run_for(session_id)
    agent, source = await state.designed_channels.definition(session_id)
    if not source:
        async with state.session_agents.view(session_id, next_run=True) as harness:
            yield harness
        return
    if active is not None and active.harness is not None:
        yield current
        return
    harness, installed = state.designed_channels.assemble(session_id, agent, source, Recorder())
    try:
        yield harness
    finally:
        try:
            await join_owned(asyncio.create_task(harness.close()))
        finally:
            state.channels.unregister_runtime(harness, installed)


async def catalog(state: WebState, session_id: str) -> dict[str, object]:
    async with skill_harness(state, session_id) as harness:
        await harness.agent.refresh_skills()
        enabled = harness.commands.skills_enabled()
        entries = []
        for name, skill in sorted(harness.agent.skills.items()):
            canonical = f"skill:{name}"
            direct = name not in WEB_RESERVED and harness.commands.canonical_name(name) == canonical
            entries.append(
                {
                    "name": name,
                    "description": skill.description,
                    "command": name if direct else canonical,
                    "canonical_command": canonical,
                    "enabled": enabled,
                }
            )
        return {"session_id": session_id, "agent": harness.config.agent, "enabled": enabled, "skills": entries}


async def validate(state: WebState, session_id: str, invocation: str) -> None:
    """New admission validates metadata/policy; no skill document is expanded yet."""
    try:
        name, _ = split_command(invocation)
        async with skill_harness(state, session_id) as harness:
            await harness.commands.skill_command(name, reserved=WEB_RESERVED)
    except (ValueError, PermissionError) as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(
            422, "Could not discover workspace skills. Refresh /skills and check the skill files."
        ) from None


def register(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/skills")
    async def skills(
        session_id: str = Query(min_length=1, max_length=80, pattern=r"^ngn-[A-Za-z0-9-]+$"),
    ) -> dict[str, object]:
        try:
            return await catalog(get(), session_id)
        except HTTPException:
            raise
        except (ValueError, PermissionError, OSError):
            raise HTTPException(
                422, "Could not discover workspace skills. Refresh /skills and check the skill files."
            ) from None
