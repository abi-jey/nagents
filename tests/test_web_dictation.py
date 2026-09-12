"""Real WAV transcription behind the web boundary, with synthetic audio and fake HTTP."""

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import aiohttp
import aiosqlite
import anyio
import httpx
import pytest

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.dictation import BYTES_PER_SECOND
from nagents.harness.dictation import MAX_RESPONSE_BYTES
from nagents.harness.dictation import VoiceDictation
from nagents.provider import CodexProvider
from nagents.web import dictation as web_dictation
from nagents.web.app import WebState
from nagents.web.wakeups import Chain
from tests.test_dictation import BODY_SECRET
from tests.test_dictation import SECRET
from tests.test_dictation import FakeNetwork
from tests.test_dictation import network as network
from tests.test_dictation import wav_bytes
from tests.test_web import URL
from tests.test_web import ControlledHarness
from tests.test_web import LiveStream
from tests.test_web import client_app
from tests.test_web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp
    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

PATH = "/api/dictation/transcribe"
AUDIO = wav_bytes(frames=48000)  # Three seconds, larger than the ordinary 64 KiB JSON limit.


def configuration(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        auth="api-key",
        dictation_enabled=True,
        dictation_api_key_env="VOICE_TEST_KEY",
        dictation_max_seconds=4,
    )


@pytest.fixture(autouse=True)
def no_capture() -> Iterator[None]:
    with (
        patch.object(VoiceDictation, "capture", side_effect=AssertionError("Never capture a host device")),
        patch.object(VoiceDictation, "_record", side_effect=AssertionError("Never open a host device")),
    ):
        yield


@asynccontextmanager
async def dictation_app(
    tmp_path: Path, config: HarnessConfig | None = None
) -> AsyncIterator[tuple["FastAPI", httpx.AsyncClient, dict[str, str], WebState]]:
    states: list[WebState] = []

    def factory(harness: Harness) -> WebState:
        state = WebState(harness)
        states.append(state)
        return state

    with patch("nagents.web.app.WebState", side_effect=factory):
        async with client_app(tmp_path, config=config or configuration(tmp_path)) as (app, client, headers, _):
            state = states[0]
            yield (
                app,
                client,
                {
                    **headers,
                    "Content-Type": "audio/wav",
                    "X-Ngn-Session": state.harness.session_id,
                    "X-Ngn-Settings-Revision": state.settings.revision,
                },
                state,
            )
    assert state.dictation._active is None
    assert state.dictation._closed


class UploadRequest:
    """Direct ASGI driver with observable body reads and explicit disconnects."""

    def __init__(
        self,
        app: "ASGIApp",
        headers: dict[str, str],
        *,
        extra: tuple[tuple[str, str], ...] = (),
        path: str = PATH,
        query: bytes = b"",
    ) -> None:
        self.input: asyncio.Queue[Message] = asyncio.Queue()
        self.reading = asyncio.Event()
        self.reads = 0
        self.messages: list[Message] = []
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "root_path": "",
            "scheme": "http",
            "http_version": "1.1",
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 10000),
            "headers": [
                *([] if "Host" in headers else [(b"host", b"127.0.0.1:8765")]),
                *((key.lower().encode(), value.encode()) for key, value in headers.items()),
                *((key.lower().encode(), value.encode()) for key, value in extra),
            ],
        }

        async def serve() -> None:
            await app(scope, self.receive, self.send)

        self.task = asyncio.create_task(serve())

    async def receive(self) -> "Message":
        self.reads += 1
        self.reading.set()
        return await self.input.get()

    async def send(self, message: "Message") -> None:
        self.messages.append(message)

    def body(self, body: bytes, *, more: bool = False) -> None:
        self.input.put_nowait({"type": "http.request", "body": body, "more_body": more})

    def disconnect(self) -> None:
        self.input.put_nowait({"type": "http.disconnect"})

    async def response(self) -> httpx.Response:
        await asyncio.wait_for(self.task, 3)
        start = next(message for message in self.messages if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"") for message in self.messages if message["type"] == "http.response.body"
        )
        return httpx.Response(start["status"], headers=start.get("headers", []), content=body)


def test_real_transcription_is_draft_only_and_preserves_codex(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path, replace(configuration(tmp_path), auth="chatgpt")) as (
            _,
            client,
            headers,
            state,
        ):
            harness = state.harness
            provider = harness.agent.provider
            assert isinstance(provider, CodexProvider)
            before = state.settings.snapshot()
            history = await harness.history()
            sessions = await harness.list_sessions()
            cursor = state.wakeups.cursor
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                saved_settings = list(await db.execute_fetchall("SELECT * FROM ngn_web_settings"))
            with (
                patch.object(harness, "run", side_effect=AssertionError("No agent run")),
                patch.object(harness.agent.session, "add_message", side_effect=AssertionError("No saved draft")),
                patch.object(harness.openai_auth, "credentials", AsyncMock(side_effect=AssertionError("No OAuth"))),
                patch.object(provider, "generate", side_effect=AssertionError("No chat request")),
            ):
                bootstrap = (await client.get("/api/bootstrap")).json()
                assert bootstrap["dictation"] == state.settings.dictation_snapshot()
                response = await client.post(PATH, content=AUDIO, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"text": "A synthetic draft."}
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["permissions-policy"] == "microphone=(self), camera=()"
            assert response.headers["x-frame-options"] == "DENY"
            assert "connect-src 'self'" in response.headers["content-security-policy"]
            assert len(network.requests) == 1
            endpoint, request = network.requests[0]
            assert endpoint == "https://api.openai.com/v1/audio/transcriptions"
            assert request["headers"] == {"Authorization": f"Bearer {SECRET}"}
            assert request["allow_redirects"] is False
            assert network.options["trust_env"] is False
            assert network.closed and network.response_closed
            assert state.settings.snapshot() == before
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                assert list(await db.execute_fetchall("SELECT * FROM ngn_web_settings")) == saved_settings
            assert await harness.history() == history
            assert await harness.list_sessions() == sessions
            assert state.wakeups.cursor == cursor and state.active is None and not state.mutating
            assert harness.agent.provider is provider and harness.config.auth == "chatgpt"
            assert state.dictation._active is None

    asyncio.run(check())


@pytest.mark.parametrize(
    ("header", "value", "status"),
    [
        ("Host", "foreign.example", 403),
        ("Origin", "https://foreign.example", 403),
        ("Origin", "null", 403),
        ("Origin", "", 403),
        ("Sec-Fetch-Site", "cross-site", 403),
        ("X-Ngn-Token", "", 403),
        ("X-Ngn-Token", "wrong", 403),
        ("X-Ngn-Session", "", 409),
        ("X-Ngn-Session", "ngn-other-root", 409),
        ("X-Ngn-Session", "ngn-child", 409),
        ("X-Ngn-Settings-Revision", "", 409),
        ("X-Ngn-Settings-Revision", "stale", 409),
        ("Content-Type", "", 415),
        ("Content-Type", "audio/webm", 415),
        ("Content-Type", "audio/wav; codecs=pcm", 415),
        ("Content-Type", "application/json", 415),
        ("Content-Type", "multipart/form-data; boundary=test", 415),
        ("Content-Encoding", "gzip", 415),
        ("Content-Encoding", "identity", 415),
        ("Content-Length", "132097", 413),
        ("Content-Length", "-1", 422),
        ("Content-Length", "1.0", 422),
        ("Content-Length", "1,1", 422),
        ("Content-Length", "x" * 30, 422),
    ],
)
def test_rejections_never_consume_audio(
    tmp_path: Path, network: FakeNetwork, header: str, value: str, status: int
) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, state):
            changed = {**headers, header: value}
            if not value:
                changed.pop(header)
            request = UploadRequest(app, changed)
            response = await request.response()
            assert response.status_code == status
            assert request.reads == 0 and not network.requests
            assert not state.mutating and state.dictation._active is None
            assert SECRET not in response.text and BODY_SECRET not in response.text

    asyncio.run(check())


@pytest.mark.parametrize(
    ("header", "value", "status"),
    [
        ("Host", "127.0.0.1:8765", 403),
        ("Origin", URL, 403),
        ("X-Ngn-Token", "wrong", 403),
        ("X-Ngn-Session", "ngn-other", 409),
        ("X-Ngn-Settings-Revision", "wrong", 409),
        ("Content-Type", "audio/wav", 415),
        ("Content-Encoding", "", 415),
        ("Content-Length", "1", 422),
        ("Transfer-Encoding", "chunked", 422),
    ],
)
def test_duplicate_and_ambiguous_headers(
    tmp_path: Path, network: FakeNetwork, header: str, value: str, status: int
) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, _):
            headers["Content-Length"] = str(len(AUDIO))
            request = UploadRequest(app, headers, extra=((header, value),))
            assert (await request.response()).status_code == status
            assert request.reads == 0 and not network.requests

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["disabled", "demo", "missing-key", "bad-key"])
def test_unavailable_returns_503_without_body_or_auth_fallback(
    tmp_path: Path, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode in {"missing-key", "bad-key"}:
        monkeypatch.setenv("VOICE_TEST_KEY", "" if mode == "missing-key" else "bad\nkey")

    async def check() -> None:
        config = replace(
            configuration(tmp_path), auth="chatgpt", dictation_enabled=mode != "disabled", demo=mode == "demo"
        )
        async with dictation_app(tmp_path, config) as (app, _, headers, state):
            with patch.object(
                state.harness.openai_auth, "credentials", AsyncMock(side_effect=AssertionError("No OAuth"))
            ):
                request = UploadRequest(app, headers)
                response = await request.response()
            assert response.status_code == 503 and isinstance(response.json()["detail"], str)
            assert request.reads == 0 and not network.requests
            assert not state.mutating
            if mode in {"missing-key", "bad-key"}:
                assert "VOICE_TEST_KEY" in response.text

    asyncio.run(check())


@pytest.mark.parametrize(
    "audio",
    [
        b"",
        b"/etc/shadow",
        b'{"path":"/etc/shadow","base_url":"https://foreign.example"}',
        b"not-wav" * 100,
        wav_bytes(rate=48000),
        wav_bytes(channels=2),
        wav_bytes(width=1),
        wav_bytes(frames=64001),
        AUDIO[:-1],
    ],
    ids=["empty", "path", "json-controls", "malformed", "rate", "stereo", "width", "duration", "truncated"],
)
def test_invalid_wav_is_422_without_upstream(tmp_path: Path, network: FakeNetwork, audio: bytes) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (_, client, headers, state):
            response = await client.post(PATH, content=audio, headers=headers)
            assert response.status_code == 422
            assert not network.requests and not state.mutating

    asyncio.run(check())


@pytest.mark.parametrize("declared", [False, True])
def test_actual_chunk_bytes_enforce_limit_without_trusting_content_length(
    tmp_path: Path, network: FakeNetwork, declared: bool
) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, state):
            if declared:
                headers["Content-Length"] = "10"
            request = UploadRequest(app, headers)
            request.body(b"x" * (4 * BYTES_PER_SECOND), more=True)
            request.body(b"x" * 4097, more=True)
            response = await request.response()
            assert response.status_code == 413 and request.reads == 2
            assert not network.requests and not state.mutating

    asyncio.run(check())


def test_short_content_length_and_body_deadline(
    tmp_path: Path, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, state):
            mismatch = UploadRequest(app, {**headers, "Content-Length": str(len(AUDIO) + 1)})
            mismatch.body(AUDIO)
            assert (await mismatch.response()).status_code == 422
            monkeypatch.setattr(web_dictation, "BODY_SECONDS", 0.05)
            slow = UploadRequest(app, headers)
            slow.body(AUDIO[:100], more=True)
            response = await slow.response()
            assert response.status_code == 408
            assert not network.requests and not state.mutating

    asyncio.run(check())


def test_chunked_success_and_metadata_removed(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, _):
            metadata = BODY_SECRET.encode()
            chunk = b"JUNK" + len(metadata).to_bytes(4, "little") + metadata + b"\x00" * (len(metadata) % 2)
            audio = AUDIO[:4] + (len(AUDIO) + len(chunk) - 8).to_bytes(4, "little") + AUDIO[8:] + chunk
            request = UploadRequest(app, headers)
            for offset in range(0, len(audio), 8192):
                request.body(audio[offset : offset + 8192], more=offset + 8192 < len(audio))
            assert (await request.response()).json() == {"text": "A synthetic draft."}
            form = network.requests[0][1]["data"]
            assert isinstance(form, aiohttp.FormData)
            assert form._fields[0][2] == AUDIO
            assert form._fields[0][0]["filename"] == "dictation.wav"

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["body", "upstream"])
def test_admission_blocks_all_mutations_before_second_body(tmp_path: Path, network: FakeNetwork, phase: str) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, client, headers, state):
            network.release.clear()
            first = UploadRequest(app, headers)
            if phase == "body":
                await asyncio.wait_for(first.reading.wait(), 3)
            else:
                first.body(AUDIO)
                await asyncio.wait_for(network.entered.wait(), 3)
            assert state.mutating
            second = UploadRequest(app, headers)
            assert (await second.response()).status_code == 409
            assert second.reads == 0
            json_headers = {**headers, "Content-Type": "application/json"}
            for path, body in (
                ("/api/run", {"session_id": state.harness.session_id, "prompt": "Do not run"}),
                ("/api/sessions/new", {}),
                ("/api/sessions/resume", {"session_id": state.harness.session_id}),
                ("/api/settings", {"revision": state.settings.revision, "values": state.settings.values.model_dump()}),
                ("/api/settings/reset", {"revision": state.settings.revision}),
            ):
                assert (await client.post(path, json=body, headers=json_headers)).status_code == 409
            assert state.active is None
            first.disconnect()
            assert (await first.response()).status_code == 400
            assert not state.mutating and state.dictation._active is None
            assert len(network.requests) == int(phase == "upstream")
            assert (await client.get("/api/sessions", headers=headers)).status_code == 200

    asyncio.run(check())


class SlowCleanupNetwork(FakeNetwork):
    def __init__(self) -> None:
        super().__init__()
        self.release.clear()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.exit_calls = 0

    async def __aexit__(self, *args: object) -> None:
        self.exit_calls += 1
        self.cleanup_entered.set()
        await self.cleanup_release.wait()
        await super().__aexit__(*args)


@pytest.mark.parametrize("action", ["disconnect", "cancel", "shutdown"])
def test_upstream_cleanup_is_joined_even_with_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async def check() -> None:
        network = SlowCleanupNetwork()
        monkeypatch.setattr(aiohttp, "ClientSession", network)
        async with dictation_app(tmp_path) as (app, _, headers, state):
            request = UploadRequest(app, headers)
            request.body(AUDIO)
            await asyncio.wait_for(network.entered.wait(), 3)
            active = state.dictation._active
            assert active is not None
            closing: list[asyncio.Task[None]] = []
            if action == "shutdown":
                closing.append(asyncio.create_task(state.dictation.close()))
            elif action == "disconnect":
                request.disconnect()
            else:
                request.task.cancel()
            try:
                await asyncio.wait_for(network.cleanup_entered.wait(), 3)
                request.task.cancel()
                await asyncio.sleep(0)
                request.task.cancel()
                if closing:
                    closing[0].cancel()
                    await asyncio.sleep(0)
                    closing[0].cancel()
                await asyncio.sleep(0)
                assert not request.task.done() and state.mutating
                assert not active.finished.is_set() and not network.closed
                rejected = UploadRequest(app, headers)
                assert (await rejected.response()).status_code == 409 and rejected.reads == 0
                assert all(not task.done() for task in closing)
            finally:
                network.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request.task, 3)
            for task in closing:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            assert network.closed and network.exit_calls == 1
            assert active.task.done() and active.finished.is_set()
            assert active.voice._closed and active.voice._upload_task is None
            assert not state.mutating and state.dictation._active is None

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["body", "upstream"])
def test_lifespan_shutdown_joins_upload_before_harness_close(
    tmp_path: Path, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def check() -> None:
        network.release.clear()
        async with dictation_app(tmp_path) as (app, _, headers, state):
            request = UploadRequest(app, headers)
            if phase == "upstream":
                request.body(AUDIO)
                await asyncio.wait_for(network.entered.wait(), 3)
            else:
                request.body(AUDIO[:100], more=True)
                await asyncio.wait_for(request.reading.wait(), 3)
            active = state.dictation._active
            assert active is not None and not state.harness._closed
            close = state.harness.close

            async def close_harness() -> None:
                assert active.finished.is_set() and active.voice._closed and not state.mutating
                if phase == "upstream":
                    assert network.closed
                await close()

            monkeypatch.setattr(state.harness, "close", close_harness)
        assert state.harness._closed and not state.mutating
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request.task, 3)
        assert active.task.done() and active.voice._closed and active.finished.is_set()
        assert len(network.requests) == int(phase == "upstream")
        if phase == "upstream":
            assert network.closed

    asyncio.run(check())


@pytest.mark.parametrize("cancellation", ["once", "repeated", "anyio"])
def test_cancelled_lifespan_still_closes_harness_provider_and_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    async def check() -> None:
        baseline = asyncio.all_tasks()
        network = SlowCleanupNetwork()
        monkeypatch.setattr(aiohttp, "ClientSession", network)
        context = dictation_app(tmp_path, replace(configuration(tmp_path), auth="chatgpt"))
        app, _, headers, state = await context.__aenter__()
        harness = state.harness
        assert isinstance(harness, ControlledHarness)
        provider = harness.agent.provider
        assert isinstance(provider, CodexProvider)
        provider_entered = asyncio.Event()
        provider_release = asyncio.Event()
        if cancellation != "repeated":
            provider_release.set()
        close_provider = provider.close
        scopes: list[anyio.CancelScope] = []
        completed = False

        async def delayed_provider_close() -> None:
            assert network.closed and state.dictation._active is None and not state.mutating
            provider_entered.set()
            await provider_release.wait()
            await close_provider()

        async def exit_lifespan() -> None:
            nonlocal completed
            if cancellation == "anyio":
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await context.__aexit__(None, None, None)
                    completed = True
                assert scope.cancelled_caught
            else:
                await context.__aexit__(None, None, None)
                completed = True

        request = UploadRequest(app, headers)
        request.body(AUDIO)
        await asyncio.wait_for(network.entered.wait(), 3)
        upload = state.dictation._active
        assert upload is not None
        with (
            patch.object(provider, "close", AsyncMock(side_effect=delayed_provider_close)) as provider_closed,
            patch.object(harness.openai_auth, "close", AsyncMock(wraps=harness.openai_auth.close)) as auth_closed,
            patch.object(harness.tasks, "end", AsyncMock(wraps=harness.tasks.end)) as tasks_closed,
        ):
            shutdown = asyncio.create_task(exit_lifespan())
            try:
                await asyncio.wait_for(network.cleanup_entered.wait(), 3)
                if cancellation == "anyio":
                    scopes[0].cancel()
                else:
                    shutdown.cancel()
                await asyncio.sleep(0)
                if cancellation == "repeated":
                    shutdown.cancel()
                    await asyncio.sleep(0)
                assert not shutdown.done() and not network.closed and not harness._closed
                network.cleanup_release.set()
                if cancellation == "repeated":
                    await asyncio.wait_for(provider_entered.wait(), 3)
                    assert not shutdown.done() and harness._closed and not harness.closed
                    shutdown.cancel()
                    await asyncio.sleep(0)
                    assert not shutdown.done()
                    provider_release.set()
                if cancellation == "anyio":
                    await asyncio.wait_for(shutdown, 3)
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(shutdown, 3)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(request.task, 3)
                assert not completed
                assert network.closed and network.exit_calls == 1
                assert upload.finished.is_set() and upload.task.done() and upload.voice._closed
                assert state.dictation._closed and state.dictation._active is None and not state.mutating
                assert harness._closed and harness.closed
                assert harness._closing is not None and harness._closing.done()
                assert not harness.agent._initialized
                provider_closed.assert_awaited_once_with()
                auth_closed.assert_awaited_once_with()
                tasks_closed.assert_awaited_once_with()
                assert state.wakeups.closed and all(task.done() for task in state.wakeups._tasks)
            finally:
                network.cleanup_release.set()
                provider_release.set()
                await asyncio.wait_for(asyncio.gather(shutdown, request.task, return_exceptions=True), 3)
                await harness.close()
        assert not (asyncio.all_tasks() - baseline)

    asyncio.run(check())


def test_anyio_disconnect_cancellation_shields_only_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        network = SlowCleanupNetwork()
        monkeypatch.setattr(aiohttp, "ClientSession", network)
        async with dictation_app(tmp_path) as (app, _, headers, state):
            scopes: list[anyio.CancelScope] = []

            async def scoped_app(scope: "Scope", receive: "Receive", send: "Send") -> None:
                with anyio.CancelScope() as cancellation:
                    scopes.append(cancellation)
                    await app(scope, receive, send)

            request = UploadRequest(scoped_app, headers)
            request.body(AUDIO)
            await asyncio.wait_for(network.entered.wait(), 3)
            scopes[0].cancel()
            try:
                await asyncio.wait_for(network.cleanup_entered.wait(), 3)
                await asyncio.sleep(0)
                assert state.mutating and not request.task.done()
            finally:
                network.cleanup_release.set()
            await asyncio.wait_for(request.task, 3)
            assert network.closed and network.exit_calls == 1
            assert not state.mutating and state.dictation._active is None

    asyncio.run(check())


def test_active_run_rejects_upload_before_reading(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, state):
            stream = LiveStream(
                app,
                {name: value for name, value in headers.items() if name != "Content-Type"},
                state.harness.session_id,
                "wait",
            )
            try:
                await stream.event("text_chunk")
                upload = UploadRequest(app, headers)
                assert (await upload.response()).status_code == 409
                assert upload.reads == 0 and not network.requests
            finally:
                await stream.disconnect()

    asyncio.run(check())


def test_due_wakeup_defers_until_upload_cleanup(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, state):
            request = UploadRequest(app, headers)
            await asyncio.wait_for(request.reading.wait(), 3)
            executed = asyncio.Event()
            wakeups = state.wakeups
            wakeups.execute = AsyncMock(side_effect=lambda _: executed.set())
            wakeups.clock = lambda: 0.0
            result = wakeups.schedule(state.harness.session_id, "synthetic-run", Chain(), "", 1, "Synthetic wakeup")
            wakeups.clock = lambda: 2.0
            await wakeups.tick()
            assert not executed.is_set() and result["wakeup_id"] in wakeups.pending
            request.disconnect()
            assert (await request.response()).status_code == 400
            await asyncio.wait_for(executed.wait(), 3)
            assert not state.mutating and not network.requests

    asyncio.run(check())


def test_session_and_settings_changes_invalidate_recording_headers(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, client, headers, state):
            json_headers = {**headers, "Content-Type": "application/json"}
            response = await client.post("/api/sessions/new", json={}, headers=json_headers)
            assert response.status_code == 200
            old_session = UploadRequest(app, headers)
            assert (await old_session.response()).status_code == 409 and old_session.reads == 0
            headers["X-Ngn-Session"] = state.harness.session_id
            response = await client.post(
                "/api/settings",
                json={"revision": state.settings.revision, "values": state.settings.values.model_dump()},
                headers=json_headers,
            )
            assert response.status_code == 200
            old_settings = UploadRequest(app, headers)
            assert (await old_settings.response()).status_code == 409 and old_settings.reads == 0
            assert not network.requests

    asyncio.run(check())


def test_captured_config_is_used_through_delayed_body(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), dictation_language="fr")
        async with dictation_app(tmp_path, config) as (app, _, headers, state):
            with patch.object(state.settings, "dictation_config", wraps=state.settings.dictation_config) as snapshot:
                request = UploadRequest(app, headers)
                await asyncio.wait_for(request.reading.wait(), 3)
                snapshot.assert_called_once_with()
                # Deliberately mutate test state outside the guarded API to prove
                # the request does not reread mutable preferences during upload.
                state.settings.values = state.settings.values.model_copy(update={"dictation_language": "de"})
                state.harness.config.dictation_base_url = "https://changed.example.invalid"
                request.body(AUDIO)
                assert (await request.response()).status_code == 200
                snapshot.assert_called_once_with()
            endpoint, posted = network.requests[0]
            assert endpoint == "https://api.openai.com/v1/audio/transcriptions"
            form = posted["data"]
            assert isinstance(form, aiohttp.FormData)
            fields = {options["name"]: value for options, _, value in form._fields}
            assert fields["language"] == "fr" and fields["model"] == "gpt-4o-mini-transcribe"

    asyncio.run(check())


@pytest.mark.parametrize("key_present", [False, True])
def test_saved_preferences_and_selected_backend_key_drive_upload(
    tmp_path: Path, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch, key_present: bool
) -> None:
    selected_key = "synthetic-selected-transcription-key"
    monkeypatch.setenv("NGN_TRANSCRIPTION_API_KEY", selected_key if key_present else "")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unrelated-platform-key")

    async def check() -> None:
        config = replace(configuration(tmp_path), auth="chatgpt", dictation_api_key_env="NGN_TRANSCRIPTION_API_KEY")
        async with dictation_app(tmp_path, config) as (app, client, headers, state):
            provider = state.harness.agent.provider
            assert isinstance(provider, CodexProvider)
            coding_model = provider.model
            values = {
                **state.settings.values.model_dump(),
                "dictation_model": "gpt-4o-transcribe",
                "dictation_language": "en",
                "dictation_max_seconds": 1,
            }
            saved = await client.post(
                "/api/settings",
                json={"revision": state.settings.revision, "values": values},
                headers={**headers, "Content-Type": "application/json"},
            )
            assert saved.status_code == 200
            projection = saved.json()["dictation"]
            assert projection["max_bytes"] == BYTES_PER_SECOND + 4096
            assert projection["max_seconds"] == 1 and projection["available"] is key_present
            assert projection["api_key_env"] == "NGN_TRANSCRIPTION_API_KEY"
            assert selected_key not in saved.text
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["dictation"] == projection
            stale = UploadRequest(app, headers)
            assert (await stale.response()).status_code == 409 and stale.reads == 0
            headers["X-Ngn-Settings-Revision"] = projection["revision"]
            with patch.object(
                state.harness.openai_auth, "credentials", AsyncMock(side_effect=AssertionError("No OAuth"))
            ):
                if key_present:
                    oversized = UploadRequest(app, {**headers, "Content-Length": str(len(AUDIO))})
                    assert (await oversized.response()).status_code == 413 and oversized.reads == 0
                    too_long = await client.post(PATH, content=wav_bytes(frames=16001), headers=headers)
                    assert too_long.status_code == 422 and not network.requests
                    response = await client.post(PATH, content=wav_bytes(), headers=headers)
                    assert response.status_code == 200 and response.json() == {"text": "A synthetic draft."}
                    assert len(network.requests) == 1
                    posted = network.requests[0][1]
                    assert posted["headers"] == {"Authorization": f"Bearer {selected_key}"}
                    form = posted["data"]
                    assert isinstance(form, aiohttp.FormData)
                    fields = {options["name"]: value for options, _, value in form._fields}
                    assert fields["model"] == "gpt-4o-transcribe" and fields["language"] == "en"
                else:
                    unavailable = UploadRequest(app, headers)
                    response = await unavailable.response()
                    assert response.status_code == 503 and unavailable.reads == 0 and not network.requests
                    assert "NGN_TRANSCRIPTION_API_KEY" in response.text
            assert state.harness.agent.provider is provider and provider.model == coding_model
            assert state.settings.values.model_dump() == values
            assert await state.harness.history() == []
            assert state.active is None and not state.mutating

    asyncio.run(check())


def test_closed_service_rejects_new_body_and_setup_exception_is_safe(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, _, headers, state):
            with patch("nagents.web.dictation.VoiceDictation", side_effect=RuntimeError(BODY_SECRET)):
                broken = UploadRequest(app, headers)
                response = await broken.response()
                assert response.status_code == 503 and BODY_SECRET not in response.text
                assert broken.reads == 0
            await state.dictation.close()
            await state.dictation.close()
            closed = UploadRequest(app, headers)
            assert (await closed.response()).status_code == 503 and closed.reads == 0
            assert not state.mutating and not network.requests

    asyncio.run(check())


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 429, 500])
def test_upstream_errors_are_safe_502_and_never_retried(
    tmp_path: Path, network: FakeNetwork, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    network.status = status
    network.body = BODY_SECRET.encode()

    async def check() -> None:
        async with dictation_app(tmp_path) as (_, client, headers, state):
            response = await client.post(PATH, content=AUDIO, headers=headers)
            assert response.status_code == 502
            assert network.reads == 0 and len(network.requests) == 1
            assert network.closed and not state.mutating
            assert SECRET not in response.text + caplog.text and BODY_SECRET not in response.text + caplog.text

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["timeout", "network", "unexpected", "invalid-json", "oversized", "long-text"])
def test_failures_are_bounded_and_safe(
    tmp_path: Path, network: FakeNetwork, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    if failure == "timeout":
        network.failure = TimeoutError(BODY_SECRET)
    elif failure == "network":
        network.failure = RuntimeError(BODY_SECRET)
    elif failure == "invalid-json":
        network.body = BODY_SECRET.encode()
    elif failure == "oversized":
        network.body = b"x" * (MAX_RESPONSE_BYTES + 1)
    elif failure == "long-text":
        network.body = json.dumps({"text": "x" * 32001}).encode()

    async def check() -> None:
        async with dictation_app(tmp_path) as (_, client, headers, state):
            if failure == "unexpected":
                with patch.object(VoiceDictation, "transcribe", AsyncMock(side_effect=RuntimeError(BODY_SECRET))):
                    response = await client.post(PATH, content=AUDIO, headers=headers)
            else:
                response = await client.post(PATH, content=AUDIO, headers=headers)
            assert response.status_code == 502
            assert SECRET not in response.text + caplog.text and BODY_SECRET not in response.text + caplog.text
            assert not state.mutating and state.dictation._active is None
            assert len(network.requests) == int(failure != "unexpected")

    asyncio.run(check())


def test_only_exact_upload_route_has_audio_exception(tmp_path: Path, network: FakeNetwork) -> None:
    async def check() -> None:
        async with dictation_app(tmp_path) as (app, client, headers, _):
            for path in (PATH + "/", PATH + "/extra", "/api/sessions/new"):
                request = UploadRequest(app, headers, path=path)
                assert (await request.response()).status_code == 415
                assert request.reads == 0
            request = UploadRequest(app, headers, query=b"endpoint=foreign")
            assert (await request.response()).status_code == 400 and request.reads == 0
            response = await client.post(
                "/api/sessions/new", content=b"x" * 65537, headers={**headers, "Content-Type": "application/json"}
            )
            assert response.status_code == 413 and not network.requests

    asyncio.run(check())
