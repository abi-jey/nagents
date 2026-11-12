"""Native audio uses the shared Live owner without a browser lease or socket."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.audio import AudioDuplex
from nagents.live import LiveEvent
from nagents.tui.voice_audio import Microphone
from nagents.tui.voice_audio import Speaker
from nagents.web.live import runtime
from nagents.web.live.runtime import LiveService
from tests.web.live.test_web_live_runtime import End
from tests.web.live.test_web_live_runtime import FakeAgent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from nagents import Agent
    from nagents.events import Event


class NativeAgent(FakeAgent):
    async def run(self) -> AsyncGenerator[Event, None]:
        assert self.audio is not None
        self.started.set()
        try:
            await self.attach.wait()
            self.controls.sender = self.send
            yield LiveEvent(event_type="session.started")
            while True:
                item = await self.events.get()
                if isinstance(item, End):
                    return
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, LiveEvent):
                    self.controls.observe({"type": item.event_type, **item.payload})
                yield item
        finally:
            self.stopped.set()


@pytest.mark.parametrize("broken_close", [False, True])
def test_native_voice_has_no_browser_lease_and_output_failure_cannot_strand_owner(
    monkeypatch: pytest.MonkeyPatch, broken_close: bool
) -> None:
    class Output(Speaker):
        async def close(self) -> None:
            await super().close()
            if broken_close:
                raise RuntimeError("private output failure")

    async def scenario() -> None:
        agent = NativeAgent("cedar")
        source, output = Microphone(), Output()
        service = LiveService(lambda voice: cast("Agent", agent))
        try:
            created = await service.create_duplex(AudioDuplex(source, output))
            identifier = str(created["session_id"])
            assert agent.audio == AudioDuplex(source, output)
            service._active[identifier].deadline = 0
            assert (await service.snapshot(identifier))["status"] == "connected"
            result = await service.close(identifier)
            assert result["status"] == ("error" if broken_close else "closed")
            assert "private" not in str(result)
            assert not service.active_session_id and agent.closed == 1 and agent.stopped.is_set()
            assert output.closed
        finally:
            source.close()
            await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_local_attachment_joins_owner_and_closes_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(runtime, "FINALIZE_SECONDS", 0.01)
        agent = NativeAgent("cedar")
        agent.attach.clear()
        source, output = Microphone(), Speaker()
        service = LiveService(lambda voice: cast("Agent", agent))
        creating = asyncio.create_task(service.create_duplex(AudioDuplex(source, output)))
        await agent.started.wait()
        creating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creating
        assert not service.active_session_id and agent.stopped.is_set() and output.closed
        source.close()
        await service.shutdown()

    asyncio.run(scenario())
