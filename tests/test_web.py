"""The local web boundary and real/demo Harness lifecycle, without provider I/O."""

import asyncio
import builtins
import json
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest
from starlette.requests import ClientDisconnect

from nagents.cli import _parser
from nagents.cli import main
from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.exceptions import ModelListError
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.tools import CodingTools
from nagents.harness.types import HarnessEvent
from nagents.provider import CodexProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message as SessionMessage
from nagents.web import built_assets
from nagents.web import local_authority
from nagents.web import serve
from nagents.web.app import create_app

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import Message
    from starlette.types import Scope

URL = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def no_guarded_workspace_io(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("requires_posix") is not None:
        yield
        return
    # Prove portable cases do not depend on workspace tools, even on a POSIX host.
    with patch.object(
        CodingTools, "directory", side_effect=OSError("Guarded workspace file tools currently require POSIX")
    ) as guarded:
        yield
        guarded.assert_not_called()


class ControlledHarness(Harness):
    """Portable scripted runs; real Harness sessions, approvals, initialization and close."""

    def __init__(self, config: HarnessConfig) -> None:
        super().__init__(config)
        self.stopped = asyncio.Event()
        self.closed = False
        self.decisions: list[bool] = []
        self.loop = asyncio.get_running_loop()

    async def initialize(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        # Replace only workspace discovery on this injected test instance. Keep
        # the real initialization lock and SQLite session/membership lifecycle.
        with patch.object(self.tools, "instructions", return_value=""), patch.object(self.tools, "discover_skills"):
            await super().initialize()

    async def run(self, prompt: str) -> AsyncGenerator[HarnessEvent, None]:
        try:
            yield TextChunkEvent(chunk="partial text")
            if prompt == "approval":
                for _ in range(2):
                    try:
                        await self.approve(
                            "example", {"path": "example.txt"}, "A test operation", "sample diff", "call-1"
                        )
                        self.decisions.append(True)
                    except PermissionError:
                        self.decisions.append(False)
            elif prompt == "error":
                raise RuntimeError("SECRET-test-provider-key")
            else:
                await asyncio.Event().wait()
            yield TextDoneEvent(text="complete")
            yield DoneEvent(session_id=self.session_id)
        finally:
            # A cancellable cleanup turn catches response cancellation bugs.
            await asyncio.sleep(0.01)
            self.stopped.set()

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        await super().close()
        self.closed = True


@asynccontextmanager
async def client_app(
    tmp_path: Path, *, controlled: bool = True, config: HarnessConfig | None = None
) -> AsyncIterator[tuple["FastAPI", httpx.AsyncClient, dict[str, str], list[Harness]]]:
    assets = tmp_path / "static"
    assets.mkdir(exist_ok=True)
    (assets / "assets").mkdir(exist_ok=True)
    (assets / "index.html").write_text('<div id="root"></div><script src="/assets/app.js"></script>')
    (assets / "assets" / "app.js").write_text("// test asset")
    if config is None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
    harnesses: list[Harness] = []

    def factory(config: HarnessConfig) -> Harness:
        harness = ControlledHarness(config) if controlled else Harness(config)
        harnesses.append(harness)
        return harness

    app = create_app(config, assets=assets, harness_factory=factory)
    assert not harnesses  # Construction happens on the lifespan's running loop.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as client,
    ):
        bootstrap = (await client.get("/api/bootstrap")).json()
        headers = {"Origin": URL, "X-Ngn-Token": bootstrap["token"]}
        yield app, client, headers, harnesses
    assert harnesses[0]._closed


class LiveStream:
    """Drive streaming ASGI directly: HTTPX's test transport buffers until EOF."""

    def __init__(
        self, app: "FastAPI", headers: dict[str, str], session_id: str, prompt: str, spec: str = "2.3"
    ) -> None:
        self.disconnected = False
        self.output: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.input: asyncio.Queue[Message] = asyncio.Queue()
        self.input.put_nowait(
            {"type": "http.request", "body": json.dumps({"session_id": session_id, "prompt": prompt}).encode()}
        )
        self.raw = bytearray()
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec},
            "method": "POST",
            "path": "/api/run",
            "raw_path": b"/api/run",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "http_version": "1.1",
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 10000),
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"content-type", b"application/json"),
                *((key.lower().encode(), value.encode()) for key, value in headers.items()),
            ],
        }
        self.task = asyncio.create_task(app(scope, self.input.get, self.send))

    async def send(self, message: "Message") -> None:
        if self.disconnected:
            raise OSError("Client connection closed")
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body":
            self.raw.extend(message.get("body", b""))
            while b"\n" in self.raw:
                line, _, rest = self.raw.partition(b"\n")
                self.raw = bytearray(rest)
                self.output.put_nowait(json.loads(line))

    async def event(self, kind: str) -> dict[str, object]:
        async with asyncio.timeout(5):
            while True:
                event = await self.output.get()
                if event["event"] == kind:
                    return event

    async def disconnect(self) -> None:
        self.disconnected = True
        await self.input.put({"type": "http.disconnect"})
        with suppress(ClientDisconnect):
            await asyncio.wait_for(self.task, 5)


def test_cli_serve_parsing_and_no_textual(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    args = _parser().parse_args(["--demo", "serve", "--port", "9876", "-C", str(tmp_path), "--model", "example"])
    assert args.demo and args.port == 9876 and args.model == "example"
    assert _parser().parse_args(["serve", "--demo"]).demo
    with patch("nagents.web.serve") as start, patch("nagents.cli.Harness", side_effect=AssertionError("wrong loop")):
        assert main(["serve", "--demo", "-C", str(tmp_path)]) == 0
        start.assert_called_once()
        assert start.call_args.kwargs["port"] == 8765


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.2", "evil.localhost", "127.0.0.1.example.com"])
def test_remote_bind_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        local_authority(host, 8765)


def test_missing_dependencies_and_assets(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path, demo=True)
    real_import = builtins.__import__

    def missing(name: str, *args: object, **kwargs: object) -> object:
        if name == "uvicorn":
            raise ModuleNotFoundError("No uvicorn", name="uvicorn")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("builtins.__import__", side_effect=missing),
        pytest.raises(ValueError, match=r"pip install -e '\.\[web\]'"),
    ):
        serve(config)
    with (
        patch("nagents.web.__file__", str(tmp_path / "__init__.py")),
        pytest.raises(ValueError, match="npm --prefix src/nagents/web-ui ci"),
    ):
        built_assets()
    with pytest.raises(ValueError, match="--port"):
        local_authority("127.0.0.1", 0)
    assert local_authority("::1", 8765) == "[::1]:8765"


def test_security_boundary_and_static_paths(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            response = await client.get("/")
            assert response.status_code == 200 and "/assets/app.js" in response.text
            assert response.headers["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert "access-control-allow-origin" not in response.headers
            assert (await client.get("/assets/app.js")).status_code == 200
            for path in [
                "/api/unknown",
                "/api/bootstrap/unknown",
                "/unknown",
                "/assets/%2e%2e/index.html",
                "/assets/%2fetc/passwd",
                "/assets/..%5cindex.html",
            ]:
                assert (await client.get(path, headers=headers)).status_code == 404
            for path in ["/", "/api/bootstrap"]:
                assert (await client.get(path, headers={"Host": "evil.test"})).status_code == 403
                assert (await client.get(path, headers={"Origin": "http://evil.test"})).status_code == 403
                assert (await client.get(path, headers={"Origin": "null"})).status_code == 403
                assert (await client.get(path, headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
            assert (await client.get("/api/sessions")).status_code == 403
            assert (await client.post("/api/sessions/new", json={}, headers={"Origin": URL})).status_code == 403
            assert (
                await client.post("/api/sessions/new", json={}, headers={"X-Ngn-Token": headers["X-Ngn-Token"]})
            ).status_code == 403
            assert (
                await client.post("/api/sessions/new", json={}, headers={**headers, "Origin": "http://localhost:8765"})
            ).status_code == 403
            assert (
                await client.post("/api/sessions/new", content="{}", headers={**headers, "Content-Type": "text/plain"})
            ).status_code == 415
            assert (
                await client.post(
                    "/api/sessions/new", content="x" * 65537, headers={**headers, "Content-Type": "application/json"}
                )
            ).status_code == 413
            assert (
                await client.post("/api/sessions/new", json={"extra": "SECRET"}, headers=headers)
            ).status_code == 422
            assert (await client.get("/api/bootstrap?token=do-not-log")).status_code == 400
            assert (await client.options("/api/run", headers=headers)).status_code == 405

    asyncio.run(check())


@pytest.mark.parametrize("source", ["codex", "openai_compatible", "openrouter", "litellm"])
def test_models_uses_active_provider_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    monkeypatch.setenv("TEST_CATALOG_KEY", "fake-web-api-key")

    async def check() -> None:
        config = HarnessConfig(
            workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key", api_key_env="TEST_CATALOG_KEY"
        )
        with patch.object(
            HarnessProvider, "get_model_list", AsyncMock(side_effect=AssertionError("No startup discovery"))
        ) as startup:
            async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
                harness = harnesses[0]
                before = (await client.get("/api/settings", headers=headers)).json()
                startup.assert_not_called()
                original = harness.agent.provider
                callback = AsyncMock(side_effect=AssertionError("No live credentials in web dispatch test"))
                provider = (
                    CodexProvider(callback, model=original.model)
                    if source == "codex"
                    else Provider(ProviderType(source), "fake-key", original.model, base_url="http://127.0.0.1:1")
                )
                harness.agent.provider = provider
                try:
                    with (
                        patch.object(
                            provider, "get_model_list", AsyncMock(return_value=["fixture-a", "fixture-b"])
                        ) as get,
                        patch.object(harness.openai_auth, "status", return_value="Fixture login status"),
                    ):
                        before = (await client.get("/api/settings", headers=headers)).json()
                        response = await client.get("/api/models", headers=headers)
                        assert response.status_code == 200
                        assert response.json() == {"models": ["fixture-a", "fixture-b"], "source": source}
                        assert response.headers["cache-control"] == "no-store"
                        get.assert_awaited_once_with()
                        assert (await client.get("/api/settings", headers=headers)).json() == before
                        assert provider.model == original.model and harness.config.provider == "openai"
                        assert harness.agent.provider is provider
                        callback.assert_not_called()
                finally:
                    harness.agent.provider = original
                    await provider.close()

    asyncio.run(check())


def test_models_guards_and_demo_never_dispatch(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            with patch.object(harnesses[0].agent.provider, "get_model_list", AsyncMock()) as get:
                for bad_headers in (
                    {},
                    {"Origin": URL},
                    {**headers, "X-Ngn-Token": "wrong"},
                    {**headers, "Host": "evil.test"},
                    {**headers, "Origin": "https://evil.test"},
                    {**headers, "Sec-Fetch-Site": "cross-site"},
                ):
                    assert (await client.get("/api/models", headers=bad_headers)).status_code == 403
                assert (await client.get("/api/models?base_url=secret", headers=headers)).status_code == 400
                response = await client.get("/api/models", headers=headers)
                assert response.status_code == 501 and "demo" in response.json()["detail"]
                get.assert_not_called()

    asyncio.run(check())


def test_models_captures_provider_during_inflight_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CATALOG_KEY", "fake-web-api-key")

    async def check() -> None:
        config = HarnessConfig(
            workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key", api_key_env="TEST_CATALOG_KEY"
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            provider = harness.agent.provider
            started, release = asyncio.Event(), asyncio.Event()

            async def catalog() -> list[str]:
                started.set()
                await release.wait()
                return ["captured-provider-model"]

            replacement = CodexProvider(AsyncMock(side_effect=AssertionError("No OAuth credentials requested")))
            with (
                patch.object(provider, "get_model_list", catalog),
                patch.object(replacement, "get_model_list", AsyncMock()) as other,
            ):
                task = asyncio.create_task(client.get("/api/models", headers=headers))
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    harness.agent.provider = replacement
                    release.set()
                    response = await task
                    assert response.json() == {"models": ["captured-provider-model"], "source": "openai_compatible"}
                    assert harness.agent.provider is replacement
                    other.assert_not_called()
                finally:
                    release.set()
                    await task
                    harness.agent.provider = provider
                    await replacement.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "failure,status",
    [
        (NotImplementedError("SECRET"), 501),
        (ModelListError("SECRET"), 502),
        (ValueError("SECRET-key"), 502),
        (RuntimeError("SECRET-upstream-body"), 502),
    ],
)
def test_model_failure_safe_and_manual_settings_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, status: int
) -> None:
    monkeypatch.setenv("TEST_CATALOG_KEY", "fake-web-api-key")

    async def check() -> None:
        config = HarnessConfig(
            workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key", api_key_env="TEST_CATALOG_KEY"
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            provider = harnesses[0].agent.provider
            with patch.object(provider, "get_model_list", AsyncMock(side_effect=failure)) as get:
                response = await client.get("/api/models", headers=headers)
                assert response.status_code == status and isinstance(response.json()["detail"], str)
                assert "SECRET" not in response.text
                assert (await client.get("/api/settings", headers=headers)).json() == before
                assert provider.model == before["values"]["model"]
                saved = await client.post(
                    "/api/settings",
                    json={"revision": before["revision"], "values": {**before["values"], "model": "manual-model"}},
                    headers=headers,
                )
                assert saved.status_code == 200 and saved.json()["values"]["model"] == "manual-model"
                assert provider.model == "manual-model"
                get.assert_awaited_once_with()

    asyncio.run(check())


def test_catalog_read_does_not_take_over_pending_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CATALOG_KEY", "fake-web-api-key")

    async def check() -> None:
        config = HarnessConfig(
            workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key", api_key_env="TEST_CATALOG_KEY"
        )
        async with client_app(tmp_path, config=config) as (app, client, headers, harnesses):
            harness = harnesses[0]
            assert isinstance(harness, ControlledHarness)
            stream = LiveStream(app, headers, harness.session_id, "approval")
            try:
                pending = await stream.event("approval")
                with patch.object(harness.agent.provider, "get_model_list", AsyncMock(return_value=[])):
                    response = await client.get("/api/models", headers=headers)
                    assert response.status_code == 200 and response.json()["models"] == []
                assert not stream.task.done() and not harness.decisions
                decision = await client.post(
                    "/api/approval",
                    json={
                        "run_id": pending["run_id"],
                        "approval_id": pending["approval_id"],
                        "call_id": pending["id"],
                        "decision": "deny",
                    },
                    headers=headers,
                )
                assert decision.status_code == 200
                await stream.event("approval_closed")
            finally:
                await stream.disconnect()

    asyncio.run(check())


@pytest.mark.requires_posix
def test_demo_run_uses_workspace_tools(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, controlled=False) as (_, client, headers, _):
            snapshot = (await client.get("/api/sessions", headers=headers)).json()
            first = snapshot["session_id"]
            response = await client.post("/api/run", json={"session_id": first, "prompt": "hello"}, headers=headers)
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[0]["event"] == "run_started"
            assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "completed"
            assert all(event["schema_version"] == 1 for event in events)
            assert any(event["event"] == "tool_call" for event in events)
            assert any(event["event"] == "text_chunk" for event in events)
            assert any("OFFLINE DEMO" in event.get("text", "") for event in events)

    asyncio.run(check())


def test_session_history_and_workspace_membership(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            first = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            await harnesses[0].agent.session.add_message(first, SessionMessage(role="user", content="hello"))
            second = (await client.post("/api/sessions/new", json={}, headers=headers)).json()["session_id"]
            assert first != second
            response = await client.post("/api/sessions/resume", json={"session_id": first}, headers=headers)
            assert response.json()["history"][0]["content"] == "hello"
            # A raw DB session, such as a child conversation, is not picker membership.
            await harnesses[0].agent.session.get_or_create_session("ngn-private-child", "harness")
            for session in ["ngn-private-child", "ngn-unknown", "../../other/sessions.db"]:
                response = await client.post("/api/sessions/resume", json={"session_id": session}, headers=headers)
                assert response.status_code in {404, 422}
            assert (
                await client.post("/api/run", json={"session_id": second, "prompt": "no"}, headers=headers)
            ).status_code == 409
            assert (
                await client.post("/api/run", json={"session_id": first, "prompt": " "}, headers=headers)
            ).status_code == 422
            assert (
                await client.post("/api/run", json={"session_id": first, "prompt": "a" * 32001}, headers=headers)
            ).status_code == 422

    asyncio.run(check())


def test_stream_exact_approvals_and_conflicts(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, controlled=True) as (app, client, headers, harnesses):
            harness = harnesses[0]
            assert isinstance(harness, ControlledHarness)
            stream = LiveStream(app, headers, harness.session_id, "approval")
            first = await stream.event("approval")
            for path, mutation in [
                ("/api/run", {"session_id": harness.session_id, "prompt": "no"}),
                ("/api/sessions/new", {}),
                ("/api/sessions/resume", {"session_id": harness.session_id}),
            ]:
                assert (await client.post(path, json=mutation, headers=headers)).status_code == 409
            assert (await client.get("/api/sessions", headers=headers)).status_code == 409
            assert (await client.get("/api/bootstrap")).json()["active_run_id"] == first["run_id"]
            body = {
                "run_id": first["run_id"],
                "approval_id": first["approval_id"],
                "call_id": first["id"],
                "decision": "deny",
            }
            for key in ["run_id", "approval_id", "call_id"]:
                assert (
                    await client.post("/api/approval", json={**body, key: "wrong"}, headers=headers)
                ).status_code == 409
            assert (
                await client.post("/api/approval", json={**body, "decision": True}, headers=headers)
            ).status_code == 422
            assert (await client.post("/api/approval", json=body, headers=headers)).status_code == 200
            assert (await client.post("/api/approval", json=body, headers=headers)).status_code == 409
            closed = await stream.event("approval_closed")
            assert closed["approval_id"] == first["approval_id"] and closed["decision"] == "deny"
            assert closed["expired"] is False
            second = await stream.event("approval")
            assert second["approval_id"] != first["approval_id"]  # Even if a provider reuses its call ID.
            assert (
                await client.post(
                    "/api/approval",
                    json={**body, "approval_id": second["approval_id"], "decision": "allow"},
                    headers=headers,
                )
            ).status_code == 200
            assert (await stream.event("approval_closed"))["decision"] == "allow"
            assert (await stream.event("run_finished"))["status"] == "completed"
            await asyncio.wait_for(stream.task, 5)
            assert harness.decisions == [False, True]
            assert (await client.post("/api/approval", json=body, headers=headers)).status_code == 409
            assert (await client.get("/api/sessions", headers=headers)).status_code == 200
        assert harness.closed

    asyncio.run(check())


@pytest.mark.parametrize("action", ["cancel", "disconnect", "shutdown"])
@pytest.mark.parametrize("prompt", ["approval", "wait"])
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_cancellation_joins_run_and_denies_pending(tmp_path: Path, action: str, prompt: str, spec: str) -> None:
    async def check() -> None:
        stream: LiveStream
        async with client_app(tmp_path, controlled=True) as (app, client, headers, harnesses):
            harness = harnesses[0]
            assert isinstance(harness, ControlledHarness)
            stream = LiveStream(app, headers, harness.session_id, prompt, spec)
            started = await stream.event("run_started")
            await stream.event("approval" if prompt == "approval" else "text_chunk")
            if action == "cancel":
                body = {"run_id": started["run_id"]}
                assert (await client.post("/api/cancel", json={"run_id": "wrong"}, headers=headers)).status_code == 409
                assert (await client.post("/api/cancel", json=body, headers=headers)).status_code == 200
                assert (await stream.event("run_finished"))["status"] == "cancelled"
                await asyncio.wait_for(stream.task, 5)
                assert (await client.post("/api/cancel", json=body, headers=headers)).status_code == 409
            elif action == "disconnect":
                await stream.disconnect()
            if action != "shutdown":
                assert harness.stopped.is_set()
                assert (await client.post("/api/sessions/new", json={}, headers=headers)).status_code == 200
        assert harness.stopped.is_set() and harness.closed
        assert True not in harness.decisions
        if action != "disconnect":
            await asyncio.wait_for(stream.task, 5)

    asyncio.run(check())


def test_approval_expiry_fails_closed(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, controlled=True) as (app, _, headers, harnesses):
            harness = harnesses[0]
            assert isinstance(harness, ControlledHarness)
            with patch("nagents.web.app.APPROVAL_TIMEOUT", 0.01):
                stream = LiveStream(app, headers, harness.session_id, "approval")
                assert "expired" in str((await stream.event("notice"))["text"])
                closed = await stream.event("approval_closed")
                assert closed["decision"] == "deny" and closed["expired"] is True
                assert (await stream.event("run_finished"))["status"] == "completed"
                await stream.task
            assert harness.decisions == [False, False]

    asyncio.run(check())


def test_safe_errors_and_lifespan_initialization_failure(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, controlled=True) as (_, client, headers, harnesses):
            response = await client.post(
                "/api/run", json={"session_id": harnesses[0].session_id, "prompt": "error"}, headers=headers
            )
            assert "SECRET" not in response.text
            assert '"status": "failed"' in response.text
        harness = ControlledHarness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True))
        app = create_app(
            harness.config, assets=tmp_path / "static", resume_session="ngn-unknown", harness_factory=lambda _: harness
        )
        with pytest.raises(ValueError, match="workspace"):
            async with app.router.lifespan_context(app):
                pytest.fail("Invalid resume must fail startup")
        assert harness.closed

    asyncio.run(check())
