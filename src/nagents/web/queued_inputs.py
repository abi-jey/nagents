"""One durable web/voice inbox, drained at safe model-response boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import nullcontext
from typing import TYPE_CHECKING
from typing import Literal

from nagents.harness.followups import RunInput

from ._async import finish_on_cancel
from ._async import join_owned

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .routing import Work
    from .service import Run
    from .service import WebState


class QueuedInputs:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.claimed: dict[str, list[Work]] = {}
        self.voice_attachments: dict[str, set[object]] = {}

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
        command: Literal["", "compact"] = "",
        voice_session_id: str = "",
        voice_delegation_id: str = "",
        voice_display: str = "",
    ) -> tuple[str, bool]:
        async def accept() -> tuple[str, bool]:
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
            active = self.state.active
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
                    self.state.running_harness.followups.changed()
                    self.state.running_harness.tasks._changed.set()
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
                "session_id": run.session_id,
                "run_id": run.id,
                "_work": work,
                "initial": initial,
            },
        )

    @asynccontextmanager
    async def context(self, run: Run, work: Work) -> AsyncIterator[None]:
        with self.state.history.admitted(work, run.id):
            async with (
                self.state.history.voice_request(
                    work.session_id, run.id, work.voice_display, voice_session_id=work.voice_session_id
                )
                if work.voice_session_id
                else nullcontext()
            ):
                yield

    async def pull(self, run: Run) -> RunInput | None:
        state = self.state
        # Channel-owned replies keep their existing delivery owner. Browser
        # inputs wait behind them rather than leaking into an external reply.
        if state.active is not run or run.finished or run.source or state.channels.closed:
            return None
        claim = asyncio.create_task(state.channels.store.claim_work(web_only=True, session_id=run.session_id))
        try:
            work = await asyncio.shield(claim)
        except asyncio.CancelledError:

            async def interrupt_claim() -> None:
                claimed = await claim
                if claimed is not None:
                    await state.channels.store.release_work(claimed)

            await join_owned(asyncio.create_task(interrupt_claim()))
            raise
        if work is None:
            return None
        if state.active is not run or run.finished or state.channels.closed:
            await state.channels.store.release_work(work)
            return None
        self.claimed.setdefault(run.id, []).append(work)
        self.admitted(run, work, initial=False)
        await state.channels.store.validate_work(work)
        prompt = await state.uploads.content(work, state.running_harness)
        return RunInput(
            prompt,
            work.voice_delegation_id,
            "followup",
            context=lambda: self.context(run, work),
            voice_session_id=work.voice_session_id,
        )

    async def finish(self, run: Run) -> None:
        status = "interrupted" if run.outcome == "cancelled" else run.outcome
        for work in self.claimed.pop(run.id, []):
            await self.state.channels.store.finish_work(work, status)
