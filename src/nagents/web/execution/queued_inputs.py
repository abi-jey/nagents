"""One durable web/voice inbox, drained at safe model-response boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from functools import partial
from typing import TYPE_CHECKING
from typing import Literal

from fastapi import HTTPException

from nagents.harness.commands import split_command
from nagents.harness.followups import RunInput

from .._async import finish_on_cancel
from .._async import join_owned
from ..settings import skills

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nagents.harness import Harness
    from nagents.types import ContentPart

    from ..http.routing import Work
    from .service import Run
    from .service import WebState


class SkillInvocationError(ValueError):
    """An admitted skill became unavailable before its owned response boundary."""


class QueuedInputs:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.claimed: dict[str, list[Work]] = {}
        self.voice_attachments: dict[str, set[object]] = {}
        self.current: ContextVar[Work | None] = ContextVar("ngn_queued_input", default=None)

    def owns(self, run: Run, work: Work) -> bool:
        """Recognize only the exact follow-up claimed and entered by this host."""
        return (
            self.state.active is run
            and not run.finished
            and work.session_id == run.session_id
            and not work.channel
            and self.current.get() is work
            and any(claimed is work for claimed in self.claimed.get(run.id, ()))
        )

    def attach_voice(self, session_id: str, owner: object) -> None:
        self.voice_attachments.setdefault(session_id, set()).add(owner)

    def detach_voice(self, session_id: str, owner: object) -> None:
        owners = self.voice_attachments.get(session_id)
        if owners is not None:
            owners.discard(owner)
            if not owners:
                self.voice_attachments.pop(session_id)

    async def submit(
        self,
        session_id: str,
        message_id: str,
        prompt: str,
        attachments: tuple[str, ...] = (),
        supported_media_types: tuple[str, ...] = (),
        *,
        command: Literal["", "compact", "skill"] = "",
        voice_session_id: str = "",
        voice_delegation_id: str = "",
        voice_display: str = "",
    ) -> tuple[str, bool]:
        async def accept() -> tuple[str, bool]:
            if command == "skill":
                if attachments or any((voice_session_id, voice_delegation_id, voice_display)):
                    raise HTTPException(422, "Skill commands accept only a typed invocation without attachments.")
                if await self.state.channels.store.skill_retry(session_id, message_id, prompt):
                    return session_id, False
                await skills.validate(self.state, session_id, prompt)
            root, admitted = await self.state.channels.store.web(
                session_id,
                message_id,
                prompt,
                attachments,
                supported_media_types,
                command=command,
                voice_session_id=voice_session_id,
                voice_delegation_id=voice_delegation_id,
                voice_display=voice_display,
            )
            active = self.state.run_for(root)
            if (
                admitted
                and active is not None
                and active.session_id == root
                and not active.finished
                and active.message_id != message_id
                and not active.task.done()
            ):
                if (
                    self.state.harness.config.submit_mode == "interrupt"
                    and not active.voice
                    and not self.voice_attachments.get(root)
                ):
                    await self.state.stop(active)
                else:
                    harness = self.state.harness_for(root)
                    harness.followups.changed()
                    harness.tasks._changed.set()
            self.state.channels.changed.set()
            return root, admitted

        return await finish_on_cancel(accept())

    def admitted(self, run: Run, work: Work, *, initial: bool) -> None:
        if not initial:
            self.state.publish(run, {"event": "input_admitted", "message_id": work.message_id, "channel": work.channel})
        self.state.observe_run(
            run,
            {
                "event": "input_admitted",
                "root_session_id": run.session_id,
                "run_id": run.id,
                "_work": work,
                "initial": initial,
            },
        )

    @asynccontextmanager
    async def context(self, run: Run, work: Work) -> AsyncIterator[None]:
        token = self.current.set(work)
        try:
            if work.command == "skill":
                await self.state.channels.store._transaction(
                    lambda db: db.execute(
                        "UPDATE harness_sessions SET title = ? WHERE id = ? AND title = ''",
                        (" ".join(work.prompt.split())[:80], work.session_id),
                    ).close()
                )
            with self.state.history.admitted(work, run.id):
                async with (
                    self.state.history.voice_request(
                        work.session_id, run.id, work.voice_display, voice_session_id=work.voice_session_id
                    )
                    if work.voice_session_id
                    else nullcontext()
                ):
                    yield
        finally:
            self.current.reset(token)

    async def content(self, work: Work, harness: Harness) -> str | list[ContentPart]:
        if work.command != "skill":
            return await self.state.uploads.content(work, harness)
        try:
            name, arguments = split_command(work.prompt)
            return await harness.commands.skill_prompt(name, arguments, reserved=skills.WEB_RESERVED)
        except (ValueError, PermissionError, OSError) as error:
            raise SkillInvocationError(f"Skill command could not run: {error}") from None

    async def pull(self, run: Run) -> RunInput | None:
        state = self.state
        # Channel-owned replies keep their existing delivery owner. Browser
        # inputs wait behind them rather than leaking into an external reply.
        if (
            state.active is not run
            or run.finished
            or run.source
            or state.channels.closed
            or run.agent != state.session_agents.selected(run.session_id)
        ):
            return None
        while True:
            claim = asyncio.create_task(state.channels.store.claim_work(web_only=True, session_id=run.session_id))
            try:
                work = await asyncio.shield(claim)
            except asyncio.CancelledError:

                async def interrupt_claim(pending: asyncio.Task[Work | None]) -> None:
                    claimed = await pending
                    if claimed is not None:
                        await state.channels.store.release_work(claimed)

                await join_owned(asyncio.create_task(interrupt_claim(claim)))
                raise
            if work is None:
                return None
            if (
                state.active is not run
                or run.finished
                or state.channels.closed
                or run.agent != state.session_agents.selected(run.session_id)
            ):
                await state.channels.store.release_work(work)
                return None
            self.claimed.setdefault(run.id, []).append(work)
            self.admitted(run, work, initial=False)
            await state.channels.store.validate_work(work)
            try:
                prompt = await self.content(work, state.running_harness)
            except SkillInvocationError as error:
                await state.channels.store.finish_work(work, "failed")
                self.claimed[run.id].remove(work)
                state.publish(run, {"event": "error", "message": str(error), "message_id": work.message_id})
                state.channels.changed.set()
                continue
            return RunInput(
                prompt,
                work.voice_delegation_id,
                "followup",
                context=partial(self.context, run, work),
                voice_session_id=work.voice_session_id,
                message_id=work.message_id,
            )

    async def finish(self, run: Run) -> None:
        status = "interrupted" if run.outcome == "cancelled" else run.outcome
        for work in self.claimed.pop(run.id, []):
            await self.state.channels.store.finish_work(work, status)
