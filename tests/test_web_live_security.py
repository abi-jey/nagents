"""Browser audio relay shares the web server's origin and token boundary."""

import pytest
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nagents.web.security import LocalOnly


@pytest.mark.parametrize("protocol", ["ngn.live.v1", "ngn.live.v2"])
@pytest.mark.parametrize("change", ["token", "origin", "host", "query", "protocol", "duplicate", "path"])
def test_live_audio_socket_rejects_invalid_credentials_or_route(change: str, protocol: str) -> None:
    app = FastAPI()
    app.add_middleware(LocalOnly, authority="testserver", token="secret", enforce_authority=True)

    @app.websocket("/api/live/sessions/{session_id}/audio")
    async def audio(socket: WebSocket, session_id: str) -> None:
        await socket.accept(subprotocol=protocol)
        await socket.send_text(session_id)

    headers = {"Origin": "http://testserver"}
    path = "/api/live/sessions/owned/audio"
    protocols = [protocol, "ngn.token.secret"]
    if change == "token":
        protocols[1] = "ngn.token.wrong"
    elif change == "origin":
        headers["Origin"] = "http://evil.example"
    elif change == "host":
        headers["Host"] = "another-host"
    elif change == "query":
        path += "?token=secret"
    elif change == "protocol":
        protocols[0] = "ngn.events.v1"
    elif change == "duplicate":
        protocols.append("ngn.token.secret")
    else:
        path = "/api/live/sessions/owned/other"
    with TestClient(app, base_url="http://testserver") as client:
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(path, headers=headers, subprotocols=protocols),
        ):
            pytest.fail("Unauthenticated audio accepted")
        assert rejected.value.code == 1008


@pytest.mark.parametrize("protocol", ["ngn.live.v1", "ngn.live.v2"])
def test_live_audio_socket_accepts_only_same_origin_token_subprotocol(protocol: str) -> None:
    app = FastAPI()
    app.add_middleware(LocalOnly, authority="testserver", token="secret", enforce_authority=True)

    @app.websocket("/api/live/sessions/{session_id}/audio")
    async def audio(socket: WebSocket, session_id: str) -> None:
        await socket.accept(subprotocol=protocol)
        await socket.send_text(session_id)

    with (
        TestClient(app, base_url="http://testserver") as client,
        client.websocket_connect(
            "/api/live/sessions/owned/audio",
            headers={"Origin": "http://testserver"},
            subprotocols=[protocol, "ngn.token.secret"],
        ) as socket,
    ):
        assert socket.accepted_subprotocol == protocol
        assert socket.receive_text() == "owned"
