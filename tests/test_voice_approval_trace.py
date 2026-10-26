"""Approval timeout notes expose phases and ownership, never private payloads."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.web.execution.service import Run
from tests.support.voice_approval_trace import VoiceApprovalTrace

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.runtime.core import Harness
    from nagents.web.execution.service import WebState


def test_forced_approval_timeout_reports_safe_live_tasks_and_early_bridge_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        private = f"SECRET-transcript-token-arguments-result {tmp_path}"
        gate = asyncio.Event()

        async def waiting() -> None:
            private_payload = private
            await gate.wait()
            assert private_payload

        async def failed() -> str:
            raise ValueError(private)

        worker = asyncio.create_task(waiting(), name=private)
        bridge = asyncio.create_task(failed(), name=private)
        await asyncio.gather(bridge, return_exceptions=True)
        run = Run(session_id=private, id=private)
        run.task = worker
        run.harness = cast("Harness", SimpleNamespace(_worker=worker))
        state = cast(
            "WebState",
            SimpleNamespace(
                active=run,
                channels=SimpleNamespace(
                    tasks=[worker],
                    work_tasks={private: worker},
                    work_cleanup={private: worker},
                ),
            ),
        )
        diagnostic = VoiceApprovalTrace()
        diagnostic.report({"status": "failed", "text": private, "request_input": private})
        diagnostic.report({"status": private})
        diagnostic.observe(run, {"event": "run_started", "request": private})
        diagnostic.observe(run, {"event": private})
        diagnostic.mark("scripted_generate")
        try:
            with pytest.raises(TimeoutError) as caught:
                try:
                    async with asyncio.timeout(0):
                        await asyncio.Event().wait()
                except TimeoutError as error:
                    diagnostic.note(error, state, bridge)
                    raise
            encoded = caught.value.__notes__[0]
            assert encoded.startswith("VOICE_APPROVAL_TRACE ")
            assert private not in encoded and "SECRET" not in encoded and str(tmp_path) not in encoded
            assert len(encoded) < 4096
            snapshot = json.loads(encoded.removeprefix("VOICE_APPROVAL_TRACE "))
            assert snapshot["stages"] == ["run_started", "scripted_generate"]
            assert snapshot["bridge_reports"] == ["failed"]
            assert snapshot["bridge_task"]["state"] == "done"
            assert snapshot["bridge_task"]["exception_type"] == "ValueError"
            assert snapshot["run"]["pending_approval"] is False
            assert snapshot["producer"]["state"] == "pending"
            assert snapshot["producer"]["await_chain"][:1] == ["waiting"]
        finally:
            gate.set()
            await worker

    asyncio.run(scenario())
