"""Spoken input remains data, with one handoff and parent-only spoken results."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolResultEvent
from nagents.harness.runtime.followups import RunTurn
from nagents.live.delegation import ClientDelegationRequest
from nagents.tui.voice_bridge import TuiVoiceBridge

if TYPE_CHECKING:
    from nagents.live.delegation import LiveAppendKind
    from nagents.tui.voice import VoicePrompt


def test_requests_deduplicate_caller_fragments_and_never_delegate_startup_data() -> None:
    async def scenario() -> None:
        prompts: list[VoicePrompt] = []
        reports: list[dict[str, object]] = []

        async def submit(prompt: VoicePrompt) -> None:
            prompts.append(prompt)

        bridge = TuiVoiceBridge(
            "ngn-test", "call", submit, reports.append, agent="assistant", provider="text", model="normal"
        )
        empty = ClientDelegationRequest("empty", transcript="[]")
        await bridge.handle_request(empty)
        assert not prompts
        transcript = json.dumps([{"speaker": "user", "text": "Hello", "start_ms": 0, "end_ms": 100}])
        hello = ClientDelegationRequest("hello", transcript=transcript)
        await asyncio.gather(bridge.handle_request(hello), bridge.handle_request(hello))
        await bridge.handle_request(ClientDelegationRequest("repeated", transcript=transcript))
        await bridge.handle_request(ClientDelegationRequest("native", "/quit", "[]"))
        assert [prompt.text for prompt in prompts] == ["Hello", "/quit"]
        assert all(prompt.session_id == "ngn-test" and prompt.bridge is bridge for prompt in prompts)
        await bridge.handle_request(ClientDelegationRequest("invalid", transcript="not JSON"))
        assert reports[-1]["status"] == "failed"
        await bridge.close()
        await bridge.handle_request(ClientDelegationRequest("late", "Do not run", "[]"))
        assert len(prompts) == 2

    asyncio.run(scenario())


def test_verified_updates_exclude_reasoning_compaction_tools_and_children() -> None:
    async def scenario() -> None:
        reports: list[dict[str, object]] = []
        sent: list[tuple[str, str, str]] = []
        accepted: list[VoicePrompt] = []

        async def submit(prompt: VoicePrompt) -> None:
            accepted.append(prompt)

        async def sink(kind: LiveAppendKind, text: str, identifier: str) -> str:
            sent.append((kind, text, identifier))
            return "session." + kind + ".append"

        bridge = TuiVoiceBridge(
            "ngn-test", "call", submit, reports.append, agent="assistant", provider="text", model="normal"
        )
        bridge.attach(sink)
        await bridge.handle_request(ClientDelegationRequest("request", "Question", "[]"))
        bridge.begin("run", accepted[0])
        bridge.turn(RunTurn("turn", "", "human", ()))
        bridge.event(ReasoningChunkEvent(chunk="PRIVATE REASONING"))
        bridge.event(ToolResultEvent(name="shell", result="PRIVATE TOOL OUTPUT"))
        bridge.event(CompactionStartedEvent())
        bridge.event(TextDoneEvent(text="PRIVATE SUMMARY"))
        bridge.event(CompactionDoneEvent())
        bridge.event(TextDoneEvent(text="PRIVATE CHILD", extra={"session_id": "child"}))
        bridge.event(TextDoneEvent(text="Verified parent answer"))
        bridge.event(TextDoneEvent(text="Verified parent answer"))
        bridge.event(DoneEvent(final_text="Verified parent answer"))
        async with asyncio.timeout(2):
            while not any(kind == "commentary" for kind, _, _ in sent):
                await asyncio.sleep(0)
        assert [item for item in sent if item[0] == "commentary"] == [
            ("commentary", "Verified parent answer", "request")
        ]
        assert "PRIVATE" not in str(sent)
        bridge.finish("run")
        assert reports[-1]["status"] == "completed" and reports[-1]["run_id"] == "run"
        await bridge.close()
        assert bridge._worker is not None and bridge._worker.done()

    asyncio.run(scenario())


@pytest.mark.parametrize("overflow", [False, True])
def test_stalled_or_failed_voice_updates_do_not_block_assistant_work(overflow: bool) -> None:
    async def scenario() -> None:
        async def submit(prompt: VoicePrompt) -> None:
            pass

        async def sink(kind: LiveAppendKind, text: str, identifier: str) -> str:
            raise RuntimeError("private transport error")

        bridge = TuiVoiceBridge(
            "ngn-test", "call", submit, lambda report: None, agent="assistant", provider="text", model="normal"
        )
        bridge.attach(sink)
        for _ in range(129 if overflow else 1):
            bridge.append("commentary", "Verified update")
        async with asyncio.timeout(2):
            while not bridge.failed:
                await asyncio.sleep(0)
        assert bridge.closed
        await bridge.close()

    asyncio.run(scenario())
