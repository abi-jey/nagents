"""Isolated legacy-server regressions: no network, Docker, dotenv or user data."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING
from typing import NoReturn

import pytest

import nagents
from nagents import DoneEvent
from nagents import SessionManager
from nagents import TextChunkEvent
from nagents.events import Usage
from nagents.types import Message
from nagents.types import ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.types import Message as ASGIMessage
    from starlette.types import Scope

pytest.importorskip("fastapi")

TOKEN = "synthetic-server-token-not-a-real-secret"
AUTH = ("authorization", f"Bearer {TOKEN}")


def forbidden_external_call(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Server tests must not create real agents, providers, MCP or Docker processes")


@pytest.fixture
def server(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    # Import only after isolating every import-time path and replacing dotenv.
    dotenv = ModuleType("dotenv")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False, raising=False)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    for name, value in {
        "NAGENTS_SESSIONS_DB": str(tmp_path / "sessions.db"),
        "NAGENTS_CONFIGS_PATH": str(tmp_path / "configs.json"),
        "NAGENTS_TOOLS_DIR": str(tmp_path / "tools"),
        "NAGENTS_MCP_CONFIG": str(tmp_path / "mcp.json"),
        "NAGENTS_MCP_ENABLED": "false",
        "NAGENTS_MCP_SERVERS": "",
        "NAGENTS_LLM_API_KEY": "synthetic-provider-key",
        "NAGENTS_LLM_PROVIDER": "openai",
        "NAGENTS_LLM_MODEL": "synthetic-model",
        "NAGENTS_LLM_BASE_URL": "http://invalid.invalid",
        "NAGENTS_SYSTEM_PROMPT": "Synthetic system prompt",
    }.items():
        monkeypatch.setenv(name, value)
    token = getattr(request, "param", TOKEN)
    if token:
        monkeypatch.setenv("NAGENTS_SERVER_TOKEN", token)
    else:
        monkeypatch.delenv("NAGENTS_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("NAGENTS_SERVER_HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(nagents, "Agent", forbidden_external_call)
    monkeypatch.setattr(nagents, "Provider", forbidden_external_call)

    # The legacy package initializes global state and logging on import. Restore
    # preexisting modules/handlers rather than leaking that state into other tests.
    for name in list(sys.modules):
        if name == "nagents.server" or name.startswith("nagents.server."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.delattr(nagents, "server", raising=False)
    for name in ("nagents", "nagents.server"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", list(logger.handlers))
        monkeypatch.setattr(logger, "level", logger.level)
        monkeypatch.setattr(logger, "propagate", logger.propagate)
    try:
        module = importlib.import_module("nagents.server.app")
        monkeypatch.setattr(module, "MCPManager", forbidden_external_call)
        tools = importlib.import_module("nagents.server.tools")
        monkeypatch.setattr(tools, "docker_run", forbidden_external_call)
        scheduler = importlib.import_module("nagents.server.scheduler")
        monkeypatch.setattr(scheduler, "WAKEUPS_PATH", tmp_path / "wakeups.json")
        yield module
    finally:
        for name in list(sys.modules):
            if name == "nagents.server" or name.startswith("nagents.server."):
                sys.modules.pop(name)
        if hasattr(nagents, "server"):
            delattr(nagents, "server")


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes


def request_app(
    server: ModuleType,
    path: str,
    *,
    method: str = "GET",
    headers: tuple[tuple[str, str], ...] = (AUTH,),
    body: bytes = b"",
    client: str = "127.0.0.1",
    host: str = "localhost",
    disconnect_after_body: bool = False,
) -> HTTPResult:
    """Exercise the real ASGI stack, including streaming disconnect cleanup."""

    async def run() -> HTTPResult:
        messages: list[ASGIMessage] = []
        disconnected = asyncio.Event()
        request_sent = False

        async def receive() -> ASGIMessage:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: ASGIMessage) -> None:
            messages.append(message)
            if disconnect_after_body and message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        route, _, query = path.partition("?")
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": route,
            "raw_path": route.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(key.encode(), value.encode()) for key, value in (("host", host), *headers)]
            + [(b"content-type", b"application/json")],
            "client": (client, 50000),
            "server": ("127.0.0.1", 8080),
        }
        await asyncio.wait_for(server.app(scope, receive, send), timeout=3)
        start = messages[0]
        assert start["type"] == "http.response.start"
        return HTTPResult(
            status=start["status"],
            headers={key.decode(): value.decode() for key, value in start["headers"]},
            body=b"".join(message.get("body", b"") for message in messages[1:]),
        )

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/ui/index.html"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/logs"),
        ("GET", "/events"),
        ("GET", "/sessions"),
        ("GET", "/sessions/example"),
        ("GET", "/sessions/example/history"),
        ("DELETE", "/sessions/example"),
        ("GET", "/tools"),
        ("POST", "/tools/reload"),
        ("POST", "/chat"),
        ("POST", "/chat/stream"),
        ("GET", "/configs"),
        ("GET", "/configs/active"),
        ("POST", "/configs"),
        ("PUT", "/configs/default"),
        ("DELETE", "/configs/default"),
        ("POST", "/configs/default/activate"),
        ("GET", "/mcp/status"),
        ("GET", "/attachments"),
        ("GET", "/attachments/example"),
        ("POST", "/health"),
        ("GET", "/unknown"),
    ],
)
def test_auth_covers_routes_before_validation_or_side_effects(server: ModuleType, method: str, path: str) -> None:
    result = request_app(server, path, method=method, headers=(), body=b"not even JSON")
    assert result.status == 401
    assert json.loads(result.body) == {"detail": "Authentication required"}
    assert result.headers["www-authenticate"].startswith("Basic ")
    assert result.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in result.headers
    assert TOKEN.encode() not in result.body
    assert not server.SESSIONS_DB.exists()
    assert not server.CONFIGS_PATH.exists()
    assert server._event_subscribers == []


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer wrong", "Bearer", "Bearer  " + TOKEN, "Basic !!!", "Basic /w==", "Digest " + TOKEN],
)
def test_invalid_credentials_fail_closed(server: ModuleType, authorization: str) -> None:
    result = request_app(server, "/configs", headers=(("authorization", authorization),))
    assert result.status == 401
    assert TOKEN.encode() not in result.body


def test_duplicate_credentials_and_url_credentials_are_not_accepted(server: ModuleType) -> None:
    assert request_app(server, "/configs", headers=(AUTH, AUTH)).status == 401
    assert request_app(server, f"/configs?token={TOKEN}&access_token={TOKEN}", headers=()).status == 401
    assert request_app(server, "/configs", headers=(("cookie", f"token={TOKEN}"),)).status == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "Basic"])
def test_authenticated_remote_access_and_browser_auth(server: ModuleType, scheme: str) -> None:
    value = base64.b64encode(f"nagents:{TOKEN}".encode()).decode() if scheme == "Basic" else TOKEN
    result = request_app(
        server,
        "/configs",
        headers=(("authorization", f"{scheme} {value}"), ("origin", "http://agent.example")),
        client="192.0.2.1",
        host="agent.example",
    )
    assert result.status == 200
    assert json.loads(result.body)[0]["id"] == "default"
    assert TOKEN.encode() not in result.body
    assert result.headers["cache-control"] == "no-store"


def test_basic_username_is_not_ignored(server: ModuleType) -> None:
    value = base64.b64encode(f"wrong:{TOKEN}".encode()).decode()
    assert request_app(server, "/configs", headers=(("authorization", f"Basic {value}"),)).status == 401


@pytest.mark.parametrize("server", [TOKEN, ""], indirect=True)
@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:9999", "https://localhost"])
def test_cross_origin_mutations_rejected_even_with_credentials(server: ModuleType, origin: str) -> None:
    result = request_app(server, "/configs", method="POST", headers=(AUTH, ("origin", origin)))
    assert result.status == 403
    assert not server.CONFIGS_PATH.exists()
    assert "access-control-allow-origin" not in result.headers


def test_cross_origin_preflight_and_fetch_metadata_are_rejected(server: ModuleType) -> None:
    result = request_app(
        server,
        "/chat",
        method="OPTIONS",
        headers=(("origin", "http://evil.example"), ("access-control-request-method", "POST")),
    )
    assert result.status == 403
    assert "access-control-allow-origin" not in result.headers
    assert request_app(server, "/configs", headers=(AUTH, ("sec-fetch-site", "cross-site"))).status == 403


@pytest.mark.parametrize("server", [""], indirect=True)
@pytest.mark.parametrize(
    ("client", "host"), [("127.0.0.1", "localhost"), ("127.0.0.2", "127.0.0.1:8080"), ("::1", "[::1]:8080")]
)
def test_unconfigured_auth_preserves_loopback_usage(server: ModuleType, client: str, host: str) -> None:
    assert request_app(server, "/configs", headers=(), client=client, host=host).status == 200


@pytest.mark.parametrize("server", [""], indirect=True)
@pytest.mark.parametrize(
    ("client", "host"),
    [("192.0.2.1", "localhost"), ("127.0.0.1", "evil.example"), ("192.0.2.1", "192.0.2.1"), ("", "localhost")],
)
def test_local_mode_rejects_remote_peers_and_rebinding(server: ModuleType, client: str, host: str) -> None:
    result = request_app(
        server,
        "/configs",
        client=client,
        host=host,
        headers=(("x-forwarded-for", "127.0.0.1"), ("x-forwarded-host", "localhost")),
    )
    assert result.status == 403


@pytest.mark.parametrize("host", ["", "[invalid", "localhost:bad", "user@localhost", "localhost/path"])
def test_invalid_host_is_rejected(server: ModuleType, host: str) -> None:
    assert request_app(server, "/configs", host=host).status == 400


def test_ambiguous_host_or_origin_is_rejected(server: ModuleType) -> None:
    assert request_app(server, "/configs", headers=(AUTH, ("host", "localhost"))).status == 400
    assert (
        request_app(
            server, "/configs", headers=(AUTH, ("origin", "http://localhost"), ("origin", "http://localhost"))
        ).status
        == 403
    )


@pytest.mark.parametrize("server", [TOKEN, ""], indirect=True)
def test_health_probe_remains_public(server: ModuleType) -> None:
    result = request_app(server, "/health", headers=(), client="192.0.2.1", host="agent.example")
    assert result.status == 200
    assert json.loads(result.body) == {"status": "ok"}


@pytest.mark.parametrize("token", ["", " ", " leading", "trailing ", "line\nbreak", "not:bearer", "\N{SNOWMAN}"])
def test_configured_invalid_token_cannot_disable_auth(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    security = importlib.import_module("nagents.server.security")
    monkeypatch.setenv("NAGENTS_SERVER_TOKEN", token)
    with pytest.raises(ValueError, match="NAGENTS_SERVER_TOKEN must be") as exc:
        security.load_server_token()
    assert str(exc.value) == "NAGENTS_SERVER_TOKEN must be a nonempty RFC 6750 bearer token"
    # Auth is a startup snapshot, not mutable through per-request environment reads.
    assert request_app(server, "/configs", headers=()).status == 401
    assert request_app(server, "/configs").status == 200


@pytest.mark.parametrize("server", [TOKEN, ""], indirect=True)
def test_entrypoint_defaults_to_loopback_and_disables_forwarded_headers(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("uvicorn")
    entrypoint = importlib.import_module("nagents.server.__main__")
    calls: list[dict[str, object]] = []

    def run(app: object, **kwargs: object) -> None:
        assert app is server.app
        calls.append(kwargs)

    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    entrypoint.main()
    assert calls == [{"host": "127.0.0.1", "port": 8080, "proxy_headers": False}]
    monkeypatch.setenv("NAGENTS_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9090")
    if server.SERVER_TOKEN:
        entrypoint.main()
        assert calls[-1] == {"host": "0.0.0.0", "port": 9090, "proxy_headers": False}
    else:
        with pytest.raises(ValueError, match="NAGENTS_SERVER_TOKEN is required"):
            entrypoint.main()
        assert len(calls) == 1


def test_chat_session_round_trip_and_streaming(server: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(server.SESSIONS_DB)
    calls: list[tuple[str, str, str]] = []
    scheduler = importlib.import_module("nagents.server.scheduler")

    class FakeAgent:
        async def run(
            self, user_message: str, session_id: str, user_id: str
        ) -> AsyncIterator[TextChunkEvent | DoneEvent]:
            assert scheduler._current_session_id == session_id
            assert scheduler._current_user_id == user_id
            calls.append((user_message, session_id, user_id))
            await manager.get_or_create_session(session_id, user_id)
            await manager.add_message(session_id, Message(role="user", content=user_message))
            await manager.add_message(session_id, Message(role="assistant", content="Hello world"))
            yield TextChunkEvent(chunk="Hello ")
            yield TextChunkEvent(chunk="world")
            yield DoneEvent(session_id=session_id, usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5))

    monkeypatch.setattr(server, "_agent", FakeAgent())
    result = request_app(server, "/chat", method="POST", body=b'{"message":"hello","user_id":"tester"}')
    assert result.status == 200
    chat = json.loads(result.body)
    session_id = chat["session_id"]
    assert session_id.startswith("session-")
    assert chat == {"response": "Hello world", "session_id": session_id, "attachments": []}
    assert calls == [("hello", session_id, "tester")]

    sessions = json.loads(request_app(server, "/sessions").body)
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["user_id"] == "tester"
    assert sessions[0]["message_count"] == 2
    assert json.loads(request_app(server, f"/sessions/{session_id}").body) == sessions[0]
    history = json.loads(request_app(server, f"/sessions/{session_id}/history").body)
    assert [(message["role"], message["content"]) for message in history] == [
        ("user", "hello"),
        ("assistant", "Hello world"),
    ]

    result = request_app(
        server,
        "/chat/stream",
        method="POST",
        body=json.dumps({"message": "again", "session_id": session_id, "user_id": "tester"}).encode(),
    )
    assert result.status == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line.removeprefix(b"data: ")) for line in result.body.splitlines() if line]
    assert [event["type"] for event in events] == ["text", "text", "done"]
    assert events[-1]["session_id"] == session_id
    assert events[-1]["tokens"] == 5
    assert TOKEN.encode() not in result.body
    assert calls[-1] == ("again", session_id, "tester")

    assert request_app(server, f"/sessions/{session_id}", method="DELETE").status == 200
    assert json.loads(request_app(server, "/sessions").body) == []
    assert request_app(server, f"/sessions/{session_id}").status == 404
    assert json.loads(request_app(server, f"/sessions/{session_id}/history").body) == []


def test_tool_calls_in_session_history(server: ModuleType) -> None:
    async def seed() -> None:
        manager = SessionManager(server.SESSIONS_DB)
        await manager.get_or_create_session("synthetic-session", "tester")
        await manager.add_message(
            "synthetic-session",
            Message(
                role="assistant", tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"path": "example"})]
            ),
        )
        await manager.add_message(
            "synthetic-session", Message(role="tool", content="example text", tool_call_id="call-1", name="read_file")
        )

    asyncio.run(seed())
    result = request_app(server, "/sessions/synthetic-session/history")
    assert result.status == 200
    history = json.loads(result.body)
    assert history[0]["tool_calls"][0]["arguments"] == {"path": "example"}
    assert history[1]["tool_call_id"] == "call-1"
    assert history[1]["name"] == "read_file"


def test_wakeup_sse_auth_and_disconnect_cleanup(server: ModuleType) -> None:
    assert request_app(server, "/events", headers=()).status == 401
    assert server._event_subscribers == []
    basic = base64.b64encode(f"nagents:{TOKEN}".encode()).decode()
    result = request_app(server, "/events", headers=(("authorization", f"Basic {basic}"),), disconnect_after_body=True)
    assert result.status == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert result.body == b'data: {"type": "connected"}\n\n'
    assert TOKEN.encode() not in result.body
    assert server._event_subscribers == []


def test_startup_shutdown_scheduler_lifecycle(server: ModuleType) -> None:
    scheduler = importlib.import_module("nagents.server.scheduler")

    async def lifecycle() -> None:
        await server._startup()
        task = scheduler._check_task
        assert task is not None
        assert scheduler._on_wakeup is not None
        await asyncio.sleep(0)
        await server._shutdown()
        await asyncio.wait_for(task, timeout=1)
        assert scheduler._check_task is None

    asyncio.run(lifecycle())
    assert not scheduler.WAKEUPS_PATH.exists()


@pytest.mark.parametrize("server", [TOKEN, ""], indirect=True)
def test_config_crud_hides_provider_key_and_protects_embedded_config(server: ModuleType) -> None:
    headers = (AUTH,) if server.SERVER_TOKEN else ()
    result = request_app(
        server,
        "/configs",
        method="POST",
        headers=(*headers, ("origin", "http://localhost"), ("sec-fetch-site", "same-origin")),
        body=b'{"name":"Synthetic","api_key":"synthetic-key"}',
    )
    assert result.status == 200
    created = json.loads(result.body)
    cfg_id = created["id"]
    assert "api_key" not in created
    assert b"synthetic-key" not in result.body
    assert "synthetic-key" in server.CONFIGS_PATH.read_text()
    assert b"synthetic-key" not in request_app(server, "/configs").body
    assert request_app(server, f"/configs/{cfg_id}", method="PUT", body=b'{"name":"Updated"}').status == 200
    assert server._all_configs[cfg_id]["api_key"] == "synthetic-key"
    assert request_app(server, "/configs/default", method="PUT", body=b'{"name":"No"}').status == 403
    assert request_app(server, "/configs/default", method="DELETE").status == 403
    assert request_app(server, f"/configs/{cfg_id}", method="DELETE").status == 200
    assert request_app(server, f"/configs/{cfg_id}", method="PUT", body=b'{"name":"Missing"}').status == 404
    assert request_app(server, "/configs/missing/activate", method="POST").status == 404


def test_attachment_download_is_authenticated_and_not_executable(server: ModuleType, tmp_path: Path) -> None:
    tools = importlib.import_module("nagents.server.tools")
    path = tmp_path / "generated.html"
    path.write_text("<script>document.body.textContent = 'untrusted'</script>")
    tools.attach_file(str(path), "Synthetic attachment")
    attachment = next(iter(server._attachments.values()))
    url = f"/attachments/{attachment.id}"
    assert request_app(server, url, headers=()).status == 401
    assert attachment.fetch_count == 0
    result = request_app(server, url)
    assert result.status == 200
    assert result.body == path.read_bytes()
    assert result.headers["content-disposition"] == 'attachment; filename="generated.html"'
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["cache-control"] == "no-store"
    assert attachment.fetch_count == 1
    listing = json.loads(request_app(server, "/attachments").body)
    assert listing[0]["fetch_count"] == 1
    assert listing[0]["filename"] == "generated.html"
    assert request_app(server, "/attachments/missing").status == 404


def test_read_only_routes_and_chat_validation(server: ModuleType) -> None:
    assert request_app(server, "/").status == 200
    assert request_app(server, "/ui/index.html").status == 200
    assert json.loads(request_app(server, "/mcp/status").body) == []
    assert any(tool["name"] == "read_file" for tool in json.loads(request_app(server, "/tools").body))
    assert request_app(server, "/chat", method="POST", body=b"{}").status == 422
    assert not server.SESSIONS_DB.exists()
