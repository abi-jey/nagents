"""Offline lifecycle tests for the browser-owned-media Live session manager."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import anyio
import pytest
from aiohttp import web
from fastapi import HTTPException

from nagents import Agent
from nagents import AudioDuplex
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import AudioChunkEvent
from nagents.events import AudioTranscriptDeltaEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import InputTranscriptDeltaEvent
from nagents.events import TextChunkEvent
from nagents.live import LiveConfig
from nagents.live import LiveEvent
from nagents.live.controls import LiveControls
from nagents.web import live_runtime as runtime
from nagents.web.live_runtime import LiveService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import Callable

    from nagents.events import Event

Payload = dict[str, object]
SECRET = "sk-server-only-test-credential"
OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
ANSWER = OFFER.replace("o=- 1 1", "o=- 2 2")
WAIT = 3.0


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(WAIT):
        while not predicate():
            await asyncio.sleep(0.001)


class End:
    """End a fake stream without conflating backend DoneEvent with session end."""


class FakeAgent:
    def __init__(self, voice: str) -> None:
        self.provider = Provider(
            ProviderType.OPENAI_COMPATIBLE,
            model="gpt-live-1",
            api_key=SECRET,
            live_config=LiveConfig(voice=voice or "marin", close_session_on_exit=False),
        )
        self.agent = Agent(
            provider=self.provider,
            system_prompt="A dedicated hosted voice assistant.",
            session_manager=SessionManager(Path(":memory:")),
        )
        self.tool_registry = self.agent.tool_registry
        self.delegation_agent: Agent | None = None
        self.audio: AudioDuplex | None = AudioDuplex()
        self.controls = LiveControls()
        self.events: asyncio.Queue[Event | Exception | End] = asyncio.Queue()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.attach = asyncio.Event()
        self.attach.set()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.closed = 0
        self.cancelled = 0
        self.sent: list[Payload] = []
        self.finalize_on_close = True
        self.finalize_on_drain = False
        self.fail_close_command = False
        self.fail_resource_close = False
        self.attachment_id = ""

    @property
    def live(self) -> LiveControls:
        if not self.started.is_set():
            raise RuntimeError("No Live run")
        return self.controls

    def live_configuration(self, *, media: bool = False) -> Payload:
        return self.agent.live_configuration(media=media)

    async def run(self) -> AsyncGenerator[Event, None]:
        config = self.provider.live_config
        assert config is not None and config.attach_to
        assert self.audio is None
        assert not config.handle_delegations
        assert config.close_session_on_exit
        self.started.set()
        try:
            await self.attach.wait()
            self.controls.sender = self.send
            attached: Payload = {"session": {"id": self.attachment_id or config.attach_to}}
            self.controls.observe(attached)
            yield LiveEvent(event_type="connection.attached", payload=attached)
            while True:
                item = await self.events.get()
                if isinstance(item, End):
                    return
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, LiveEvent):
                    self.controls.observe({"type": item.event_type, **item.payload})
                yield item
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            if self.finalize_on_drain:
                # Native Agent.run can drain final usage after it stops emitting
                # events, leaving confirmation only in the retained controls.
                self.controls.observe({"type": "session.closed"})
            self.stopped.set()

    async def send(self, event: Payload) -> None:
        self.sent.append(event)
        if self.fail_close_command:
            raise RuntimeError(SECRET)
        if event["type"] == "session.close" and self.finalize_on_close:
            self.events.put_nowait(LiveEvent(event_type="session.closed", payload={"reason": SECRET}))
            self.events.put_nowait(End())

    async def close(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed += 1
        await self.agent.close()
        if self.fail_resource_close:
            raise RuntimeError(SECRET)


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents: list[FakeAgent] = []
        self.voices: list[str] = []
        self.requests: list[tuple[str, Payload]] = []
        self.hangups: list[str] = []
        self.provision_started = asyncio.Event()
        self.provision_release = asyncio.Event()
        self.provision_release.set()
        self.hangup_started = asyncio.Event()
        self.hangup_release = asyncio.Event()
        self.hangup_release.set()
        self.provision_error = False
        self.hangup_error = False
        self.response: object = "default"
        self.configure: Callable[[FakeAgent], None] = lambda agent: None
        monkeypatch.setattr(runtime, "LiveAPI", lambda provider: self)
        monkeypatch.setattr(runtime, "PROVISION_SECONDS", 0.25)
        monkeypatch.setattr(runtime, "ATTACH_SECONDS", 0.1)
        monkeypatch.setattr(runtime, "FINALIZE_SECONDS", 0.02)
        monkeypatch.setattr(runtime, "CLEANUP_SECONDS", 0.1)
        monkeypatch.setattr(runtime, "LEASE_SECONDS", 1.0)
        self.service = LiveService(self.factory)

    def factory(self, voice: str) -> Agent:
        self.voices.append(voice)
        agent = FakeAgent(voice)
        self.configure(agent)
        self.agents.append(agent)
        return cast("Agent", agent)

    async def create_webrtc(self, sdp: str, session: Payload) -> Payload:
        self.requests.append((sdp, session))
        self.provision_started.set()
        await self.provision_release.wait()
        if self.provision_error:
            raise RuntimeError(SECRET)
        if self.response != "default":
            return cast("Payload", self.response)
        return {
            "session": {"id": f"native-{len(self.requests)}", "client_secret": SECRET},
            "transport": {"sdp": ANSWER, "secret": SECRET},
            "api_key": SECRET,
        }

    async def hangup(self, session_id: str) -> None:
        self.hangups.append(session_id)
        self.hangup_started.set()
        await self.hangup_release.wait()
        if self.hangup_error:
            raise RuntimeError(SECRET)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


async def create(service: LiveService, voice: str = "") -> str:
    result = await service.create(OFFER, voice)
    assert set(result) == {"session_id", "sdp", "model", "voice"}
    assert result["sdp"] == ANSWER
    assert result["model"] == "gpt-live-1"
    identifier = result["session_id"]
    assert isinstance(identifier, str) and identifier and not identifier.startswith("native-")
    return identifier


def test_create_attaches_fresh_hosted_agent_and_close_is_confirmed_and_idempotent(rig: Rig) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service, "cedar")
        assert rig.voices == ["cedar"]
        offer, config = rig.requests[0]
        assert offer == OFFER
        assert config["model"] == "gpt-live-1"
        assert config["instructions"] == "A dedicated hosted voice assistant."
        assert config["audio"] == {"output": {"voice": "cedar"}}
        delegation = cast("Payload", config["delegation"])
        assert delegation["type"] == "responses"
        assert cast("Payload", delegation["responses"])["tools"] == []
        agent = rig.agents[0]
        assert agent.provider.live_config is not None
        assert agent.provider.live_config.attach_to == "native-1"
        assert agent.audio is None and not agent.sent
        assert (await rig.service.snapshot(identifier))["status"] == "connected"
        result = await rig.service.close(identifier)
        assert result["status"] == "closed"
        assert "Finalization confirmed" in str(result["message"])
        assert agent.stopped.is_set() and agent.closed == 1
        assert [command["type"] for command in agent.sent] == ["session.close"]
        assert not rig.hangups
        assert await rig.service.close(identifier) == result
        assert SECRET not in json.dumps(result)
        await rig.service.shutdown()
        assert agent.closed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "sdp",
    [None, 10, {}, "", " ", "offer", "v=0bad", "v=0\r\n\x00", "v=0\n" + "x" * 65536, "v=0\n" + "é" * 40000],
    ids=[
        "null",
        "number",
        "object",
        "empty",
        "blank",
        "not-sdp",
        "bad-version",
        "control",
        "too-long",
        "too-many-bytes",
    ],
)
def test_create_rejects_invalid_offers_before_factory(rig: Rig, sdp: object) -> None:
    async def scenario() -> None:
        with pytest.raises(HTTPException) as failure:
            await rig.service.create(cast("str", sdp))
        assert failure.value.status_code == 422
        assert not rig.agents and not rig.requests
        await rig.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("voice", [None, [], " ", "marin\nsecret", "v" * 129])
def test_create_rejects_invalid_voice_values(rig: Rig, voice: object) -> None:
    async def scenario() -> None:
        with pytest.raises(HTTPException) as failure:
            await rig.service.create(OFFER, cast("str", voice))
        assert failure.value.status_code == 422 and not rig.voices

    asyncio.run(scenario())


def test_concurrent_provisioning_and_connected_reservations_are_atomic(rig: Rig) -> None:
    async def scenario() -> None:
        rig.provision_release.clear()
        assert rig.service.active_session_id == ""
        first = asyncio.create_task(create(rig.service))
        await rig.provision_started.wait()
        reservation = rig.service.active_session_id
        assert reservation
        assert (await rig.service.snapshot(reservation))["status"] == "connecting"
        with pytest.raises(HTTPException) as busy:
            await rig.service.create(OFFER)
        assert busy.value.status_code == 409 and len(rig.agents) == 1
        rig.provision_release.set()
        identifier = await first
        assert identifier == reservation
        with pytest.raises(HTTPException) as active:
            await rig.service.create(OFFER)
        assert active.value.status_code == 409
        await rig.service.close(identifier)
        assert rig.service.active_session_id == ""
        second = await create(rig.service)
        assert second != identifier and len(rig.agents) == 2
        await rig.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "hangup"),
    [
        (None, False),
        ([], False),
        ({}, False),
        ({"session": {"id": {"secret": SECRET}}}, False),
        ({"session": {"id": "native/../bad"}}, False),
        ({"session": {"id": "native-known"}}, True),
        ({"session": {"id": "native-known"}, "transport": {"sdp": SECRET}}, True),
        ({"session": {"id": "native-known"}, "transport": {"sdp": {"secret": SECRET}}}, True),
    ],
)
def test_malformed_upstream_is_sanitized_and_known_allocations_are_hung_up(
    rig: Rig, response: object, hangup: bool
) -> None:
    async def scenario() -> None:
        rig.response = response
        with pytest.raises(HTTPException) as failure:
            await rig.service.create(OFFER)
        assert failure.value.status_code == 502
        assert SECRET not in str(failure.value.detail)
        assert rig.hangups == (["native-known"] if hangup else [])
        assert rig.agents[0].closed == 1 and not rig.service._active
        rig.response = "default"
        await create(rig.service)
        await rig.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["exception", "timeout"])
def test_provision_failure_releases_resources_and_reservation(rig: Rig, kind: str) -> None:
    async def scenario() -> None:
        rig.provision_error = kind == "exception"
        if kind == "timeout":
            rig.provision_release.clear()
        with pytest.raises(HTTPException) as failure:
            await rig.service.create(OFFER)
        assert failure.value.status_code == 502
        assert "unconfirmed" in str(failure.value.detail)
        assert SECRET not in str(failure.value.detail)
        assert rig.agents[0].closed == 1 and not rig.service._active
        assert not rig.hangups

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [0, 422, 503])
def test_factory_errors_are_sanitized_and_do_not_hold_reservation(code: int) -> None:
    def factory(voice: str) -> Agent:
        if code:
            raise HTTPException(code, SECRET)
        raise RuntimeError(SECRET)

    async def scenario() -> None:
        service = LiveService(factory)
        for _ in range(2):
            with pytest.raises(HTTPException) as failure:
                await service.create(OFFER)
            assert failure.value.status_code == (422 if code == 422 else 503)
            assert SECRET not in str(failure.value.detail)
        assert not service._active

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "config", [LiveConfig(delegation="client"), LiveConfig(attach_to="foreign"), LiveConfig(fork_from="foreign")]
)
def test_factory_must_return_a_fresh_hosted_agent(rig: Rig, config: LiveConfig) -> None:
    async def scenario() -> None:
        rig.configure = lambda agent: setattr(agent.provider, "live_config", config)
        with pytest.raises(HTTPException) as failure:
            await rig.service.create(OFFER)
        assert failure.value.status_code == 503
        assert not rig.requests and rig.agents[0].closed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "wrong-id", "exception", "error", "eof"])
def test_sideband_startup_failure_hangs_up_provisioned_call(rig: Rig, failure: str) -> None:
    def configure(agent: FakeAgent) -> None:
        if failure == "timeout":
            agent.attach.clear()
        elif failure == "wrong-id":
            agent.attachment_id = "some-other-call"
        elif failure == "exception":
            agent.events.put_nowait(RuntimeError(SECRET))
        elif failure == "error":
            agent.events.put_nowait(ErrorEvent(message=SECRET))
        else:
            agent.events.put_nowait(End())

    async def scenario() -> None:
        rig.configure = configure
        with pytest.raises(HTTPException) as error:
            await rig.service.create(OFFER)
        assert error.value.status_code == 502
        assert SECRET not in str(error.value.detail)
        assert rig.hangups == ["native-1"]
        assert rig.agents[0].stopped.is_set() and rig.agents[0].closed == 1
        assert not rig.service._active

    asyncio.run(scenario())


def test_create_waits_for_attachment_readiness(rig: Rig) -> None:
    async def scenario() -> None:
        rig.configure = lambda agent: agent.attach.clear()
        task = asyncio.create_task(create(rig.service))
        await until(lambda: bool(rig.agents) and rig.agents[0].started.is_set())
        assert not task.done()
        rig.agents[0].attach.set()
        identifier = await task
        assert (await rig.service.snapshot(identifier))["status"] == "connected"
        await rig.service.shutdown()

    asyncio.run(scenario())


def test_canceled_provisioning_collects_allocation_and_finishes_cleanup_despite_repeated_cancel(rig: Rig) -> None:
    async def scenario() -> None:
        rig.provision_release.clear()
        rig.hangup_release.clear()
        task = asyncio.create_task(rig.service.create(OFFER))
        await rig.provision_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and rig.service._active
        rig.provision_release.set()
        await rig.hangup_started.wait()
        task.cancel()
        with pytest.raises(HTTPException) as busy:
            await rig.service.create(OFFER)
        assert busy.value.status_code == 409
        rig.hangup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.hangups == ["native-1"]
        assert rig.agents[0].closed == 1 and not rig.agents[0].started.is_set()
        assert not rig.service._active

    asyncio.run(scenario())


def test_canceled_create_during_attachment_hangs_up_and_joins_sideband(rig: Rig) -> None:
    async def scenario() -> None:
        rig.configure = lambda agent: agent.attach.clear()
        task = asyncio.create_task(rig.service.create(OFFER))
        await until(lambda: bool(rig.agents) and rig.agents[0].started.is_set())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.hangups == ["native-1"]
        assert rig.agents[0].stopped.is_set() and rig.agents[0].closed == 1
        assert not rig.service._active

    asyncio.run(scenario())


def test_level_triggered_anyio_cancellation_still_cleans_up(rig: Rig) -> None:
    async def scenario() -> None:
        with anyio.CancelScope() as scope:
            rig.provision_release.clear()

            async def disconnect() -> None:
                await rig.provision_started.wait()
                scope.cancel()
                await asyncio.sleep(0.01)
                rig.provision_release.set()

            disconnect_task = asyncio.create_task(disconnect())
            try:
                await rig.service.create(OFFER)
            finally:
                with anyio.CancelScope(shield=True):
                    await disconnect_task
        assert rig.hangups == ["native-1"] and rig.agents[0].closed == 1
        assert not rig.service._active

    asyncio.run(scenario())


def test_normalized_captions_are_bounded_safe_and_cursor_filtered(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(runtime, "MAX_EVENTS", 4)
        identifier = await create(rig.service)
        agent = rig.agents[0]
        before = await rig.service.snapshot(identifier)
        agent.events.put_nowait(AudioChunkEvent(chunk=SECRET, extra={"key": SECRET}))
        agent.events.put_nowait(TextChunkEvent(chunk=SECRET))
        agent.events.put_nowait(DoneEvent(final_text=SECRET))
        agent.events.put_nowait(LiveEvent(event_type="response.event", payload={"credentials": SECRET}))
        agent.events.put_nowait(
            InputTranscriptDeltaEvent(delta=" hello", extra={"start_ms": 1, "end_ms": 2.5, "key": SECRET})
        )
        agent.events.put_nowait(AudioTranscriptDeltaEvent(delta=" hello", extra={"start_ms": 3, "end_ms": 4}))
        agent.events.put_nowait(
            LiveEvent(
                event_type="session.input_transcript.delta",
                payload={"delta": f"\x00{SECRET} world", "start_ms": True, "end_ms": float("inf")},
            )
        )
        agent.events.put_nowait(
            AudioTranscriptDeltaEvent(delta="x" * 8000, extra={"start_ms": float("nan"), "end_ms": -1})
        )
        await until(lambda: rig.service._records[identifier].cursor == cast("int", before["cursor"]) + 4)
        snapshot = await rig.service.snapshot(identifier, cast("int", before["cursor"]))
        events = cast("list[Payload]", snapshot["events"])
        assert len(events) == 4
        assert [event["speaker"] for event in events] == ["user", "assistant", "user", "assistant"]
        assert events[0]["text"] == events[1]["text"] == " hello"
        assert events[0]["start_ms"] == 1 and events[0]["end_ms"] == 2.5
        assert events[2]["text"] == "[redacted] world"
        assert "start_ms" not in events[2] and "end_ms" not in events[3]
        assert len(str(events[3]["text"])) == runtime.MAX_TEXT_CHARACTERS
        assert SECRET not in json.dumps(snapshot, allow_nan=False)
        assert (await rig.service.snapshot(identifier, cast("int", snapshot["cursor"])))["events"] == []
        events[0]["text"] = "mutated response"
        assert cast("list[Payload]", (await rig.service.snapshot(identifier))["events"])[0]["text"] == " hello"
        assert snapshot["status"] == "connected"  # A backend DoneEvent is not a voice turn boundary.
        await rig.service.shutdown()

    asyncio.run(scenario())


def test_poll_renews_only_the_target_lease_and_retention_is_bounded(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(runtime, "MAX_COMPLETED", 2)
        identifiers: list[str] = []
        for _ in range(3):
            identifier = await create(rig.service)
            identifiers.append(identifier)
            await rig.service.close(identifier)
        assert len(rig.service._records) == 2
        with pytest.raises(HTTPException) as missing:
            await rig.service.snapshot(identifiers[0])
        assert missing.value.status_code == 404
        active_id = await create(rig.service)
        active = rig.service._active[active_id]
        active.deadline = asyncio.get_running_loop().time() + 0.2
        before = active.deadline
        await rig.service.snapshot(identifiers[-1])
        assert active.deadline == before
        await rig.service.snapshot(active_id)
        assert active.deadline > before + 0.5
        await rig.service.shutdown()

    asyncio.run(scenario())


def test_lost_browser_heartbeat_automatically_closes_call(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(runtime, "LEASE_SECONDS", 0.03)
        identifier = await create(rig.service)
        await until(lambda: not rig.service._active)
        result = await rig.service.snapshot(identifier)
        assert result["status"] == "closed"
        assert "heartbeat expired" in str(result["message"])
        assert rig.agents[0].closed == 1 and rig.agents[0].stopped.is_set()

    asyncio.run(scenario())


def test_late_poll_cannot_revive_expired_lease(rig: Rig) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        rig.service._active[identifier].deadline = 0
        assert (await rig.service.snapshot(identifier))["status"] == "closing"
        await until(lambda: not rig.service._active)
        assert rig.agents[0].closed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("after", [-1, True, "1", None])
def test_bad_cursor_and_foreign_ids_do_not_touch_active_call(rig: Rig, after: object) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        before = rig.service._active[identifier].deadline
        with pytest.raises(HTTPException) as cursor:
            await rig.service.snapshot(identifier, cast("int", after))
        assert cursor.value.status_code == 422
        for method in (rig.service.snapshot, rig.service.close):
            with pytest.raises(HTTPException) as foreign:
                await method("native-1")
            assert foreign.value.status_code == 404
        assert rig.service._active[identifier].deadline == before
        assert not rig.hangups and not rig.agents[0].sent
        await rig.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "eof", "live-error-then-eof", "agent-error"])
def test_transport_failure_is_sanitized_and_cleaned_up(rig: Rig, failure: str) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        item: Event | Exception | End
        if failure == "exception":
            item = RuntimeError(SECRET)
        elif failure == "eof":
            item = End()
        elif failure == "live-error-then-eof":
            rig.agents[0].events.put_nowait(LiveEvent(event_type="error", payload={"error": {"message": SECRET}}))
            item = End()
        else:
            item = ErrorEvent(message=SECRET)
        rig.agents[0].events.put_nowait(item)
        await until(lambda: not rig.service._active)
        result = await rig.service.snapshot(identifier)
        assert result["status"] == "error"
        assert "unconfirmed" in str(result["message"])
        assert SECRET not in json.dumps(result)
        assert rig.hangups == ["native-1"] and rig.agents[0].closed == 1
        await create(rig.service)
        await rig.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "event",
    [
        ErrorEvent(message=SECRET, recoverable=True),
        LiveEvent(event_type="error", payload={"error": {"message": SECRET, "client_event_id": "rejected"}}),
    ],
)
def test_recoverable_error_does_not_end_continuous_call(rig: Rig, event: Event) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        rig.agents[0].events.put_nowait(event)
        await until(lambda: rig.service._records[identifier].cursor == 3)
        result = await rig.service.snapshot(identifier)
        assert result["status"] == "connected" and SECRET not in json.dumps(result)
        await rig.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("command_failure", [False, True])
def test_unconfirmed_finalization_falls_back_to_http_hangup(rig: Rig, command_failure: bool) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        rig.agents[0].finalize_on_close = False
        rig.agents[0].fail_close_command = command_failure
        result = await rig.service.close(identifier)
        assert result["status"] == "closed" and "unconfirmed" in str(result["message"])
        assert rig.hangups == ["native-1"]
        assert rig.agents[0].cancelled == 1 and rig.agents[0].closed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("during_drain", [False, True])
@pytest.mark.parametrize("resource_error", [False, True])
def test_late_finalization_supersedes_failed_hangup_without_hiding_resource_errors(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, during_drain: bool, resource_error: bool
) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        agent = rig.agents[0]
        agent.finalize_on_close = False
        agent.finalize_on_drain = during_drain
        agent.fail_resource_close = resource_error

        async def hangup(session_id: str) -> None:
            rig.hangups.append(session_id)
            assert not agent.controls.status.finalized
            if not during_drain:
                # The close event wins the race with the outstanding HTTP
                # fallback, which can now fail because the session has ended.
                agent.events.put_nowait(LiveEvent(event_type="session.closed"))
                agent.events.put_nowait(End())
                await agent.stopped.wait()
            raise RuntimeError(SECRET)

        monkeypatch.setattr(rig, "hangup", hangup)
        result = await asyncio.wait_for(rig.service.close(identifier), WAIT)
        assert agent.controls.status.finalized
        assert "Finalization confirmed." in str(result["message"])
        errors = [event["text"] for event in cast("list[Payload]", result["events"]) if event["type"] == "error"]
        if resource_error:
            assert result["status"] == "error"
            assert errors == ["Live resource cleanup could not be confirmed."]
        else:
            assert result["status"] == "closed"
            assert result["message"] == "Live session closed. Finalization confirmed."
            assert not errors
        assert SECRET not in json.dumps(result)
        assert rig.hangups == ["native-1"]
        assert agent.cancelled == int(during_drain)
        assert agent.closed == 1 and agent.stopped.is_set() and not rig.service._active

    asyncio.run(scenario())


def test_confirmed_finalization_preserves_prior_connection_error(rig: Rig) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        agent = rig.agents[0]
        agent.finalize_on_drain = True
        agent.events.put_nowait(ErrorEvent(message=SECRET))
        await until(lambda: not rig.service._active)
        result = await rig.service.snapshot(identifier)
        assert result["status"] == "error"
        assert result["message"] == "Live connection reported an error. Finalization confirmed."
        assert [event["text"] for event in cast("list[Payload]", result["events"]) if event["type"] == "error"] == [
            "Live connection reported an error."
        ]
        assert SECRET not in json.dumps(result)
        assert agent.controls.status.finalized and agent.closed == 1
        assert not rig.hangups

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["hangup-error", "hangup-timeout", "resource-close"])
def test_cleanup_failure_is_bounded_and_reported_safely(rig: Rig, failure: str) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        agent = rig.agents[0]
        agent.finalize_on_close = failure == "resource-close"
        agent.fail_resource_close = failure == "resource-close"
        rig.hangup_error = failure == "hangup-error"
        if failure == "hangup-timeout":
            rig.hangup_release.clear()
        result = await asyncio.wait_for(rig.service.close(identifier), WAIT)
        assert result["status"] == "error"
        assert SECRET not in json.dumps(result)
        assert not rig.service._active and agent.stopped.is_set() and agent.closed == 1

    asyncio.run(scenario())


def test_concurrent_and_canceled_close_join_one_teardown(rig: Rig) -> None:
    async def scenario() -> None:
        identifier = await create(rig.service)
        agent = rig.agents[0]
        agent.close_release.clear()
        first = asyncio.create_task(rig.service.close(identifier))
        second = asyncio.create_task(rig.service.close(identifier))
        await agent.close_started.wait()
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        assert not first.done() and not second.done()
        with pytest.raises(HTTPException) as busy:
            await rig.service.create(OFFER)
        assert busy.value.status_code == 409
        agent.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second)["status"] == "closed"
        assert agent.closed == 1 and len(agent.sent) == 1 and not rig.hangups

    asyncio.run(scenario())


@pytest.mark.parametrize("during_create", [False, True])
def test_shutdown_closes_active_or_inflight_create_and_disables_admission(rig: Rig, during_create: bool) -> None:
    async def scenario() -> None:
        if during_create:
            rig.provision_release.clear()
            pending = asyncio.create_task(rig.service.create(OFFER))
            await rig.provision_started.wait()
            stopping = asyncio.create_task(rig.service.shutdown())
            await asyncio.sleep(0)
            assert not stopping.done()
            rig.provision_release.set()
            with pytest.raises(HTTPException):
                await pending
            await stopping
            assert rig.hangups == ["native-1"]
        else:
            await create(rig.service)
            await rig.service.shutdown()
        assert not rig.service._active and rig.agents[0].closed == 1
        await rig.service.shutdown()
        with pytest.raises(HTTPException) as disabled:
            await rig.service.create(OFFER)
        assert disabled.value.status_code == 503

    asyncio.run(scenario())


def test_real_agent_and_live_api_attach_without_second_start_or_server_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real native HTTP/WS code against a loopback-only provider fake."""

    async def scenario() -> None:
        commands: list[Payload] = []
        provisioned: list[Payload] = []
        sidebands: list[str] = []
        agents: list[Agent] = []

        async def provision(request: web.Request) -> web.Response:
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            provisioned.append(await request.json())
            return web.json_response(
                {"session": {"id": "native-real", "client_secret": SECRET}, "transport": {"sdp": ANSWER}}
            )

        async def attach(request: web.Request) -> web.WebSocketResponse:
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            sidebands.append(request.path)
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {"type": "session.input_transcript.delta", "delta": "Hello", "start_ms": 0, "end_ms": 10}
            )
            await socket.send_json(
                {"type": "session.output_transcript.delta", "delta": "Hi", "start_ms": 10, "end_ms": 20}
            )
            async for message in socket:
                event = cast("Payload", json.loads(message.data))
                commands.append(event)
                if event["type"] == "session.close":
                    await socket.send_json(
                        {"type": "session.closed", "usage": {"seconds": 1.0}, "reason": "client_close"}
                    )
                    break
            return socket

        app = web.Application()
        app.router.add_post("/sessions", provision)
        app.router.add_get("/sessions/native-real/attach", attach)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/sessions"
        monkeypatch.setattr(Provider, "live_endpoint", lambda self, **kwargs: url)

        def factory(voice: str) -> Agent:
            agent = Agent(
                provider=Provider(
                    ProviderType.OPENAI_COMPATIBLE,
                    model="gpt-live-1",
                    api_key=SECRET,
                    live_config=LiveConfig(voice=voice or "marin"),
                ),
                session_manager=SessionManager(Path(":memory:")),
            )
            agents.append(agent)
            return agent

        service = LiveService(factory)
        try:
            identifier = await create(service)
            await until(lambda: service._records[identifier].cursor >= 4)
            snapshot = await service.snapshot(identifier)
            captions = [event for event in cast("list[Payload]", snapshot["events"]) if event["type"] == "transcript"]
            assert [event["text"] for event in captions] == ["Hello", "Hi"]
            assert (await service.close(identifier))["status"] == "closed"
            assert len(provisioned) == 1 and sidebands == ["/sessions/native-real/attach"]
            assert [event["type"] for event in commands] == ["session.close"]
            assert agents[0].audio is None and agents[0].live.status.finalized
            assert agents[0]._live_updates is None
        finally:
            await service.shutdown()
            await runner.cleanup()

    asyncio.run(scenario())
