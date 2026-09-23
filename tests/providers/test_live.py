"""Offline tests for native GPT-Live voice sessions (``nagents.live``)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import aclosing
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiohttp import web

from nagents import Agent
from nagents import AudioChunkEvent
from nagents import AudioDuplex
from nagents import AudioTranscriptDeltaEvent
from nagents import CompactionStartedEvent
from nagents import DoneEvent
from nagents import ErrorEvent
from nagents import FinishReason
from nagents import InputTranscriptDeltaEvent
from nagents import LiveConfig
from nagents import Provider
from nagents import ProviderType
from nagents import ReasoningChunkEvent
from nagents import SessionManager
from nagents import TextChunkEvent
from nagents import TextDoneEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent
from nagents import Usage
from nagents.live import LiveEvent
from nagents.live import _LiveUpdates
from nagents.live import runtime as live
from nagents.realtime import AudioFormat
from nagents.realtime import BytesAudioInput
from nagents.realtime import BytesAudioOutput
from nagents.types import TextContent
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.events import Event as AgentEvent
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition

Send = Callable[[dict[str, object]], Awaitable[None]]
Handler = Callable[[web.Request], Awaitable[web.WebSocketResponse]]


def transcript(kind: str, delta: str, start: int) -> dict[str, object]:
    return {"type": kind, "delta": delta, "start_ms": start, "end_ms": start + 5}


def delegation(identifier: str, target: str = "client") -> dict[str, object]:
    return {
        "type": "session.delegation.created",
        "delegation": {"id": identifier, "target": target},
        "offset_ms": 10,
    }


def collect_into(sink: list[dict[str, object]]) -> Send:
    async def collect(event: dict[str, object]) -> None:
        sink.append(event)

    return collect


async def discard_backend_event(event: AgentEvent) -> None:
    pass


async def wait_until(predicate: Callable[[], bool], timeout: float = HANG_GUARD) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class CollectingSpeaker:
    """Minimal speaker that records playback and can signal when audio arrives."""

    def __init__(self, ready: asyncio.Event | None = None) -> None:
        self.audio_format = AudioFormat()
        self.chunks: list[bytes] = []
        self.closed = False
        self.ready = ready

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        if self.ready is not None:
            self.ready.set()

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class BlockingMic:
    """Yields fixed chunks then stays open, like a live microphone."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.audio_format = AudioFormat()
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        await asyncio.Event().wait()


@asynccontextmanager
async def live_server(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> AsyncIterator[None]:
    app = web.Application()
    app.router.add_get("/live", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/live"
    monkeypatch.setattr(live, "LIVE_URL", url)
    monkeypatch.setattr(Provider, "live_endpoint", lambda self, **kwargs: url.replace("http://", "ws://"))
    try:
        yield
    finally:
        await runner.cleanup()


def test_live_config_and_responses_delegation() -> None:
    assert LiveConfig().delegation == "responses"
    assert LiveConfig(delegation="client", voice="cedar").voice == "cedar"
    with pytest.raises(ValueError):
        LiveConfig(delegation="invalid")  # type: ignore[arg-type]

    hosted = live.ResponsesDelegation("backend-model", web_search=False).to_dict()
    assert hosted["type"] == "responses"
    responses = cast("dict[str, object]", hosted["responses"])
    assert responses["model"] == "backend-model"
    assert responses["instructions"] == "Return concise verified facts and status."
    assert responses["tools"] == []
    assert responses["tool_choice"] == "auto"

    search = live.ResponsesDelegation("backend-model", instructions="facts", web_search=True).to_dict()
    assert cast("dict[str, object]", search["responses"])["tools"] == [{"type": "web_search"}]


def test_client_delegations_context_dedup_and_limit() -> None:
    async def scenario() -> None:
        async def backend(context: str, identifier: str) -> str:
            return ""

        sent: list[dict[str, object]] = []
        client = live.ClientDelegations(backend, collect_into(sent))
        client.observe(transcript("session.input_transcript.delta", " Thursday", 20))
        client.observe(transcript("session.output_transcript.delta", "Which day?", 0))
        client.observe(transcript("session.input_transcript.delta", "Thursday", 10))

        fragments = cast("list[dict[str, object]]", json.loads(client.context()))
        assert [fragment["text"] for fragment in fragments] == ["Which day?", "Thursday", " Thursday"]
        assert [fragment["speaker"] for fragment in fragments] == ["assistant", "user", "user"]

        # Hosted delegations are never queued for local client execution.
        client.observe(delegation("hosted", target="responses"))
        assert client.pending.empty()

        client.observe(delegation("first"))
        client.observe(delegation("second"))
        client.observe(delegation("first"))  # Duplicates are ignored, not latest-wins.
        assert [client.pending.get_nowait(), client.pending.get_nowait()] == ["first", "second"]
        assert client.pending.empty()

        for index in range(32):
            client.observe(delegation(f"queue-{index}"))
        with pytest.raises(RuntimeError, match="queue is full"):
            client.observe(delegation("overflow"))

    asyncio.run(scenario())


def test_client_delegations_serialize_and_only_explicit_invalidation_drops() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[tuple[str, str]] = []
        output: list[dict[str, object]] = []
        active = 0
        peak = 0

        async def backend(context: str, identifier: str) -> str:
            nonlocal active, peak
            calls.append((context, identifier))
            active += 1
            peak = max(peak, active)
            try:
                if len(calls) == 1:
                    started.set()
                    await release.wait()
                return f"answer for {identifier}"
            finally:
                active -= 1

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(transcript("session.input_transcript.delta", "Question", 0))
        client.observe(delegation("first"))
        client.observe(delegation("second"))
        worker = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(started.wait(), HANG_GUARD)
            # Input transcript deltas alone must NOT invalidate a running task.
            for index in range(3):
                client.observe(transcript("session.input_transcript.delta", f"more {index}", 50 + index))
            release.set()
            await wait_until(lambda: len(output) >= 2)

            assert peak == 1  # One backend lane: tasks never overlap.
            assert [identifier for _, identifier in calls] == ["first", "second"]
            assert all(event["type"] == "session.commentary.append" for event in output)
            assert [event["delegation_id"] for event in output][:2] == ["first", "second"]
            assert "more 2" in calls[1][0]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_client_delegation_explicit_invalidation_drops_superseded_result() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        output: list[dict[str, object]] = []
        calls: list[str] = []

        async def backend(context: str, identifier: str) -> str:
            calls.append(identifier)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return f"answer for {identifier}"

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(transcript("session.input_transcript.delta", "Question", 0))
        client.observe(delegation("first"))
        client.observe(delegation("second"))
        worker = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(started.wait(), HANG_GUARD)
            client.updates.invalidate_tasks()  # Application explicitly superseded the task.
            release.set()
            await wait_until(lambda: bool(output))
            assert [event["delegation_id"] for event in output] == ["second"]
            assert calls == ["first", "second"]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_result_chunks_preserve_content_under_the_append_bound() -> None:
    assert live.result_chunks("") == []
    unicode_text = "é" * 1000
    parts = live.result_chunks(unicode_text)
    assert "".join(parts) == unicode_text
    assert len(parts) > 1
    assert all(len(part.encode("utf-8")) <= 480 for part in parts)

    words = "word " * 200
    word_parts = live.result_chunks(words)
    assert "".join(word_parts) == words
    assert all(len(part.encode("utf-8")) <= 480 for part in word_parts)


def test_backend_long_result_is_chunked_across_appends() -> None:
    async def scenario() -> None:
        output: list[dict[str, object]] = []
        text = "é" * 1000

        async def backend(context: str, identifier: str) -> str:
            return text

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(transcript("session.input_transcript.delta", "Question", 0))
        client.observe(delegation("task"))
        worker = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: bool(output))
            await asyncio.sleep(0.05)
            assert len(output) > 1
            assert "".join(str(event["content"]) for event in output) == text
            assert all(len(str(event["content"]).encode("utf-8")) <= 480 for event in output)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_backend_empty_result_sends_nothing() -> None:
    async def scenario() -> None:
        output: list[dict[str, object]] = []
        calls = 0

        async def backend(context: str, identifier: str) -> str:
            nonlocal calls
            calls += 1
            return ""

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(transcript("session.input_transcript.delta", "Question", 0))
        client.observe(delegation("task"))
        worker = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: calls == 1)
            await asyncio.sleep(0.05)
            assert output == []
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_backend_failure_is_redacted() -> None:
    async def scenario() -> None:
        output: list[dict[str, object]] = []

        async def backend(context: str, identifier: str) -> str:
            raise RuntimeError("private backend detail")

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(transcript("session.input_transcript.delta", "Question", 0))
        client.observe(delegation("task"))
        worker = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: bool(output))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        content = str(output[0]["content"])
        assert content == "The backend could not complete this request."
        assert "private backend detail" not in content

    asyncio.run(scenario())


def test_delegation_before_transcript_waits_for_ready() -> None:
    async def scenario() -> None:
        output: list[dict[str, object]] = []
        calls = 0

        async def backend(context: str, identifier: str) -> str:
            nonlocal calls
            calls += 1
            return "ready now"

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(delegation("task"))  # May arrive before its transcript.
        worker = asyncio.create_task(client.run())
        try:
            await asyncio.sleep(0.05)
            assert calls == 0
            client.observe(transcript("session.input_transcript.delta", "Question", 0))
            await wait_until(lambda: bool(output))
            assert calls == 1
            assert output[0]["delegation_id"] == "task"
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_application_submission_routes_to_backend_with_null_delegation() -> None:
    async def scenario() -> None:
        output: list[dict[str, object]] = []
        seen: list[str] = []

        async def backend(context: str, identifier: str) -> str:
            seen.append(identifier)
            return "application result"

        client = live.ClientDelegations(backend, collect_into(output))
        client.observe(transcript("session.input_transcript.delta", "Question", 0))
        identifier = await client.submit([TextContent(text="Do it")])
        assert identifier.startswith("application:")
        worker = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: bool(output))
            assert seen == [identifier]
            assert output[0]["delegation_id"] is None
            assert output[0]["content"] == "application result"
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


class _StubAgent:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.calls: list[tuple[object, str | None]] = []

    async def run(
        self, user_message: object = None, session_id: str | None = None, **kwargs: object
    ) -> AsyncIterator[object]:
        self.calls.append((user_message, session_id))
        for event in self._events:
            yield event


def test_infer_tags_tool_events_and_returns_final_text() -> None:
    async def scenario() -> None:
        emitted: list[AgentEvent] = []

        async def emit(event: AgentEvent) -> None:
            emitted.append(event)

        stub = _StubAgent(
            [
                ToolCallEvent(id="call-1", name="work", arguments={"value": 1}),
                ToolResultEvent(id="call-1", name="work", result="ok"),
                DoneEvent(final_text="It is noon"),
            ]
        )
        result = await live.infer(cast("Agent", stub), "what time is it", "dg-1", emit, session_id="saved-session")

        assert result == "It is noon"
        assert stub.calls[0][1] == "saved-session"
        assert "what time is it" in str(stub.calls[0][0])  # Transcript carried into the backend.
        # Every backend event bubbles up tagged and in order, including completion.
        assert [type(event).__name__ for event in emitted] == ["ToolCallEvent", "ToolResultEvent", "DoneEvent"]
        assert all(event.extra["delegation_id"] == "dg-1" for event in emitted)
        assert all(event.extra["source"] == "live_backend" for event in emitted)
        assert isinstance(emitted[2], DoneEvent) and emitted[2].final_text == "It is noon"
        # The delegation payload only carries the ID, never the raw task text.
        assert "what time is it" not in json.dumps(emitted[0].extra)

    asyncio.run(scenario())


def test_infer_emits_error_then_raises() -> None:
    async def scenario() -> None:
        emitted: list[AgentEvent] = []

        async def emit(event: AgentEvent) -> None:
            emitted.append(event)

        stub = _StubAgent([ErrorEvent(message="backend exploded")])
        with pytest.raises(RuntimeError, match="Backend inference failed"):
            await live.infer(cast("Agent", stub), "question", "dg-2", emit)
        assert isinstance(emitted[0], ErrorEvent)
        assert emitted[0].extra == {"delegation_id": "dg-2", "source": "live_backend"}

    asyncio.run(scenario())


def test_infer_requires_a_completed_result() -> None:
    async def scenario() -> None:
        async def emit(event: AgentEvent) -> None:
            pass

        stub = _StubAgent([ToolCallEvent(id="call-1", name="work", arguments={})])
        with pytest.raises(RuntimeError, match="ended without a result"):
            await live.infer(cast("Agent", stub), "question", "dg-3", emit)

    asyncio.run(scenario())


def test_infer_reuses_one_backend_session_across_delegations() -> None:
    async def scenario() -> None:
        async def emit(event: AgentEvent) -> None:
            pass

        stub = _StubAgent([DoneEvent(final_text="ok")])
        await live.infer(cast("Agent", stub), "one", "dg-1", emit, session_id="shared")
        await live.infer(cast("Agent", stub), "two", "dg-2", emit, session_id="shared")
        assert [session for _, session in stub.calls] == ["shared", "shared"]

        generated = _StubAgent([DoneEvent(final_text="ok")])
        await live.infer(cast("Agent", generated), "one", "dg-3", emit)
        await live.infer(cast("Agent", generated), "two", "dg-4", emit)
        first, second = (session for _, session in generated.calls)
        assert first and second and first != second

    asyncio.run(scenario())


def test_hosted_handshake_audio_and_cancel_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        captured: list[dict[str, object]] = []
        authorization: list[str] = []
        queries: list[dict[str, str]] = []
        raw: list[dict[str, object]] = []
        ready = asyncio.Event()
        speaker = CollectingSpeaker(ready)

        async def handle(request: web.Request) -> web.WebSocketResponse:
            authorization.append(request.headers["Authorization"])
            queries.append(dict(request.query))
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            captured.append(await socket.receive_json())
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            while True:
                event = await socket.receive_json()
                captured.append(event)
                if event["type"] == "session.input_audio.append":
                    await socket.send_json({"type": "session.output_audio.delta", "delta": event["audio"]})
                if event["type"] == "session.close":
                    await socket.send_json({"type": "session.closed", "usage": {"duration": 1}})
                    break
            return socket

        async with live_server(monkeypatch, handle):
            connection = live._LiveConnection(
                "test-key",
                live.ResponsesDelegation("backend-model", web_search=True),
                model="voice-model",
                voice="cedar",
                on_event=collect_into(raw),
                on_backend_event=discard_backend_event,
                updates=_LiveUpdates(),
                options=LiveConfig(voice="cedar"),
            )
            task = asyncio.create_task(
                connection.run(AudioDuplex(input=BlockingMic([b"\x01", b"\x02\x03\x04"]), output=speaker))
            )
            try:
                await asyncio.wait_for(ready.wait(), HANG_GUARD)
                task.cancel()  # Same cancellation used by asyncio.run() on Ctrl+C.
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, HANG_GUARD)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        assert authorization == ["Bearer test-key"]
        assert queries == [{}]
        start = captured[0]
        assert start["type"] == "session.start"
        session = cast("dict[str, object]", start["session"])
        assert session["model"] == "voice-model"
        audio = cast("dict[str, object]", session["audio"])
        assert audio["format"] == {"type": "audio/pcm", "rate": 24000}
        assert cast("dict[str, object]", audio["output"])["voice"] == "cedar"
        assert cast("dict[str, object]", session["delegation"]) == {
            "type": "responses",
            "responses": {
                "model": "backend-model",
                "instructions": "Return concise verified facts and status.",
                "tools": [{"type": "web_search"}],
                "tool_choice": "auto",
            },
        }
        assert speaker.chunks == [b"\x01\x02\x03\x04"]  # Odd chunks reassembled into PCM16 samples.
        assert speaker.closed is True
        assert captured[-1]["type"] == "session.close"
        assert [event["type"] for event in raw][:2] == ["session.started", "session.output_audio.delta"]

    asyncio.run(scenario())


def test_handshake_rejects_unexpected_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        raw: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({"type": "error", "message": "nope"})
            return socket

        async with live_server(monkeypatch, handle):
            connection = live._LiveConnection(
                "test-key",
                live.ResponsesDelegation("backend-model"),
                on_event=collect_into(raw),
                on_backend_event=discard_backend_event,
                updates=_LiveUpdates(),
            )
            with pytest.raises(RuntimeError, match="did not start"):
                await connection.run(AudioDuplex(input=BytesAudioInput(b""), output=CollectingSpeaker()))
        assert raw and raw[0]["type"] == "error"

    asyncio.run(scenario())


def test_server_error_event_is_forwarded_without_killing_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        raw: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            await socket.send_json({"type": "error", "message": "bad command"})
            await socket.send_json({"type": "session.closed", "usage": {"duration": 1}})
            return socket

        async with live_server(monkeypatch, handle):
            connection = live._LiveConnection(
                "test-key",
                live.ResponsesDelegation("backend-model"),
                on_event=collect_into(raw),
                on_backend_event=discard_backend_event,
                updates=_LiveUpdates(),
            )
            await connection.run(AudioDuplex(input=BytesAudioInput(b""), output=CollectingSpeaker()))
        assert [event["type"] for event in raw] == ["session.started", "error", "session.closed"]

    asyncio.run(scenario())


def test_send_audio_rejects_incomplete_samples_and_none_input() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []
        with pytest.raises(ValueError, match="incomplete sample"):
            await live.send_audio(AudioDuplex(input=BytesAudioInput(b"\x01")), collect_into(sent))
        assert sent == []

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await live.send_audio(AudioDuplex(), collect_into(sent))

    asyncio.run(scenario())


def test_agent_run_streams_typed_live_events_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            start = await socket.receive_json()
            assert start["type"] == "session.start"
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            await socket.send_json(
                {"type": "session.input_transcript.delta", "delta": "hello", "start_ms": 0, "end_ms": 5}
            )
            await socket.send_json(
                {"type": "session.output_transcript.delta", "delta": "hi there", "start_ms": 5, "end_ms": 10}
            )
            await socket.send_json(
                {"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x00\x01").decode()}
            )
            await socket.send_json({"type": "session.custom.note", "value": 1})
            await socket.send_json({"type": "session.closed", "usage": {"duration": 2}})
            return socket

        log = tmp_path / "live.jsonl"
        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=LiveConfig(delegation="responses")
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "main.db"),
            system_prompt="Be brief.",
            compactor=None,
            audio=AudioDuplex(input=BytesAudioInput(b""), output=BytesAudioOutput()),
        )
        async with live_server(monkeypatch, handle):
            try:
                events = [event async for event in agent.run(log_file=log)]
            finally:
                await agent.close()

        assert [type(event).__name__ for event in events] == [
            "LiveEvent",
            "InputTranscriptDeltaEvent",
            "AudioTranscriptDeltaEvent",
            "AudioChunkEvent",
            "LiveEvent",
            "LiveEvent",
        ]
        assert isinstance(events[0], LiveEvent) and events[0].event_type == "session.started"
        assert isinstance(events[1], InputTranscriptDeltaEvent) and events[1].delta == "hello"
        assert isinstance(events[2], AudioTranscriptDeltaEvent) and events[2].delta == "hi there"
        assert isinstance(events[3], AudioChunkEvent)
        assert events[3].chunk == base64.b64encode(b"\x00\x01").decode()
        assert isinstance(events[4], LiveEvent) and events[4].event_type == "session.custom.note"
        assert isinstance(events[5], LiveEvent) and events[5].event_type == "session.closed"
        assert events[1].extra["start_ms"] == 0

        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [line["type"] for line in lines] == [
            "session.started",
            "session.input_transcript.delta",
            "session.output_transcript.delta",
            "session.output_audio.delta",
            "session.custom.note",
            "session.closed",
        ]

    asyncio.run(scenario())


def test_client_backend_runs_while_audio_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        captured: list[dict[str, object]] = []
        raw: list[dict[str, object]] = []
        backend_events: list[object] = []
        first_started = asyncio.Event()
        release = asyncio.Event()
        audio_echoed = asyncio.Event()
        infer_ids: list[str] = []
        infer_sessions: list[str] = []

        async def fake_infer(
            agent: Agent,
            transcript_text: str,
            identifier: str,
            emit: Callable[[AgentEvent], Awaitable[None]],
            *,
            session_id: str = "",
        ) -> str:
            infer_ids.append(identifier)
            infer_sessions.append(session_id)
            if len(infer_ids) == 1:
                first_started.set()
                await release.wait()
            return f"result-{identifier}"

        monkeypatch.setattr(live, "infer", fake_infer)

        async def on_backend_event(event: AgentEvent) -> None:
            backend_events.append(event)

        def commentary_count() -> int:
            return sum(1 for event in captured if event["type"] == "session.commentary.append")

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            await socket.send_json(
                {"type": "session.input_transcript.delta", "delta": "what time", "start_ms": 0, "end_ms": 5}
            )
            await socket.send_json(delegation("dg-1"))
            await socket.send_json(delegation("dg-2"))
            while True:
                event = await socket.receive_json()
                captured.append(event)
                if event["type"] == "session.input_audio.append":
                    await socket.send_json({"type": "session.output_audio.delta", "delta": event["audio"]})
                    audio_echoed.set()
                if commentary_count() >= 2:
                    await socket.send_json({"type": "session.closed", "usage": {"duration": 1}})
                    break
            return socket

        backend = Agent(
            Provider(ProviderType.OPENAI_COMPATIBLE, "offline-key", "offline-model"),
            SessionManager(tmp_path / "backend.db"),
            compactor=None,
        )
        speaker = CollectingSpeaker()
        async with live_server(monkeypatch, handle):
            connection = live._LiveConnection(
                "test-key",
                backend,
                model="voice-model",
                on_event=collect_into(raw),
                on_backend_event=on_backend_event,
                updates=_LiveUpdates(),
            )
            task = asyncio.create_task(connection.run(AudioDuplex(input=BlockingMic([b"\x01\x02"]), output=speaker)))
            try:
                await asyncio.wait_for(first_started.wait(), HANG_GUARD)
                await asyncio.wait_for(audio_echoed.wait(), HANG_GUARD)  # Audio keeps flowing.
                release.set()
                await asyncio.wait_for(task, HANG_GUARD)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        assert infer_ids == ["dg-1", "dg-2"]  # Exactly one backend run per delegation.
        assert len(set(infer_sessions)) == 1 and infer_sessions[0]
        commentaries = [event for event in captured if event["type"] == "session.commentary.append"]
        assert [event["delegation_id"] for event in commentaries] == ["dg-1", "dg-2"]
        assert [event["content"] for event in commentaries] == ["result-dg-1", "result-dg-2"]
        assert all(set(event) == {"type", "event_id", "delegation_id", "content"} for event in commentaries)
        assert speaker.chunks  # Playback continued while the backend was busy.

    asyncio.run(scenario())


def test_early_iterator_close_finalizes_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        captured: list[dict[str, object]] = []
        speaker = CollectingSpeaker()

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            while True:
                event = await socket.receive_json()
                captured.append(event)
                if event["type"] == "session.close":
                    await socket.send_json({"type": "session.closed", "usage": {"duration": 1}})
                    break
            return socket

        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=LiveConfig(delegation="responses")
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "main.db"),
            compactor=None,
            audio=AudioDuplex(input=BytesAudioInput(b""), output=speaker),
        )
        async with live_server(monkeypatch, handle):
            stream = agent.run()
            first = await asyncio.wait_for(stream.__anext__(), HANG_GUARD)
            assert isinstance(first, LiveEvent) and first.event_type == "session.started"
            await asyncio.wait_for(stream.aclose(), HANG_GUARD)
        await agent.close()

        assert captured and captured[-1]["type"] == "session.close"
        assert speaker.closed is True

    asyncio.run(scenario())


async def drain(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


def test_live_voice_configuration_errors(tmp_path: Path) -> None:
    def voice_provider(config: LiveConfig) -> Provider:
        return Provider(ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=config)

    hostless = Agent(
        voice_provider(LiveConfig(delegation="responses")),
        SessionManager(tmp_path / "no-audio.db"),
        compactor=None,
    )
    with pytest.raises(ValueError, match="Voice mode requires"):
        asyncio.run(drain(hostless.run()))

    # Client mode without a delegation agent is allowed: hosted tools and a null-ID
    # application update path remain available (see runtime client_handler).
    clientless = Agent(
        voice_provider(LiveConfig(delegation="client")),
        SessionManager(tmp_path / "no-delegate.db"),
        compactor=None,
        audio=AudioDuplex(input=BytesAudioInput(b""), output=BytesAudioOutput()),
    )
    with pytest.raises(ValueError, match="continuous audio"):
        asyncio.run(drain(clientless.run(auto_commit=True)))

    delegate = Agent(
        voice_provider(LiveConfig(delegation="client")),
        SessionManager(tmp_path / "delegate.db"),
        compactor=None,
    )
    hosted_with_delegate = Agent(
        voice_provider(LiveConfig(delegation="responses")),
        SessionManager(tmp_path / "hosted.db"),
        compactor=None,
        delegation_agent=delegate,
        audio=AudioDuplex(input=BytesAudioInput(b""), output=BytesAudioOutput()),
    )
    with pytest.raises(ValueError, match="Select client mode for a delegation_agent"):
        asyncio.run(drain(hosted_with_delegate.run()))


def test_live_audio_format_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        connection = live._LiveConnection(
            "test-key",
            live.ResponsesDelegation("backend-model"),
            on_backend_event=discard_backend_event,
            updates=_LiveUpdates(),
        )
        mismatched = AudioDuplex(
            input=BytesAudioInput(b"", audio_format=AudioFormat(sample_rate=24000)),
            output=BytesAudioOutput(AudioFormat(sample_rate=16000)),
        )
        with pytest.raises(ValueError, match="input and output audio formats must match"):
            await connection.run(mismatched)

        unsupported = AudioDuplex(
            input=BytesAudioInput(b"", audio_format=AudioFormat(sample_rate=44100)),
            output=BytesAudioOutput(AudioFormat(sample_rate=44100)),
        )
        with pytest.raises(ValueError, match="Unsupported GPT-Live audio format"):
            await connection.run(unsupported)

    asyncio.run(scenario())


def test_live_updates_validation_and_payload() -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        updates = _LiveUpdates()
        with pytest.raises(RuntimeError, match=r"wait for session\.started"):
            await updates.append("commentary", "hi")
        updates.sender = send
        for bad in ("", "   "):
            with pytest.raises(ValueError, match="nonempty text"):
                await updates.append("commentary", bad)
        with pytest.raises(ValueError, match="Unknown client delegation ID"):
            await updates.append("commentary", "ok", "unknown")
        with pytest.raises(ValueError, match="Unknown Live append channel"):
            await updates.append("nope", "ok")

        # The 500-token ceiling is enforced by the server, not a local 480-byte cap.
        long_content = "x" * 3000
        event_id = await updates.append("commentary", long_content)
        assert sent[0] == {
            "type": "session.commentary.append",
            "event_id": event_id,
            "delegation_id": None,
            "content": long_content,
        }
        updates.delegations.add("dg-1")
        second = await updates.append("thinking", "note", "dg-1")
        assert sent[1]["type"] == "session.thinking.append"
        assert sent[1]["delegation_id"] == "dg-1" and sent[1]["event_id"] == second
        third = await updates.append("instructions", "be brief", "dg-1")
        assert sent[2]["type"] == "session.instructions.append" and sent[2]["event_id"] == third

    asyncio.run(scenario())


def test_append_update_scope_and_cross_connection(tmp_path: Path) -> None:
    async def scenario() -> None:
        sent: list[dict[str, object]] = []

        async def send(event: dict[str, object]) -> None:
            sent.append(event)

        async def other_send(event: dict[str, object]) -> None:
            pass

        updates = _LiveUpdates()
        updates.sender = send
        updates.delegations.add("dg-1")
        foreign = _LiveUpdates()
        foreign.sender = other_send
        backend = Agent(
            Provider(ProviderType.OPENAI_COMPATIBLE, "offline-key", "offline-model"),
            SessionManager(tmp_path / "backend.db"),
            compactor=None,
        )
        other = Agent(
            Provider(ProviderType.OPENAI_COMPATIBLE, "offline-key", "offline-model"),
            SessionManager(tmp_path / "other.db"),
            compactor=None,
        )

        with pytest.raises(RuntimeError, match="No active GPT-Live connection for this agent"):
            await other.add_comment("no connection")

        token = live._delegation_scope.set(live._DelegationScope(updates, backend, "dg-1"))
        try:
            first = await backend.add_comment("from backend")
            assert sent[-1]["delegation_id"] == "dg-1" and sent[-1]["event_id"] == first
            second = await backend.add_thinking("more")
            assert sent[-1]["delegation_id"] == "dg-1" and sent[-1]["event_id"] == second  # Same ID reused.
            with pytest.raises(RuntimeError, match="No active GPT-Live connection for this agent"):
                await other.add_instructions("nope")
            other._live_updates = foreign
            with pytest.raises(RuntimeError, match="another Live connection"):
                await other.add_comment("cross connection")
            other._live_updates = updates
        finally:
            live._delegation_scope.reset(token)

        # Outside delegated work the application update is session-wide (null ID).
        await other.add_comment("application")
        assert sent[-1]["delegation_id"] is None

        updates.sender = None
        with pytest.raises(RuntimeError, match=r"wait for session\.started"):
            await other.add_comment("after close")

    asyncio.run(scenario())


class _StreamingStub:
    def __init__(self, before: list[object], gate: asyncio.Event, after: list[object]) -> None:
        self._before = before
        self._gate = gate
        self._after = after

    async def run(
        self, user_message: object = None, session_id: str | None = None, **kwargs: object
    ) -> AsyncIterator[object]:
        for event in self._before:
            yield event
        await self._gate.wait()
        for event in self._after:
            yield event


def test_infer_streams_every_event_type_in_real_time_while_blocked() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        emitted: list[AgentEvent] = []

        async def emit(event: AgentEvent) -> None:
            emitted.append(event)

        stub = _StreamingStub(
            [TextChunkEvent(chunk="private text")],
            gate,
            [
                ReasoningChunkEvent(chunk="private reasoning"),
                ToolCallEvent(id="call-1", name="work", arguments={"value": 1}),
                ToolResultEvent(id="call-1", name="work", result="ok"),
                CompactionStartedEvent(message_count=2),
                TextDoneEvent(text="done", usage=Usage(total_tokens=7)),
                DoneEvent(final_text="final"),
            ],
        )
        task = asyncio.create_task(live.infer(cast("Agent", stub), "question", "dg-9", emit, session_id="s"))
        try:
            await wait_until(lambda: len(emitted) >= 1)
            assert isinstance(emitted[0], TextChunkEvent)
            assert not task.done()  # Events arrive before the backend completes.
        finally:
            gate.set()
        result = await asyncio.wait_for(task, HANG_GUARD)

        assert result == "final"
        assert [type(event).__name__ for event in emitted] == [
            "TextChunkEvent",
            "ReasoningChunkEvent",
            "ToolCallEvent",
            "ToolResultEvent",
            "CompactionStartedEvent",
            "TextDoneEvent",
            "DoneEvent",
        ]
        assert all(event.extra["delegation_id"] == "dg-9" for event in emitted)
        assert all(event.extra["source"] == "live_backend" for event in emitted)
        assert isinstance(emitted[5], TextDoneEvent) and emitted[5].usage.total_tokens == 7

    asyncio.run(scenario())


def test_infer_recoverable_error_keeps_streaming() -> None:
    async def scenario() -> None:
        emitted: list[AgentEvent] = []

        async def emit(event: AgentEvent) -> None:
            emitted.append(event)

        stub = _StubAgent(
            [
                ErrorEvent(message="transient", recoverable=True),
                TextDoneEvent(text="ok"),
                DoneEvent(final_text="ok"),
            ]
        )
        result = await live.infer(cast("Agent", stub), "question", "dg-4", emit)
        assert result == "ok"
        assert [type(event).__name__ for event in emitted] == ["ErrorEvent", "TextDoneEvent", "DoneEvent"]
        assert all(event.extra["source"] == "live_backend" for event in emitted)

    asyncio.run(scenario())


def test_live_update_methods_are_not_auto_registered_tools(tmp_path: Path) -> None:
    agent = Agent(
        Provider(ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=LiveConfig(delegation="client")),
        SessionManager(tmp_path / "voice.db"),
        compactor=None,
    )
    registered = set(agent.tool_registry.names())
    assert {"add_comment", "add_thinking", "add_instructions"}.isdisjoint(registered)


def test_overlapping_live_runs_rejected(tmp_path: Path) -> None:
    agent = Agent(
        Provider(
            ProviderType.OPENAI_COMPATIBLE, "test-key", "gpt-live-1", live_config=LiveConfig(delegation="responses")
        ),
        SessionManager(tmp_path / "voice.db"),
        compactor=None,
        audio=AudioDuplex(input=BytesAudioInput(b""), output=BytesAudioOutput()),
    )
    agent._live_updates = _LiveUpdates()

    async def scenario() -> None:
        audio = AudioDuplex(input=BytesAudioInput(b""), output=BytesAudioOutput())
        with pytest.raises(RuntimeError, match="already has an active GPT-Live run"):
            await live.run_live(agent, audio).__anext__()

    asyncio.run(scenario())


class ScriptedProvider(Provider):
    def __init__(self, rounds: list[list[Event]]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "offline-key", "offline-model")
        self.rounds = iter(rounds)

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncGenerator[Event, None]:
        for event in next(self.rounds):
            yield event


def test_registered_tool_application_update_and_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        captured: list[dict[str, object]] = []
        events: list[AgentEvent] = []
        app_event_ids: list[str] = []

        def appends() -> list[dict[str, object]]:
            return [
                event
                for event in captured
                if event["type"]
                in {"session.commentary.append", "session.thinking.append", "session.instructions.append"}
            ]

        async def handle(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({"type": "session.started", "session": {"id": "session"}})
            await socket.send_json(
                {"type": "session.input_transcript.delta", "delta": "check clock", "start_ms": 0, "end_ms": 5}
            )
            await socket.send_json(delegation("dg-1"))
            while True:
                event = await socket.receive_json()
                captured.append(event)
                if str(event["type"]) in {
                    "session.commentary.append",
                    "session.thinking.append",
                    "session.instructions.append",
                }:
                    await socket.send_json({"type": "session.update.ack", "client_event_id": event["event_id"]})
                    if event["delegation_id"] == "dg-1" and event["content"] == "final":
                        await socket.send_json({"type": "session.closed", "usage": {"duration": 1}})
                        break
            return socket

        voice = Agent(
            Provider(
                ProviderType.OPENAI_COMPATIBLE,
                "test-key",
                "gpt-live-1",
                live_config=LiveConfig(delegation="client"),
            ),
            SessionManager(tmp_path / "voice.db"),
            system_prompt="Be concise.",
            compactor=None,
            audio=AudioDuplex(input=BlockingMic([b"\x01\x02"]), output=CollectingSpeaker()),
        )

        async def get_utc_time() -> str:
            """An ordinary async tool that also announces progress through the voice agent."""
            await voice.add_comment("nested progress")  # Uses the enclosing delegation's ID.
            return "12:00"

        # Nothing injects update methods automatically; the example registers them explicitly.
        backend = Agent(
            ScriptedProvider(
                [
                    [
                        TextChunkEvent(chunk="secret reasoning"),
                        TextDoneEvent(text="", finish_reason=FinishReason.TOOL_CALLS),
                        ToolCallEvent(id="call-1", name="add_comment", arguments={"content": "Checking"}),
                        ToolCallEvent(id="call-2", name="get_utc_time", arguments={}),
                    ],
                    [
                        TextChunkEvent(chunk="private draft"),
                        TextDoneEvent(text="private draft"),
                        DoneEvent(final_text="final"),
                    ],
                ]
            ),
            SessionManager(tmp_path / "backend.db"),
            tools=[voice.add_comment, get_utc_time],
            compactor=None,
        )
        voice.delegation_agent = backend

        try:
            async with live_server(monkeypatch, handle):
                async with aclosing(voice.run()) as stream:
                    async for event in stream:
                        events.append(event)
                        if isinstance(event, LiveEvent) and event.event_type == "session.started":
                            app_event_ids.append(await voice.add_comment("ready"))
                with pytest.raises(RuntimeError, match="No active GPT-Live connection"):
                    await voice.add_comment("late")
        finally:
            await voice.close()
            await backend.close()

        delegated = [event for event in appends() if event["delegation_id"] == "dg-1"]
        assert any(event["content"] == "Checking" for event in delegated)  # Registered model tool.
        assert any(event["content"] == "nested progress" for event in delegated)  # Call inside another tool.
        assert any(event["content"] == "final" for event in delegated)  # Final backend answer.
        assert any(event["content"] == "ready" and event["delegation_id"] is None for event in appends())

        contents = {str(event["content"]) for event in appends()}
        assert "secret reasoning" not in contents and "private draft" not in contents

        ack_ids = {
            event.payload["client_event_id"]
            for event in events
            if isinstance(event, LiveEvent) and event.event_type == "session.update.ack"
        }
        assert set(app_event_ids) <= ack_ids
        assert {event["event_id"] for event in appends()} <= ack_ids

        assert any(
            isinstance(event, ToolResultEvent) and event.extra.get("delegation_id") == "dg-1" for event in events
        )
        assert any(isinstance(event, DoneEvent) and event.extra.get("delegation_id") == "dg-1" for event in events)
        assert any(
            isinstance(event, TextChunkEvent) and event.extra.get("source") == "live_backend" for event in events
        )

    asyncio.run(scenario())
