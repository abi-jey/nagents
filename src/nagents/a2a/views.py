"""Small out-of-band browser UI; no local ngn administration is exposed."""

from __future__ import annotations

import html
import json
import re
import secrets
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs
from urllib.parse import urlencode

from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse
from starlette.routing import Route

from .auth import AuthenticationError
from .authorization import AuthorizationUnavailable
from .config import mapping

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from .application import A2AService
    from .auth import Principal

BROWSER_COOKIE = "ngn_a2a_browser"
SESSION_COOKIE = "ngn_a2a_session"
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
STYLE = """
:root{color-scheme:light dark;font-family:system-ui,sans-serif;background:#f7f7f5;color:#282826}
body{margin:0;padding:clamp(20px,6vw,64px)}main{max-width:760px;margin:auto}header{font-size:13px;color:#73736d}
h1{font-size:26px;font-weight:560;letter-spacing:-.7px;margin:28px 0 12px}p{line-height:1.6;color:#66665f}
pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#ededeb;padding:20px;border-radius:12px}
form,nav{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}button,a.action{font:inherit;padding:10px 18px;border:1px solid #d2d2cb;border-radius:9px;background:#fff;color:#333;text-decoration:none;cursor:pointer}
button.primary,a.primary{background:#343830;color:#fff;border-color:#343830}.meta{font-size:12px;margin-top:24px}
@media(prefers-color-scheme:dark){:root{background:#191b19;color:#eee}p,header{color:#aaa}pre{background:#252825}button,a.action{background:#292c29;color:#eee;border-color:#4a4e48}button.primary,a.primary{background:#d1dcc8;color:#1b2416}}
"""


def page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} · Nagents</title><style>{STYLE}</style>"
        f"<main><header>Nagents · Remote agent authorization</header><h1>{html.escape(title)}</h1>{body}</main></html>",
        status_code=status,
        headers={"Content-Security-Policy": CSP},
    )


@dataclass
class BrowserSession:
    token: str = field(repr=False)
    expires_at: float


class AuthorizationViews:
    def __init__(self, service: A2AService) -> None:
        self.service = service
        self.sessions: dict[str, BrowserSession] = {}

    def routes(self) -> list[Route]:
        return [
            Route("/authorize/{identifier}", self.review, methods=["GET"]),
            Route("/authorize/{identifier}/decision", self.decision, methods=["POST"]),
            Route("/authorize/{identifier}/connect", self.connect, methods=["POST"]),
            Route("/oauth/login", self.login, methods=["GET"]),
            Route("/oauth/callback", self.callback, methods=["GET"]),
        ]

    async def user(self, request: Request, *, required: bool = True) -> Principal | None:
        headers = request.headers.getlist("authorization")
        token = ""
        if headers:
            if len(headers) != 1:
                raise AuthenticationError()
            scheme, _, token = headers[0].partition(" ")
            if scheme.lower() != "bearer":
                raise AuthenticationError()
        else:
            identifier = request.cookies.get(SESSION_COOKIE, "")
            session = self.sessions.get(identifier)
            if session is not None and session.expires_at > time.time():
                token = session.token
        if not token:
            if required:
                raise AuthenticationError()
            return None
        # Opaque-token sessions are introspected on every decision too; cookies
        # never freeze an old scope set or bypass a revoked/expired token.
        principal = await self.service.authenticator.authenticate(token)
        principal.require("ngn:access")
        principal.require("ngn:approve")
        if not self.service.owner_limit.admit(principal.user_name):
            raise AuthenticationError("rate_limited", 429)
        return principal

    @staticmethod
    def identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", value):
            raise AuthorizationUnavailable()
        return value

    def cookie(self, response: Response, name: str, value: str, seconds: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=seconds,
            httponly=True,
            secure=not self.service.config.loopback_http,
            samesite="lax",
            path="/",
        )

    async def review(self, request: Request) -> Response:
        identifier = self.identifier(request.path_params["identifier"])
        user = await self.user(request, required=False)
        if user is None:
            if "application/json" in request.headers.get("accept", ""):
                raise AuthenticationError()
            target = "/oauth/login?" + urlencode({"authorization": identifier})
            return page(
                "Review a remote agent request",
                f'<p>Sign in through the configured identity provider to view this request.</p><nav><a class="action primary" href="{html.escape(target)}">Sign in to review</a></nav>',
            )
        record = await self.service.authorizations.inspect(identifier, user)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "id": record.id,
                    "task_id": record.task_id,
                    "kind": record.kind,
                    "operation": record.details,
                    "digest": record.digest,
                    "nonce": record.nonce,
                    "expires_at": record.expires_at,
                }
            )
        data = html.escape(json.dumps(record.details, indent=2, ensure_ascii=False))
        hidden = f'<input type="hidden" name="nonce" value="{record.nonce}"><input type="hidden" name="digest" value="{record.digest}">'
        if record.kind == "oauth":
            controls = f'<form method="post" action="/authorize/{identifier}/connect">{hidden}<button class="primary" type="submit">Connect service</button></form>'
            title = "Authorize service access"
        else:
            controls = f'<form method="post" action="/authorize/{identifier}/decision">{hidden}<button class="primary" type="submit" name="approved" value="true">Allow this operation once</button><button type="submit" name="approved" value="false">Deny</button></form>'
            title = "Review the proposed operation"
        if record.kind == "oauth":
            controls += f'<form method="post" action="/authorize/{identifier}/decision">{hidden}<button type="submit" name="approved" value="false">Deny access</button></form>'
        behalf = (
            "<p>This request belongs to another service principal in your tenant. Your separately granted operator scope permits this decision.</p>"
            if record.owner != user.user_name
            else ""
        )
        return page(
            title,
            f"<p>Approval applies to this task and this exact request. It does not approve future operations.</p>{behalf}<pre>{data}</pre>{controls}<p class=meta>Expires in {max(0, int(record.expires_at - time.time()))} seconds · Task {html.escape(record.task_id)}</p>",
        )

    async def _decision_data(self, request: Request) -> dict[str, object]:
        if not request.headers.get("authorization") and request.headers.get(
            "origin"
        ) != self.service.config.public_url.rstrip("/"):
            raise AuthenticationError("invalid_origin", 403)
        raw = await request.body()
        try:
            if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
                return mapping(json.loads(raw), "decision")
            values = parse_qs(raw.decode(), max_num_fields=4, strict_parsing=True)
            if any(len(value) != 1 for value in values.values()):
                raise ValueError()
            return {key: value[0] for key, value in values.items()}
        except (ValueError, UnicodeError):
            raise AuthorizationUnavailable() from None

    async def decision(self, request: Request) -> Response:
        user = await self.user(request)
        if user is None:
            raise AuthenticationError()
        record = await self.service.authorizations.inspect(self.identifier(request.path_params["identifier"]), user)
        values = await self._decision_data(request)
        approved = values.get("approved")
        if isinstance(approved, str) and approved in {"true", "false"}:
            approved = approved == "true"
        if type(approved) is not bool or (record.kind == "oauth" and approved):
            raise AuthorizationUnavailable()
        nonce, digest = values.get("nonce"), values.get("digest")
        if not isinstance(nonce, str) or not isinstance(digest, str):
            raise AuthorizationUnavailable()
        await self.service.authorizations.decide(record.id, user, nonce, approved, digest)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"decision": "approved" if approved else "denied"})
        return page(
            "Decision recorded", "<p>Return to your client to follow the task. This link cannot be used again.</p>"
        )

    async def connect(self, request: Request) -> Response:
        user = await self.user(request)
        if user is None:
            raise AuthenticationError()
        record = await self.service.authorizations.inspect(self.identifier(request.path_params["identifier"]), user)
        values = await self._decision_data(request)
        if record.kind != "oauth" or values.get("nonce") != record.nonce or values.get("digest") != record.digest:
            raise AuthorizationUnavailable()
        service = record.details.get("service")
        if not isinstance(service, str):
            raise AuthorizationUnavailable()
        browser = request.cookies.get(BROWSER_COOKIE, "") or secrets.token_urlsafe(32)
        url = self.service.oauth.begin(record.id, browser, service=service, user=user)
        response = RedirectResponse(url, status_code=303)
        self.cookie(response, BROWSER_COOKIE, browser, 300)
        return response

    async def login(self, request: Request) -> Response:
        identifier = self.identifier(request.query_params.get("authorization", ""))
        browser = request.cookies.get(BROWSER_COOKIE, "") or secrets.token_urlsafe(32)
        response = RedirectResponse(self.service.oauth.begin(identifier, browser), status_code=303)
        self.cookie(response, BROWSER_COOKIE, browser, 300)
        return response

    async def callback(self, request: Request) -> Response:
        browser = request.cookies.get(BROWSER_COOKIE, "")
        state = request.query_params.get("state", "")
        user = await self.user(request) if self.service.oauth.requires_user(state) else None
        if request.query_params.get("error"):
            await self.service.oauth.decline(state, browser, user)
            return page(
                "Authorization declined",
                "<p>The service did not grant access. Return to your client to follow the task.</p>",
            )
        result = await self.service.oauth.finish(state, request.query_params.get("code", ""), browser, user)
        response: Response = RedirectResponse(f"/authorize/{result.authorization_id}", status_code=303)
        if result.access_token:
            now = time.time()
            self.sessions = {key: session for key, session in self.sessions.items() if session.expires_at > now}
            if len(self.sessions) >= 256:
                raise AuthenticationError("session_limit", 429)
            identifier = secrets.token_urlsafe(32)
            expires = min(result.user.expires_at, now + 900)
            self.sessions[identifier] = BrowserSession(result.access_token, expires)
            self.cookie(response, SESSION_COOKIE, identifier, max(1, int(expires - now)))
        else:
            # The consent decision consumed its link; don't redirect back to a
            # now-inactive transaction and present a confusing 404.
            response = page(
                "Service connected",
                "<p>The task can now use the approved service access. Return to your client to follow its progress.</p>",
            )
        return response

    def close(self) -> None:
        self.sessions.clear()
