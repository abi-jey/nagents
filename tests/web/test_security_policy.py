"""Browser resources and application traffic stay on the ngn origin."""

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from nagents.web.security import LocalOnly


@pytest.mark.parametrize("path", ["/", "/missing", "/api/private"])
def test_security_policy_covers_success_and_rejected_responses(path: str) -> None:
    app = FastAPI()
    app.add_middleware(LocalOnly, authority="testserver", token="secret")

    @app.get("/")
    async def index() -> HTMLResponse:
        # Downstream routes cannot loosen the browser's origin boundary.
        return HTMLResponse(
            "<p>ngn</p>",
            headers={"Content-Security-Policy": "default-src *", "X-DNS-Prefetch-Control": "on"},
        )

    with TestClient(app, base_url="http://testserver") as client:
        response = client.get(path)
    assert response.status_code == {"/": 200, "/missing": 404, "/api/private": 403}[path]
    assert response.headers.get_list("x-dns-prefetch-control") == ["off"]
    policies = response.headers.get_list("content-security-policy")
    assert len(policies) == 1
    directives = {parts[0]: parts[1:] for directive in policies[0].split(";") if (parts := directive.split())}
    assert directives["default-src"] == ["'none'"]
    for resource in ("connect-src", "script-src", "style-src", "font-src", "worker-src", "form-action"):
        assert directives[resource] == ["'self'"]
    for resource in ("img-src", "media-src"):
        assert directives[resource] == ["'self'", "blob:", "data:"]
    assert directives["base-uri"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
