"""The document entry point permits browser navigation without opening API access."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nagents.web.security import LocalOnly


def navigation_app(*, remote_bind: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        LocalOnly,
        authority="0.0.0.0:8765" if remote_bind else "testserver",
        token="test-token",
        enforce_authority=not remote_bind,
    )

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse("<html><title>ngn</title></html>")

    @app.get("/assets/app.js")
    async def asset() -> HTMLResponse:
        return HTMLResponse("window.ngn = true", media_type="text/javascript")

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/private")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    ("remote_bind", "base_url"),
    [(False, "http://testserver"), (True, "https://ngn.example.test")],
)
@pytest.mark.parametrize("site", ["same-site", "cross-site"])
@pytest.mark.parametrize("origin", ["", "null"])
def test_top_level_document_navigation_loads_at_loopback_and_remote_bindings(
    remote_bind: bool, base_url: str, site: str, origin: str
) -> None:
    headers = {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    if origin:
        headers["Origin"] = origin

    with TestClient(navigation_app(remote_bind=remote_bind), base_url=base_url) as client:
        response = client.get("/", headers=headers)

    assert response.status_code == 200
    assert "<title>ngn</title>" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"].find("frame-ancestors 'none'") >= 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "document"},
        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "document"},
        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"},
        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
    ],
)
def test_only_a_complete_top_level_navigation_exempts_cross_site_fetch_metadata(headers: dict[str, str]) -> None:
    with TestClient(navigation_app(), base_url="http://testserver") as client:
        response = client.get("/", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Same-origin requests only."}


def test_navigation_exemption_does_not_extend_to_assets_queries_apis_or_mutations() -> None:
    navigation = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Origin": "null",
    }
    with TestClient(navigation_app(), base_url="http://testserver") as client:
        assert client.get("/assets/app.js", headers=navigation).status_code == 403
        assert client.get("/api/bootstrap", headers=navigation).status_code == 403
        assert client.get("/api/private", headers={**navigation, "X-Ngn-Token": "test-token"}).status_code == 403
        assert client.get("/?token=leak", headers=navigation).status_code == 400
        assert client.post("/", headers=navigation).status_code == 403
        assert client.post("/api/private", headers={**navigation, "X-Ngn-Token": "test-token"}).status_code == 403


def test_loopback_host_validation_still_applies_to_top_level_navigation() -> None:
    with TestClient(navigation_app(), base_url="http://testserver") as client:
        response = client.get(
            "/",
            headers={
                "Host": "attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "Untrusted Host. Open the exact loopback URL printed by ngn serve."}


def test_same_origin_bootstrap_and_cross_site_websocket_policy_are_unchanged() -> None:
    app = navigation_app()

    @app.websocket("/api/events")
    async def events(socket: WebSocket) -> None:
        await socket.accept(subprotocol="ngn.events.v1")

    with TestClient(app, base_url="http://testserver") as client:
        bootstrap = client.get(
            "/api/bootstrap", headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
        )
        assert bootstrap.status_code == 200
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                "/api/events",
                headers={"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
                subprotocols=["ngn.events.v1", "ngn.token.test-token"],
            ),
        ):
            pytest.fail("Cross-site websocket unexpectedly connected")
        assert rejected.value.code == 1008
