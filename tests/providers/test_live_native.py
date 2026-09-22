"""Native GPT-Live contract tests: controls, hosted tools, HTTP API and audio."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiohttp import web

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.audio import AudioDuplex
from nagents.audio import AudioFormat
from nagents.audio import BytesAudioInput
from nagents.audio import BytesAudioOutput
from nagents.events import AudioChunkEvent
from nagents.events import DoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.live import LiveAPI
from nagents.live import LiveCommandError
from nagents.live import LiveConfig
from nagents.live import LiveEvent
from nagents.live import LiveStatus
from nagents.live import _LiveUpdates
from nagents.live import runtime as runtime
from nagents.live import verify_webhook
from nagents.live.audio import DrainableOutput
from nagents.live.audio import PacedAudioInput
from nagents.live.audio import PlaybackOutput
from nagents.live.audio import SilenceInput
from nagents.live.controls import LiveControls
from nagents.live.hosted import HostedTools
from nagents.types import ImageContent
from nagents.types import TextContent
from nagents.types import ToolCall
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event

Send = Callable[[dict[str, object]], Awaitable[None]]
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


async def wait_until(predicate: Callable[[], bool], timeout: float = HANG_GUARD) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def transcript_delta(kind: str, delta: str, start: int) -> dict[str, object]:
    return {"type": kind, "delta": delta, "start_ms": start, "end_ms": start + 5}


def delegation_event(identifier: str, target: str = "client") -> dict[str, object]:
    return {
        "type": "session.delegation.created",
        "delegation": {"id": identifier, "target": target},
        "offset_ms": 10,
    }


@asynccontextmanager
async def ws_server(monkeypatch: pytest.MonkeyPatch, handler: Handler, *, prefix: str) -> AsyncIterator[None]:
    app = web.Application()
    app.router.add_get(prefix, handler)
    app.router.add_get(prefix + "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}{prefix}"
    monkeypatch.setattr(runtime, "LIVE_URL", url)
    try:
        yield
    finally:
        await runner.cleanup()


async def discard(event: dict[str, object]) -> None:
    pass


async def discard_backend(event: Event) -> None:
    pass


class CollectingSpeaker:
    def __init__(self) -> None:
        self.audio_format = AudioFormat()
        self.chunks: list[bytes] = []
        self.closed = False

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- LiveConfig


def test_live_config_session_body_and_validation() -> None:
    config = LiveConfig(
        delegation="responses",
        voice="cedar",
        history=({"role": "user", "content": [{"type": "input_text", "text": "hi"}]},),
        store=True,
        backend_options={"reasoning": {"effort": "low"}},
    )
    body = config.session("gpt-live-1", "Be brief.")
    assert body["model"] == "gpt-live-1"
    assert body["instructions"] == "Be brief."
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert body["store"] is True
    audio = cast("dict[str, object]", body["audio"])
    assert audio["format"] == {"type": "audio/pcm", "rate": 24000}
    assert cast("dict[str, object]", audio["output"])["voice"] == "cedar"
    responses = cast("dict[str, object]", cast("dict[str, object]", body["delegation"])["responses"])
    assert responses["reasoning"] == {"effort": "low"}

    media = config.session("gpt-live-1", "x", media=True)
    assert "format" not in cast("dict[str, object]", media["audio"])

    with pytest.raises(ValueError, match="attachment or a stored fork"):
        LiveConfig(attach_to="a", fork_from="b")
    with pytest.raises(ValueError, match="Invalid Live history"):
        LiveConfig(history=tuple({"role": "user"} for _ in range(129)))
    with pytest.raises(ValueError, match="Unsupported Live Responses backend option"):
        LiveConfig(backend_options={"unknown": 1}).session("m", "i")
    with pytest.raises(ValueError, match="one text part"):
        LiveConfig(history=({"role": "user", "content": "plain"},)).session("m", "i")
    with pytest.raises(ValueError, match="text-only"):
        LiveConfig(history=({"role": "user", "content": [{"type": "input_image", "image_url": "x"}]},)).session(
            "m", "i"
        )


# --------------------------------------------------------------------------- LiveControls


def test_live_controls_command_wait_status_and_final_usage() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        controls = LiveControls()
        with pytest.raises(RuntimeError, match=r"wait for session\.started"):
            await controls.command("session.close")
        controls.sender = send

        # Acknowledged commands resolve through observe(); reuse the same ID.
        identifier = await controls.command("session.input_audio.mute")
        assert sent[-1]["type"] == "session.input_audio.mute"
        controls.observe({"type": "session.command.ack", "client_event_id": identifier})
        result = await controls.wait(identifier)
        assert result["type"] == "session.command.ack"

        # Server error payloads become LiveCommandError instead of crashing the reader.
        rejected = await controls.command("session.input_audio.unmute")
        controls.observe({"type": "error", "error": {"code": "bad_command", "client_event_id": rejected}})
        with pytest.raises(LiveCommandError, match="bad_command"):
            await controls.wait(rejected)

        # Status is accumulated from observe and finalized on session.closed.
        controls.observe({"type": "session.started", "session": {"id": "s-1"}})
        controls.observe({"type": "usage", "usage": {"seconds": 12.5}})
        controls.observe({"type": "context_window", "context_window": {"usage_ratio": 0.25}})
        assert controls.status.session_id == "s-1"
        assert controls.status.seconds == 12.5
        assert controls.status.context_ratio == 0.25
        await controls.close()
        controls.observe({"type": "session.closed", "reason": "caller_hangup"})
        assert controls.status.finalized is True and controls.status.reason == "caller_hangup"
        assert controls.closing is True
        assert controls.sender is None
        with pytest.raises(RuntimeError, match=r"wait for session\.started"):
            await controls.command("session.close")

        # disconnect() resolves outstanding futures with a connection_closed error.
        other = LiveControls()
        other.sender = send
        pending = await other.command("session.input_audio.mute")
        other.disconnect()
        with pytest.raises(LiveCommandError, match="connection_closed"):
            await other.wait(pending)

    asyncio.run(scenario())


def test_live_controls_mute_backend_validation_and_submit_routing() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        controls = LiveControls()
        controls.sender = send
        await controls.mute_input()
        await controls.unmute_input()
        assert [event["type"] for event in sent] == [
            "session.input_audio.mute",
            "session.input_audio.unmute",
        ]

        # Backend updates require a Responses session and an allowed setting.
        with pytest.raises(ValueError, match="supported Responses backend settings"):
            await controls.update_backend(model="x")
        controls.responses = True
        await controls.update_backend(model="gpt-5.6-luna", reasoning={"effort": "low"})
        assert sent[-1]["type"] == "session.update"
        with pytest.raises(ValueError, match="supported Responses backend settings"):
            await controls.update_backend(unknown=1)

        assert controls.invalidate_tasks() == 1
        assert controls.invalidate_tasks() == 2

        with pytest.raises(RuntimeError, match="unavailable before Live startup"):
            await controls.submit_text("hi")

        seen: list[list[object]] = []

        async def submit(content: list[object]) -> str:
            seen.append(content)
            return "submitted"

        controls.submit_input = submit  # type: ignore[assignment]
        assert await controls.submit_text("hello") == "submitted"
        assert isinstance(seen[0][0], TextContent) and seen[0][0].text == "hello"
        image = ImageContent(base64_data="aGk=", media_type="image/png")
        await controls.submit_image(image, text="look")
        assert [type(part).__name__ for part in seen[1]] == ["TextContent", "ImageContent"]

    asyncio.run(scenario())


def test_live_controls_unknown_wait_and_close_reuse() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        controls = LiveControls()
        controls.sender = send
        with pytest.raises(ValueError, match="Unknown or expired"):
            await controls.wait("missing")

        first = await controls.close()
        assert controls.closing is True
        with pytest.raises(RuntimeError, match=r"wait for session\.started"):
            await controls.close()
        assert first in {event["event_id"] for event in sent}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- HostedTools


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
        self.calls.append(tool_call)
        return ToolResultEvent(id=tool_call.id, name=tool_call.name, result={"value": tool_call.arguments.get("value")})


class _FakeAgent:
    def __init__(self) -> None:
        self.tool_executor = _FakeExecutor()


def hosted_controls() -> tuple[LiveControls, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    async def send(event: dict[str, object]) -> None:
        sent.append(event)

    controls = LiveControls()
    controls.sender = send
    return controls, sent


def function_item(call_id: str, name: str = "work", arguments: str = '{"value": 1}') -> dict[str, object]:
    return {
        "type": "response.event",
        "delegation_id": "d1",
        "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments},
        },
    }


def test_hosted_tools_batches_after_completion_and_dedupes() -> None:
    async def scenario() -> None:
        controls, sent = hosted_controls()
        emitted: list[Event] = []

        async def emit(event: Event) -> None:
            emitted.append(event)

        agent = _FakeAgent()
        hosted = HostedTools(cast("Agent", agent), controls, emit)
        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.created", "response": {"id": "r1"}},
            }
        )
        hosted.observe(function_item("c1", arguments='{"value": 1}'))
        hosted.observe(function_item("c1", arguments='{"value": 1}'))  # Duplicate call ID ignored.
        hosted.observe(function_item("c2", arguments='{"value": 2}'))

        assert controls.pending_tools == 2
        assert sent == []  # No early response.create before response.completed.

        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.completed", "response": {"id": "r1"}},
            }
        )
        task = asyncio.create_task(hosted.run())
        try:
            await wait_until(lambda: controls.pending_tools == 0)
            await asyncio.sleep(0)  # Let run() emit the trailing response.create.
            assert [call.id for call in agent.tool_executor.calls] == ["c1", "c2"]
            assert [event["type"] for event in sent] == [
                "response.item.create",
                "response.item.create",
                "response.create",
            ]
            tool_calls = [event for event in emitted if isinstance(event, ToolCallEvent)]
            assert [event.extra["delegation_id"] for event in tool_calls] == ["d1", "d1"]
            assert all(event.extra["source"] == "live_backend" for event in tool_calls)
            results = [event for event in emitted if isinstance(event, ToolResultEvent)]
            assert [event.error for event in results] == [None, None]
            outputs = [cast("dict[str, object]", event["item"]) for event in sent[:2]]
            assert outputs[0]["call_id"] == "c1" and outputs[1]["call_id"] == "c2"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_hosted_tools_invalid_arguments_and_superseded_revision() -> None:
    async def scenario() -> None:
        controls, sent = hosted_controls()
        emitted: list[Event] = []

        async def emit(event: Event) -> None:
            emitted.append(event)

        agent = _FakeAgent()
        hosted = HostedTools(cast("Agent", agent), controls, emit)
        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.created", "response": {"id": "r1"}},
            }
        )
        hosted.observe(function_item("c1", arguments="not json"))
        hosted.observe(function_item("c2", arguments='{"value": 2}'))
        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.completed", "response": {"id": "r1"}},
            }
        )
        controls.invalidate_tasks()  # Bump revision before run() so queued work is superseded.
        task = asyncio.create_task(hosted.run())
        try:
            await wait_until(lambda: controls.pending_tools == 0)
            results = [event for event in emitted if isinstance(event, ToolResultEvent)]
            assert results[0].error == "Invalid function call"
            assert results[1].error == "Skipped: application task was superseded"
            assert agent.tool_executor.calls == []
            assert sent[-1]["type"] == "response.create"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_hosted_tools_submit_text_and_image() -> None:
    async def scenario() -> None:
        controls, sent = hosted_controls()
        emitted: list[Event] = []

        async def emit(event: Event) -> None:
            emitted.append(event)

        hosted = HostedTools(cast("Agent", _FakeAgent()), controls, emit)
        identifier = await hosted.submit([TextContent(text="hello")])
        assert identifier
        assert [event["type"] for event in sent] == ["response.item.create", "response.create"]
        message = cast("dict[str, object]", sent[-2]["item"])
        assert message["role"] == "user"
        assert cast("list[dict[str, object]]", message["content"])[0] == {"type": "input_text", "text": "hello"}
        assert sent[1]["type"] == "response.create"  # No pending tools -> continue immediately.

        controls.submit_input = hosted.submit
        image = ImageContent(base64_data="aGk=", media_type="image/png")
        identifier = await controls.submit_image(image)
        assert identifier
        assert [event["type"] for event in sent[-2:]] == ["response.item.create", "response.create"]
        item = cast("dict[str, object]", sent[-2]["item"])
        parts = cast("list[dict[str, object]]", item["content"])
        assert any(part["type"] == "input_image" for part in parts)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- audio


def test_playback_output_mute_hold_and_approval() -> None:
    async def scenario() -> None:
        sink = BytesAudioOutput()
        playback = PlaybackOutput(sink)

        await playback.pause()
        await playback.write(b"dropped")
        assert playback.dropped_bytes == 7 and sink.data == b""
        playback.resume()
        await playback.write(b"played")
        assert sink.data == b"played"
        sink._buffer.clear()

        revision = await playback.hold()
        await playback.write(b"held")
        assert playback.holding is True and sink.data == b""
        assert await playback.approve(revision + 1) is False  # Stale revision rejected.
        assert playback.holding is True
        assert await playback.approve(revision) is True
        assert sink.data == b"held" and playback.holding is False

        await playback.interrupt()
        assert playback.muted is True and sink.data == b"held"

    asyncio.run(scenario())


def test_playback_output_hold_buffer_limit() -> None:
    async def scenario() -> None:
        playback = PlaybackOutput(BytesAudioOutput(), buffer_limit=4)
        await playback.hold()
        await playback.write(b"1234")
        with pytest.raises(BufferError, match="buffer exceeded"):
            await playback.write(b"5")
        assert playback.muted is True

    asyncio.run(scenario())


class _DrainSink:
    def __init__(self, audio_format: AudioFormat | None = None) -> None:
        self.audio_format = audio_format or AudioFormat()
        self.data = bytearray()
        self.drained = 0
        self.interrupts = 0

    async def write(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def close(self) -> None:
        pass

    async def drain(self) -> None:
        self.drained += 1


def test_playback_output_play_clip_verifies_drain_completion() -> None:
    async def scenario() -> None:
        sink = _DrainSink()
        playback = PlaybackOutput(sink)
        source = BytesAudioInput(b"\x01\x02\x03\x04", chunk_size=2)
        await playback.play_clip(source)
        assert sink.drained == 1
        assert playback.clip_completed is True
        assert bytes(sink.data) == b"\x01\x02\x03\x04"
        assert isinstance(sink, DrainableOutput)

        # Mismatched formats are rejected before playback.
        with pytest.raises(ValueError, match="Verified playback"):
            await playback.play_clip(BytesAudioInput(b"", audio_format=AudioFormat(sample_rate=8000)))

    asyncio.run(scenario())


def test_silence_and_paced_input() -> None:
    async def scenario() -> None:
        silence = SilenceInput(AudioFormat(sample_rate=8000, sample_width=2))
        iterator = silence.__aiter__()
        chunk = await iterator.__anext__()
        assert chunk == b"\x00" * (8000 * 2 // 50)
        assert await iterator.__anext__() == chunk

        mulaw = SilenceInput(AudioFormat(encoding="audio/pcmu", sample_rate=8000, sample_width=1))
        assert (await mulaw.__aiter__().__anext__())[:1] == b"\xff"
        alaw = SilenceInput(AudioFormat(encoding="audio/pcma", sample_rate=8000, sample_width=1))
        assert (await alaw.__aiter__().__anext__())[:1] == b"\xd5"

        paced = PacedAudioInput(
            BytesAudioInput(b"\x00\x00" * 8, chunk_size=16, audio_format=AudioFormat(sample_rate=8000)),
            silence_tail=True,
        )
        chunks = []
        async for value in paced:
            chunks.append(value)
            if len(chunks) == 2:
                break
        assert chunks[0] == b"\x00" * 16  # Pacing does not alter payloads.

    asyncio.run(scenario())


# --------------------------------------------------------------------------- LiveAPI


@asynccontextmanager
async def api_server(handler: Handler) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/v1/live/sessions"
    finally:
        await runner.cleanup()


def live_provider() -> Provider:
    return Provider(ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=LiveConfig())


def test_live_api_webhooks_and_http_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        bodies: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            bodies.append({"method": request.method, "path": request.path})
            if request.path.endswith("/content"):
                response = web.StreamResponse()
                await response.prepare(request)
                await response.write(b"record-")
                await response.write(b"bytes")
                await response.write_eof()
                return response
            payload = await request.json() if request.can_read_body else {}
            if request.path.endswith("/fork"):
                body = web.StreamResponse(status=201)
                await body.prepare(request)
                await body.write(b'{"id": "forked", ')
                await body.write(b'"ok": true}')  # JSON split across chunks.
                await body.write_eof()
                return body
            if request.path.endswith("/reject"):
                return web.json_response({}, status=500)
            return web.json_response({"id": "session-1", "echo": payload})

        async with api_server(handle) as base_url:
            api = LiveAPI(live_provider())
            api.base_url = base_url
            assert await api.create_webrtc("v=0 offer", {"model": "gpt-live-1"})
            created = await api.create_webrtc("v=0 offer", {"model": "gpt-live-1"})
            assert created["id"] == "session-1"
            assert await api.fork_webrtc("source-1", "v=0 offer", store=True)
            await api.accept("session-1", {"model": "gpt-live-1", "voice": "marin"})
            await api.hangup("session-1")
            assert await api.fork_webrtc("source-1", "v=0 offer", store=True) == {"id": "forked", "ok": True}
            with pytest.raises(RuntimeError, match=r"failed \(500\)"):
                await api.reject("session-1", 500)

            chunks = [chunk async for chunk in api.recording("session-1")]
            assert b"".join(chunks) == b"record-bytes"

    asyncio.run(scenario())


def test_live_api_argument_validation() -> None:
    api = LiveAPI(live_provider())
    with pytest.raises(ValueError, match="session ID is required"):
        api.url("", "accept")
    with pytest.raises(ValueError, match="SDP offer"):
        asyncio.run(api.create_webrtc("", {}))
    with pytest.raises(ValueError, match="SIP rejection status"):
        asyncio.run(api.reject("s", 200))
    with pytest.raises(ValueError, match="SIP or telephone target"):
        asyncio.run(api.refer("s", "https://example.test"))


def test_verify_webhook_signature() -> None:
    secret_bytes = b"super-secret-key"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    body = json.dumps({"type": "sip.call", "id": "1"}).encode()
    identifier = "msg-1"
    timestamp = str(int(time.time()))
    signature = base64.b64encode(
        hmac.new(secret_bytes, identifier.encode() + b"." + timestamp.encode() + b"." + body, hashlib.sha256).digest()
    ).decode()
    headers = {"webhook-id": identifier, "webhook-timestamp": timestamp, "webhook-signature": "v1," + signature}
    assert verify_webhook(body, headers, secret) == {"type": "sip.call", "id": "1"}

    with pytest.raises(ValueError, match="Invalid Live webhook"):
        verify_webhook(body + b"tamper", headers, secret)
    with pytest.raises(ValueError, match="Invalid Live webhook"):
        verify_webhook(body, {**headers, "webhook-timestamp": str(int(time.time()) - 10_000)}, secret)
    with pytest.raises(ValueError, match="Invalid Live webhook"):
        verify_webhook(body, {"webhook-id": identifier}, secret)


# --------------------------------------------------------------------------- attach / fork startup


def test_attach_connection_skips_session_start(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        paths: list[str] = []
        speaker = CollectingSpeaker()

        async def handle(request: web.Request) -> web.WebSocketResponse:
            paths.append(request.path)
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            # Attach bypasses session.start: the server speaks first.
            await socket.send_json({"type": "connection.attached", "session": {"id": "sid-1"}})
            await socket.send_json({"type": "session.closed", "usage": {"seconds": 1}})
            return socket

        async with ws_server(monkeypatch, handle, prefix="/live"):
            connection = runtime._LiveConnection(
                "test-key",
                runtime.ResponsesDelegation("backend-model"),
                on_backend_event=discard_backend,
                updates=_LiveUpdates(),
                options=LiveConfig(attach_to="sid-1"),
            )
            await asyncio.wait_for(connection.run(AudioDuplex(input=None, output=speaker)), HANG_GUARD)
        assert paths == ["/live/sid-1/attach"]
        assert speaker.closed is True
        assert connection.updates.sender is None  # Controls are disconnected on cleanup.
        assert connection.updates.status.finalized is True

    asyncio.run(scenario())


def test_fork_connection_sends_limited_session_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        captured: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            captured.append(await socket.receive_json())
            await socket.send_json({"type": "session.started", "session": {"id": "forked"}})
            await socket.send_json({"type": "session.closed", "usage": {"seconds": 1}})
            return socket

        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE,
            "test-key",
            "gpt-live-1",
            live_config=LiveConfig(
                delegation="responses",
                fork_from="src-1",
                store=True,
                backend_options={"reasoning": {"effort": "low"}},
            ),
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "fork.db"),
            compactor=None,
            audio=AudioDuplex(input=None, output=BytesAudioOutput()),
        )
        async with ws_server(monkeypatch, handle, prefix="/live"):
            try:
                _ = [event async for event in agent.run()]
            finally:
                await agent.close()

        assert captured[0]["type"] == "session.start"
        session = cast("dict[str, object]", captured[0]["session"])
        # Forks send only the supported subset: store, optional backend settings and audio format.
        assert set(session) == {"store", "delegation", "audio"}
        assert session["store"] is True
        assert cast("dict[str, object]", session["delegation"]) == {
            "type": "responses",
            "responses": {"reasoning": {"effort": "low"}},
        }
        assert cast("dict[str, object]", session["audio"])["format"] == {"type": "audio/pcm", "rate": 24000}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- hosted programmatic inputs


def test_hosted_programmatic_submit_reaches_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        captured: list[dict[str, object]] = []

        def item_message() -> list[dict[str, object]]:
            return [event for event in captured if event["type"] == "response.item.create"]

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            start = await socket.receive_json()
            assert start["type"] == "session.start"
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            while True:
                event = await socket.receive_json()
                captured.append(event)
                if event["type"] == "response.create" and item_message():
                    await socket.send_json({"type": "session.closed", "usage": {"seconds": 1}})
                    break
            return socket

        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE,
            "test-key",
            "gpt-live-1",
            live_config=LiveConfig(delegation="responses"),
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "hosted.db"),
            compactor=None,
            audio=AudioDuplex(input=None, output=BytesAudioOutput()),
        )
        async with ws_server(monkeypatch, handle, prefix="/live"):
            try:
                async for event in agent.run():
                    if isinstance(event, LiveEvent) and event.event_type == "session.started":
                        await agent.live.submit_text("hello")
                        await agent.live.submit_image(ImageContent(base64_data="aGk=", media_type="image/png"))
            finally:
                await agent.close()
        messages = item_message()
        assert messages
        content = cast("list[dict[str, object]]", cast("dict[str, object]", messages[0]["item"])["content"])
        assert content[0] == {"type": "input_text", "text": "hello"}
        assert captured[-1]["type"] == "response.create"

    asyncio.run(scenario())


def test_agent_live_property_requires_startup(tmp_path: Path) -> None:
    agent = Agent(
        Provider(ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=LiveConfig()),
        SessionManager(tmp_path / "voice.db"),
        compactor=None,
    )
    with pytest.raises(RuntimeError, match="has not started a Live connection"):
        _ = agent.live
    assert isinstance(LiveStatus(), LiveStatus)


# --------------------------------------------------------------------------- recent production behaviors


class _InferStub:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def run(
        self, user_message: object = None, session_id: str | None = None, **kwargs: object
    ) -> AsyncIterator[object]:
        for event in self._events:
            yield event


def test_infer_application_events_keep_application_id_and_null_delegation() -> None:
    async def scenario() -> None:
        emitted: list[Event] = []

        async def emit(event: Event) -> None:
            emitted.append(event)

        stub = _InferStub([ToolResultEvent(id="c1", name="work", result="ok"), DoneEvent(final_text="done")])
        result = await runtime.infer(cast("Agent", stub), [TextContent(text="do it")], "application:abc", emit)
        assert result == "done"
        assert all(event.extra["delegation_id"] is None for event in emitted)
        assert all(event.extra["application_id"] == "application:abc" for event in emitted)
        assert all(event.extra["source"] == "live_backend" for event in emitted)

        normal = _InferStub([DoneEvent(final_text="ok")])
        result = await runtime.infer(cast("Agent", normal), "question", "dg-1", emit)
        assert result == "ok"
        assert emitted[-1].extra["delegation_id"] == "dg-1"
        assert "application_id" not in emitted[-1].extra

    asyncio.run(scenario())


def test_client_delegations_can_observe_without_claiming_tools() -> None:
    async def scenario() -> None:
        async def backend(context: str, identifier: str) -> str:
            return "unused"

        async def send(event: dict[str, object]) -> None:
            pass

        client = runtime.ClientDelegations(backend, send)
        client.observe(transcript_delta("session.input_transcript.delta", "hi", 0))
        client.observe(delegation_event("dg-1"))
        assert [client.pending.get_nowait()] == ["dg-1"]

        detached = runtime.ClientDelegations(backend, send)
        detached.enabled = False  # Sideband observer: keep events, claim no tools.
        detached.observe(transcript_delta("session.input_transcript.delta", "hi", 0))
        detached.observe(delegation_event("dg-1"))
        assert detached.pending.empty()

    asyncio.run(scenario())


def test_hosted_tools_failed_response_drops_unexecuted_calls() -> None:
    async def scenario() -> None:
        controls, sent = hosted_controls()
        emitted: list[Event] = []

        async def emit(event: Event) -> None:
            emitted.append(event)

        hosted = HostedTools(cast("Agent", _FakeAgent()), controls, emit)
        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.created", "response": {"id": "r1"}},
            }
        )
        hosted.observe(function_item("c1"))
        assert controls.pending_tools == 1

        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.incomplete", "response": {"id": "r1"}},
            }
        )
        assert controls.pending_tools == 0

        task = asyncio.create_task(hosted.run())
        try:
            await asyncio.sleep(0.05)
            assert emitted == []  # No tool call/result was released.
            assert sent == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_empty_verified_clip_raises_without_marking_played() -> None:
    async def scenario() -> None:
        sink = _DrainSink()
        playback = PlaybackOutput(sink)
        with pytest.raises(ValueError, match="contains no audio"):
            await playback.play_clip(BytesAudioInput(b""))
        assert playback.clip_completed is False
        assert sink.drained == 0
        assert sink.interrupts >= 1  # The failed clip interrupted playback instead.

    asyncio.run(scenario())


def test_live_configuration_encodes_local_tools_for_http_create(tmp_path: Path) -> None:
    def weather(city: str) -> str:
        """Look up weather."""
        return city

    agent = Agent(
        Provider(
            ProviderType.OPENAI_COMPATIBLE,
            "test-key",
            "gpt-live-1",
            live_config=LiveConfig(delegation="responses", web_search=True),
        ),
        SessionManager(tmp_path / "configuration.db"),
        tools=[weather],
        compactor=None,
    )
    plain = agent.live_configuration()
    media = agent.live_configuration(media=True)
    assert cast("dict[str, object]", plain["audio"])["format"] == {"type": "audio/pcm", "rate": 24000}
    assert "format" not in cast("dict[str, object]", media["audio"])
    for session in (plain, media):
        responses = cast("dict[str, object]", cast("dict[str, object]", session["delegation"])["responses"])
        tools = cast("list[dict[str, object]]", responses["tools"])
        assert {"type": "web_search"} in tools
        assert any(tool.get("name") == "weather" for tool in tools)


def test_pending_acknowledgments_drain_after_sender_stops() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        controls = LiveControls()
        controls.sender = send
        identifier = await controls.command("session.input_audio.mute")
        controls.sender = None  # Commands stop, but outstanding acknowledgments still resolve.
        controls.observe({"type": "session.input_audio.muted", "client_event_id": identifier})
        result = await controls.wait(identifier)
        assert result["type"] == "session.input_audio.muted"

    asyncio.run(scenario())


def test_live_controls_pending_commands_fail_fast() -> None:
    async def scenario() -> None:
        async def send(event: dict[str, object]) -> None:
            pass

        controls = LiveControls()
        controls.sender = send
        for _ in range(1024):
            await controls.command("session.input_audio.mute")
        with pytest.raises(RuntimeError, match="Too many pending Live commands"):
            await controls.command("session.input_audio.mute")

    asyncio.run(scenario())


def test_reflected_audio_chunk_preserves_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        chunks: list[Event] = []

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            await socket.send_json(
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"\x00\x01").decode(),
                    "start_ms": 120,
                    "end_ms": 180,
                }
            )
            await socket.send_json({"type": "session.closed", "usage": {"seconds": 1}})
            return socket

        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE,
            "test-key",
            "gpt-live-1",
            live_config=LiveConfig(delegation="responses"),
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "timestamps.db"),
            compactor=None,
            audio=AudioDuplex(input=None, output=BytesAudioOutput()),
        )
        async with ws_server(monkeypatch, handle, prefix="/live"):
            try:
                async for event in agent.run():
                    chunks.append(event)
            finally:
                await agent.close()

        audio = [event for event in chunks if isinstance(event, AudioChunkEvent)]
        assert audio
        assert audio[0].format == "audio/pcm"
        assert audio[0].extra["sample_rate"] == 24000
        assert audio[0].extra["start_ms"] == 120 and audio[0].extra["end_ms"] == 180

    asyncio.run(scenario())


def test_send_audio_disabled_or_empty_never_injects() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        for audio in (AudioDuplex(), AudioDuplex(output=BytesAudioOutput())):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await runtime.send_audio(audio, send, enabled=False)
        assert sent == []

    asyncio.run(scenario())


def test_send_audio_finite_input_continues_with_silence() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        audio = AudioDuplex(input=BytesAudioInput(b"\x00\x00" * 4, chunk_size=4))
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await runtime.send_audio(audio, send)

        assert sent and sent[0]["type"] == "session.input_audio.append"
        silence = base64.b64decode(str(sent[-1]["audio"]))
        assert silence == b"\x00" * (24000 * 2 // 50)  # EOF keeps the input timeline alive.

    asyncio.run(scenario())


def test_connection_preserves_dict_voice_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"type": "connection.attached", "session": {"id": "sid"}})
            await socket.send_json({"type": "session.closed", "usage": {"seconds": 1}})
            return socket

        async with ws_server(monkeypatch, handle, prefix="/live"):
            connection = runtime._LiveConnection(
                "test-key",
                runtime.ResponsesDelegation("backend-model"),
                voice={"id": "cedar"},
                on_backend_event=discard_backend,
                updates=_LiveUpdates(),
                options=LiveConfig(attach_to="sid"),
            )
            await asyncio.wait_for(connection.run(AudioDuplex(input=None, output=CollectingSpeaker())), HANG_GUARD)

        audio = cast("dict[str, object]", connection.config["audio"])
        assert audio["output"] == {"voice": {"id": "cedar"}}  # Voice selector survives format resolution.
        assert audio["format"] == {"type": "audio/pcm", "rate": 24000}

    asyncio.run(scenario())


class _ExtraExecutor:
    async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
        return ToolResultEvent(id=tool_call.id, name=tool_call.name, result="ok", extra={"custom": "keep"})


class _ExtraAgent:
    def __init__(self) -> None:
        self.tool_executor = _ExtraExecutor()


def test_hosted_tools_preserves_custom_extra_and_rejects_nan() -> None:
    async def scenario() -> None:
        controls, _sent = hosted_controls()
        emitted: list[Event] = []

        async def emit(event: Event) -> None:
            emitted.append(event)

        hosted = HostedTools(cast("Agent", _ExtraAgent()), controls, emit)
        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.created", "response": {"id": "r1"}},
            }
        )
        hosted.observe(function_item("c1", arguments='{"value": 1}'))
        hosted.observe(function_item("c2", arguments='{"value": NaN}'))
        hosted.observe(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.completed", "response": {"id": "r1"}},
            }
        )
        task = asyncio.create_task(hosted.run())
        try:
            await wait_until(lambda: controls.pending_tools == 0)
            results = [event for event in emitted if isinstance(event, ToolResultEvent)]
            assert results[0].extra == {
                "custom": "keep",
                "source": "live_backend",
                "delegation_id": "d1",
                "response_id": "r1",
            }
            assert results[1].error == "Invalid function call"  # NaN is not valid JSON.
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
