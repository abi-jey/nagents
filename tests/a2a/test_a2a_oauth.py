"""Browser PKCE is bound to one authenticated task and one browser session."""

from __future__ import annotations

import asyncio
import hashlib
import html.parser
import json
import os
import secrets
import stat
import tempfile
import time
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import TypedDict
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from a2a.helpers import new_text_message
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import TaskState
from aiohttp import web
from aiohttp.test_utils import TestServer

from nagents.a2a import request_credentials
from nagents.a2a.server.application import create_app
from nagents.a2a.server.authorization import AuthorizationDenied
from nagents.a2a.server.authorization import AuthorizationExpired
from nagents.a2a.server.authorization import AuthorizationSession
from nagents.a2a.server.authorization import AuthorizationUnavailable
from nagents.a2a.server.config import A2AConfig
from nagents.a2a.server.config import IssuerConfig
from nagents.a2a.server.config import ServiceConfig
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.session import SessionManager
from tests.a2a.test_a2a_auth import AUDIENCE
from tests.a2a.test_a2a_auth import IssuerState
from tests.a2a.test_a2a_auth import SignedKey
from tests.a2a.test_a2a_auth import access_claims
from tests.a2a.test_a2a_auth import local_issuer
from tests.a2a.test_a2a_auth import make_token
from tests.a2a.test_a2a_auth import signing_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Mapping

    from nagents.a2a.agent.runtime import AgentContext
    from nagents.a2a.server.oauth import AccessGrant
    from nagents.events import Event
    from nagents.types import Message

pytestmark = pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")

PUBLIC_URL = "http://127.0.0.1:42424"


class OAuthFixture:
    def __init__(self, key: SignedKey) -> None:
        self.key = key
        self.expected_challenge = ""
        self.token_requests: list[dict[str, str]] = []
        self.resource_authorizations: list[str] = []
        self.downstream_token = ""
        self.service_scope = "demo:read"
        self.token_error = ""
        self.issuer = ""

    def token(self, subject: str) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer,
                "sub": subject,
                "aud": [AUDIENCE, "account"],
                "iat": now,
                "exp": now + 300,
                "scope": "ngn:access ngn:approve",
                "tenant_id": "tenant-a",
            },
            self.key.private,
            algorithm=self.key.algorithm,
            headers={"kid": self.key.kid, "typ": "at+jwt"},
        )


async def identity_provider(state: OAuthFixture) -> TestServer:
    app = web.Application()

    async def discovery(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "issuer": state.issuer,
                "jwks_uri": f"{state.issuer}/jwks",
                "authorization_endpoint": f"{state.issuer}/authorize",
                "token_endpoint": f"{state.issuer}/token",
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def jwks(_: web.Request) -> web.Response:
        return web.json_response({"keys": [state.key.public_jwk]})

    async def authorize(_: web.Request) -> web.Response:
        return web.Response(text="The test drives the authorization redirect without a browser.")

    async def token(request: web.Request) -> web.Response:
        if state.token_error:
            return web.Response(status=500, text=state.token_error)
        form = {key: value for key, value in (await request.post()).items() if isinstance(value, str)}
        state.token_requests.append(form)
        verifier = form.get("code_verifier", "")
        challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or challenge != state.expected_challenge:
            return web.json_response({"error": "invalid_grant"}, status=400)
        subject = "mallory" if form.get("code") == "foreign-code" else "alice"
        access_token = state.downstream_token or state.token(subject)
        return web.json_response(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": state.service_scope if state.downstream_token else "ngn:access ngn:approve",
            }
        )

    async def resource(request: web.Request) -> web.Response:
        state.resource_authorizations.append(request.headers.get("Authorization", ""))
        return web.json_response({"value": "downstream result"})

    app.router.add_get("/.well-known/openid-configuration", discovery)
    app.router.add_get("/jwks", jwks)
    app.router.add_get("/authorize", authorize)
    app.router.add_post("/token", token)
    app.router.add_get("/resource", resource)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    state.issuer = str(server.make_url("/")).rstrip("/")
    return server


def request(message_id: str) -> SendMessageRequest:
    message = new_text_message("Review this one operation", role=Role.ROLE_USER)
    message.message_id = message_id
    return SendMessageRequest(message=message)


@asynccontextmanager
async def main_issuer(key: SignedKey) -> AsyncIterator[tuple[str, str]]:
    async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
        token = make_token(
            key,
            access_claims(issuer, scope="ngn:access ngn:invoke ngn:read ngn:cancel ngn:approve"),
        )
        yield issuer, token


def test_browser_login_uses_pkce_one_use_state_and_cookie_csrf(tmp_path: Path) -> None:
    async def scenario() -> None:
        key = signing_key("RS256", "browser-key")
        state = OAuthFixture(key)
        server = await identity_provider(state)
        try:
            config = A2AConfig(
                public_url=PUBLIC_URL,
                agent="assistant",
                issuer=IssuerConfig(
                    issuer=state.issuer,
                    audience=AUDIENCE,
                    algorithms=("RS256",),
                    tenant_claim="tenant_id",
                    scope_claim="scope",
                ),
                data_dir=tmp_path / "a2a-state",
                loopback_http=True,
                browser_client_id="browser-client",
                browser_scopes=("ngn:access", "ngn:approve"),
            )

            def factory(context: AgentContext) -> Agent:
                del context
                raise AssertionError("OAuth-only test should not start an Agent")

            app = create_app(config=config, agent_factory=factory)
            async with app.router.lifespan_context(app):
                service = app.state.a2a
                owner_token = state.token("alice")
                owner = await service.authenticator.authenticate(owner_token)
                claim = await service.store.reserve(request("oauth-approval"), owner)
                record = await service.authorizations.create(
                    claim.task_id,
                    owner,
                    "action",
                    {"tool": "publish", "arguments": {"destination": "fixture"}},
                )
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL) as browser:
                    login = await browser.get(
                        "/oauth/login", params={"authorization": record.id}, follow_redirects=False
                    )
                    assert login.status_code == 303
                    browser_cookie = browser.cookies.get("ngn_a2a_browser")
                    assert browser_cookie
                    authorization = urlsplit(login.headers["location"])
                    query = parse_qs(authorization.query)
                    state_token = query["state"][0]
                    assert query["client_id"] == ["browser-client"]
                    assert query["redirect_uri"] == [f"{PUBLIC_URL}/oauth/callback"]
                    assert query["code_challenge_method"] == ["S256"]
                    assert query["code_challenge"][0]
                    state.expected_challenge = query["code_challenge"][0]

                    callback = await browser.get(
                        "/oauth/callback", params={"state": state_token, "code": "good-code"}, follow_redirects=False
                    )
                    assert callback.status_code == 303
                    assert callback.headers["location"] == f"/authorize/{record.id}"
                    session_cookie = browser.cookies.get("ngn_a2a_session")
                    assert session_cookie and session_cookie != owner_token
                    assert owner_token not in callback.text
                    assert owner_token not in callback.headers.get("set-cookie", "")
                    assert state.token_requests[0]["code_verifier"]

                    review = await browser.get(f"/authorize/{record.id}", headers={"Accept": "application/json"})
                    assert review.status_code == 200
                    assert review.json()["operation"] == record.details

                    decision = {"approved": "true", "nonce": record.nonce, "digest": record.digest}
                    csrf = await browser.post(f"/authorize/{record.id}/decision", data=decision)
                    assert csrf.status_code == 403
                    approved = await browser.post(
                        f"/authorize/{record.id}/decision",
                        data=decision,
                        headers={"Origin": PUBLIC_URL},
                    )
                    assert approved.status_code == 200
                    with pytest.raises(AuthorizationUnavailable):
                        await service.authorizations.inspect(record.id, owner)

                    browser.cookies.delete("ngn_a2a_session")
                    replay = await browser.get("/oauth/callback", params={"state": state_token, "code": "good-code"})
                    assert replay.status_code == 404

                # The transaction is principal-bound: a token returned for a
                # different subject cannot complete an existing task's login.
                other_claim = await service.store.reserve(request("oauth-other-owner"), owner)
                other_record = await service.authorizations.create(
                    other_claim.task_id, owner, "action", {"tool": "private"}
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
                ) as other_browser:
                    other_login = await other_browser.get(
                        "/oauth/login", params={"authorization": other_record.id}, follow_redirects=False
                    )
                    other_state = parse_qs(urlsplit(other_login.headers["location"]).query)["state"][0]
                    state.expected_challenge = parse_qs(urlsplit(other_login.headers["location"]).query)[
                        "code_challenge"
                    ][0]
                    foreign = await other_browser.get(
                        "/oauth/callback",
                        params={"state": other_state, "code": "foreign-code"},
                        follow_redirects=False,
                    )
                    assert foreign.status_code == 404
                    assert await service.authorizations.inspect(other_record.id, owner) == other_record
                    assert len(state.token_requests) == 2
        finally:
            await server.close()

    asyncio.run(scenario())


def test_downstream_grant_is_private_scoped_and_consent_failures_do_not_release_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        main_key = signing_key("RS256", "main-a2a-key")
        downstream_state = OAuthFixture(signing_key("RS256", "downstream-key"))
        downstream_server = await identity_provider(downstream_state)
        try:
            downstream_state.downstream_token = "DOWNSTREAM_ONLY_PRIVATE_TOKEN"
            async with main_issuer(main_key) as (main_issuer_url, owner_token):
                config = A2AConfig(
                    public_url=PUBLIC_URL,
                    agent="assistant",
                    issuer=IssuerConfig(
                        issuer=main_issuer_url,
                        audience=AUDIENCE,
                        algorithms=("RS256",),
                        role_claim="realm_access.roles",
                        role_scopes={"approver": ("ngn:approve",)},
                    ),
                    data_dir=tmp_path / "a2a-service",
                    loopback_http=True,
                    max_active_per_principal=8,
                    max_active_tasks=8,
                    services={
                        "demo": ServiceConfig(
                            issuer=downstream_state.issuer,
                            client_id="downstream-client",
                            scopes=("demo:read",),
                            api_origin=downstream_state.issuer,
                            resource=f"{downstream_state.issuer}/resource",
                        )
                    },
                )

                def factory(context: AgentContext) -> Agent:
                    del context
                    raise AssertionError("Credential test does not start a model")

                app = create_app(config=config, agent_factory=factory)
                async with app.router.lifespan_context(app):
                    service = app.state.a2a
                    owner = await service.authenticator.authenticate(owner_token)

                    async def credential_session(
                        message_id: str,
                        *,
                        expire_immediately: bool = False,
                    ) -> tuple[AuthorizationSession, asyncio.Task[AccessGrant], list[tuple[TaskState, str]]]:
                        claim = await service.store.reserve(request(message_id), owner)
                        reports: list[tuple[TaskState, str]] = []

                        async def report(state: TaskState, text: str) -> None:
                            reports.append((state, text))
                            if expire_immediately and state == TaskState.TASK_STATE_AUTH_REQUIRED:
                                identifier = text.rsplit("/", 1)[-1]
                                pending = service.authorizations._pending[identifier]
                                fake_now = pending.record.expires_at + 1
                                expiry_patcher.setattr(
                                    "nagents.a2a.server.authorization.time",
                                    SimpleNamespace(time=lambda: fake_now),
                                )

                        session = AuthorizationSession(
                            service.authorizations,
                            claim.task_id,
                            owner,
                            report,
                            credential_handler=service.oauth.credentials,
                        )
                        task = asyncio.create_task(session.request_credentials("demo"))
                        for _ in range(100):
                            if reports:
                                break
                            await asyncio.sleep(0.005)
                        assert reports and reports[0][0] == TaskState.TASK_STATE_AUTH_REQUIRED
                        return session, task, reports

                    expiry_patcher = pytest.MonkeyPatch()

                    def authorization_id(reports: list[tuple[TaskState, str]]) -> str:
                        return reports[0][1].rsplit("/", 1)[-1]

                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
                    ) as browser:
                        _session, pending, reports = await credential_session("grant-approved")
                        identifier = authorization_id(reports)
                        record = await service.authorizations.inspect(identifier, owner)
                        assert record.kind == "oauth"
                        assert record.details["scopes"] == ["demo:read"]
                        review = await browser.get(
                            f"/authorize/{identifier}",
                            headers={"Authorization": f"Bearer {owner_token}", "Accept": "application/json"},
                        )
                        assert review.status_code == 200
                        connection = await browser.post(
                            f"/authorize/{identifier}/connect",
                            data={"nonce": record.nonce, "digest": record.digest},
                            headers={"Authorization": f"Bearer {owner_token}", "Origin": PUBLIC_URL},
                            follow_redirects=False,
                        )
                        assert connection.status_code == 303
                        authorization = urlsplit(connection.headers["location"])
                        query = parse_qs(authorization.query)
                        assert query["scope"] == ["demo:read"]
                        assert query["resource"] == [f"{downstream_state.issuer}/resource"]
                        state_token = query["state"][0]
                        downstream_state.expected_challenge = query["code_challenge"][0]
                        callback = await browser.get(
                            "/oauth/callback",
                            params={"state": state_token, "code": "service-code"},
                            headers={"Authorization": f"Bearer {owner_token}"},
                        )
                        assert callback.status_code == 200
                        assert downstream_state.token_requests[-1]["code_verifier"]
                        assert downstream_state.downstream_token not in callback.text
                        grant = await pending
                        callback_replay = await browser.get(
                            "/oauth/callback",
                            params={"state": state_token, "code": "service-code"},
                            headers={"Authorization": f"Bearer {owner_token}"},
                        )
                        assert callback_replay.status_code == 404
                        assert "DOWNSTREAM_ONLY_PRIVATE_TOKEN" not in repr(grant)
                        assert grant.service == "demo"
                        credential_path = service.oauth._credential_store(record, "demo").path
                        assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
                        assert "DOWNSTREAM_ONLY_PRIVATE_TOKEN" in credential_path.read_text()
                        assert [state for state, _ in reports] == [
                            TaskState.TASK_STATE_AUTH_REQUIRED,
                            TaskState.TASK_STATE_WORKING,
                        ]
                        assert "DOWNSTREAM_ONLY_PRIVATE_TOKEN" not in repr(reports)
                        attacker_requests: list[str] = []
                        attacker_app = web.Application()

                        async def attacker_resource(request: web.Request) -> web.Response:
                            attacker_requests.append(request.headers.get("Authorization", ""))
                            return web.json_response({"unexpected": "request reached wrong origin"})

                        attacker_app.router.add_get("/resource", attacker_resource)
                        async with (
                            TestServer(attacker_app, host="127.0.0.1") as attacker_server,
                            httpx.AsyncClient(auth=grant.auth(), trust_env=False, follow_redirects=False) as target,
                        ):
                            with pytest.raises(AuthorizationUnavailable):
                                await target.get(str(attacker_server.make_url("/resource")))
                            assert attacker_requests == []
                            target_response = await target.get(f"{downstream_state.issuer}/resource")
                            assert target_response.status_code == 200
                            grant.expires_at = time.time() - 1
                            with pytest.raises(AuthorizationUnavailable):
                                await target.get(f"{downstream_state.issuer}/resource")
                        assert attacker_requests == []
                        assert downstream_state.resource_authorizations == ["Bearer DOWNSTREAM_ONLY_PRIVATE_TOKEN"]
                        assert owner_token not in downstream_state.resource_authorizations[0]

                        denied_session, denied_task, denied_reports = await credential_session("grant-denied")
                        denied_id = authorization_id(denied_reports)
                        denied_record = await service.authorizations.inspect(denied_id, owner)
                        denied = await browser.post(
                            f"/authorize/{denied_id}/decision",
                            data={
                                "approved": "false",
                                "nonce": denied_record.nonce,
                                "digest": denied_record.digest,
                            },
                            headers={"Authorization": f"Bearer {owner_token}", "Origin": PUBLIC_URL},
                        )
                        assert denied.status_code == 200
                        with pytest.raises(AuthorizationDenied):
                            await denied_task
                        assert denied_session.failure is not None
                        assert len(downstream_state.resource_authorizations) == 1

                        canceled_session, canceled_task, canceled_reports = await credential_session("grant-canceled")
                        canceled_id = authorization_id(canceled_reports)
                        canceled_record = await service.authorizations.inspect(canceled_id, owner)
                        await service.authorizations.cancel_task(canceled_record.task_id)
                        with pytest.raises(asyncio.CancelledError):
                            await canceled_task
                        assert canceled_session.failure is None
                        assert len(downstream_state.resource_authorizations) == 1

                        expired_session, expired_task, _expired_reports = await credential_session(
                            "grant-expired", expire_immediately=True
                        )
                        try:
                            with pytest.raises(AuthorizationExpired):
                                await expired_task
                        finally:
                            expiry_patcher.undo()
                        assert expired_session.failure is not None
                        assert len(downstream_state.resource_authorizations) == 1

                        failed_session, failed_task, failed_reports = await credential_session("grant-idp-error")
                        failed_id = authorization_id(failed_reports)
                        failed_record = await service.authorizations.inspect(failed_id, owner)
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
                        ) as failed_browser:
                            failed_connect = await failed_browser.post(
                                f"/authorize/{failed_id}/connect",
                                data={"nonce": failed_record.nonce, "digest": failed_record.digest},
                                headers={
                                    "Authorization": f"Bearer {owner_token}",
                                    "Origin": PUBLIC_URL,
                                },
                                follow_redirects=False,
                            )
                            failed_query = parse_qs(urlsplit(failed_connect.headers["location"]).query)
                            failed_state = failed_query["state"][0]
                            downstream_state.expected_challenge = failed_query["code_challenge"][0]
                            downstream_state.token_error = "PRIVATE_IDP_ERROR_RESPONSE_BODY"
                            failure = await failed_browser.get(
                                "/oauth/callback",
                                params={"state": failed_state, "code": "failed-code"},
                                headers={"Authorization": f"Bearer {owner_token}"},
                            )
                            assert failure.status_code == 503
                            assert "PRIVATE_IDP_ERROR_RESPONSE_BODY" not in failure.text
                            downstream_state.token_error = ""
                        await service.authorizations.cancel_task(failed_record.task_id)
                        with pytest.raises(asyncio.CancelledError):
                            await failed_task
                        assert failed_session.failure is None
                        assert len(downstream_state.resource_authorizations) == 1

                        failed_session, failed_task, failed_reports = await credential_session("grant-error")
                        failed_id = authorization_id(failed_reports)
                        failed_record = await service.authorizations.inspect(failed_id, owner)
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
                        ) as failed_browser:
                            failed_connect = await failed_browser.post(
                                f"/authorize/{failed_id}/connect",
                                data={"nonce": failed_record.nonce, "digest": failed_record.digest},
                                headers={
                                    "Authorization": f"Bearer {owner_token}",
                                    "Origin": PUBLIC_URL,
                                },
                                follow_redirects=False,
                            )
                            failed_query = parse_qs(urlsplit(failed_connect.headers["location"]).query)
                            failed_state = failed_query["state"][0]
                            downstream_state.expected_challenge = failed_query["code_challenge"][0]
                            downstream_state.token_error = "PRIVATE_IDP_ERROR_RESPONSE_BODY"
                            failure = await failed_browser.get(
                                "/oauth/callback",
                                params={"state": failed_state, "code": "error-code"},
                                headers={"Authorization": f"Bearer {owner_token}"},
                            )
                            assert failure.status_code == 503
                            assert "PRIVATE_IDP_ERROR_RESPONSE_BODY" not in failure.text
                            downstream_state.token_error = ""
                        await service.authorizations.cancel_task(failed_record.task_id)
                        with pytest.raises(asyncio.CancelledError):
                            await failed_task
                        assert failed_session.failure is None
                        assert len(downstream_state.resource_authorizations) == 1
        finally:
            await downstream_server.close()

    asyncio.run(scenario())


class _ParsedKeycloakForm(TypedDict):
    action: str
    fields: dict[str, str]
    submit_controls: list[tuple[str, str]]


class _KeycloakLoginForm(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[_ParsedKeycloakForm] = []
        self.current: _ParsedKeycloakForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.current = {
                "action": values.get("action") or "",
                "fields": {},
                "submit_controls": [],
            }
            self.forms.append(self.current)
        elif tag in {"input", "button"} and self.current is not None:
            name = values.get("name")
            field_value = values.get("value") or ""
            if name:
                self.current["fields"][name] = field_value
            if name and (tag == "button" or (values.get("type") or "").lower() in {"submit", "image"}):
                self.current["submit_controls"].append((name, field_value))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


def _read_idp_fixture() -> dict[str, str] | None:
    directory = Path(os.environ.get("NGN_A2A_KEYCLOAK_FIXTURE", Path(tempfile.gettempdir()) / "ngn-a2a-idp"))
    try:
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            return None
        file = directory / "credentials.env"
        if stat.S_IMODE(file.stat().st_mode) != 0o600 or file.is_symlink():
            return None
        result: dict[str, str] = {}
        for line in file.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                result[name] = value
        return result
    except (OSError, ValueError):
        return None


async def _complete_keycloak_login(
    client: httpx.AsyncClient,
    authorization_url: str,
    *,
    issuer: str,
    redirect_uri: str,
    username: str,
    password: str,
    accept_consent: bool = False,
) -> tuple[str, str]:
    issuer_parts = urlsplit(issuer)
    callback_parts = urlsplit(redirect_uri)

    async def request(
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        built = client.build_request(method, url, data=data, headers=headers)
        parts = urlsplit(str(built.url))
        if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}:
            cookies = []
            for cookie in client.cookies.jar:
                if (
                    cookie.domain.lstrip(".") in {"127.0.0.1", "localhost"}
                    and parts.path.startswith(cookie.path or "/")
                    and (cookie.expires is None or cookie.expires > time.time())
                ):
                    cookies.append(f"{cookie.name}={cookie.value}")
            if cookies:
                built.headers["Cookie"] = "; ".join(cookies)
        return await client.send(built)

    response = await request("GET", authorization_url)
    for _ in range(12):
        if response.status_code in {301, 302, 303, 307, 308}:
            location = urljoin(str(response.url), response.headers.get("location", ""))
            parsed = urlsplit(location)
            if (parsed.scheme, parsed.netloc, parsed.path) == (
                callback_parts.scheme,
                callback_parts.netloc,
                callback_parts.path,
            ):
                query = parse_qs(parsed.query)
                code = (query.get("code") or [""])[0]
                state = (query.get("state") or [""])[0]
                if not code or not state:
                    raise AssertionError("Keycloak callback omitted its authorization code or state")
                return code, state
            if (parsed.scheme, parsed.netloc) != (issuer_parts.scheme, issuer_parts.netloc):
                raise AssertionError("Keycloak attempted an unexpected external redirect")
            response = await request("GET", location)
            continue
        if response.status_code != 200:
            raise AssertionError("Keycloak login returned an unexpected status")
        parser = _KeycloakLoginForm()
        parser.feed(response.text)
        login_form = next(
            (form for form in parser.forms if "username" in form["fields"] and "password" in form["fields"]),
            None,
        )
        if login_form is None:
            consent_form = next(
                (
                    form
                    for form in parser.forms
                    if any(
                        name.lower() == "accept" and value.lower() in {"", "yes", "accept", "true"}
                        for name, value in form["submit_controls"]
                    )
                ),
                None,
            )
            if not accept_consent or consent_form is None:
                raise AssertionError("Keycloak login or explicit consent form was not available")
            fields = consent_form["fields"].copy()
            accepted = next(
                control
                for control in consent_form["submit_controls"]
                if control[0].lower() == "accept" and control[1].lower() in {"", "yes", "accept", "true"}
            )
            for name, _value in consent_form["submit_controls"]:
                fields.pop(name, None)
            fields[accepted[0]] = accepted[1]
            action = urljoin(str(response.url), consent_form["action"])
            action_parts = urlsplit(action)
            if (action_parts.scheme, action_parts.netloc) != (issuer_parts.scheme, issuer_parts.netloc):
                raise AssertionError("Keycloak consent form had an unexpected origin")
            origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
            response = await request(
                "POST", action, data=fields, headers={"Origin": origin, "Referer": str(response.url)}
            )
            continue
        fields = login_form["fields"].copy()
        fields["username"] = username
        fields["password"] = password
        action = urljoin(str(response.url), login_form["action"])
        action_parts = urlsplit(action)
        if (action_parts.scheme, action_parts.netloc) != (issuer_parts.scheme, issuer_parts.netloc):
            raise AssertionError("Keycloak login form had an unexpected origin")
        origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
        response = await request("POST", action, data=fields, headers={"Origin": origin, "Referer": str(response.url)})
    raise AssertionError("Keycloak authorization redirect limit was exceeded")


async def _keycloak_user_tokens(settings: dict[str, str], username: str, password: str) -> tuple[str, str]:
    issuer = settings["NGN_IDP_ISSUER"]
    redirect = settings["NGN_IDP_BROWSER_REDIRECT_URI"]
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings["NGN_IDP_BROWSER_CLIENT_ID"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "openid " + settings["NGN_IDP_TEST_SCOPES"],
        "state": state,
        "nonce": secrets.token_urlsafe(24),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorization_url = settings["NGN_IDP_AUTHORIZATION_URL"] + "?" + urlencode(params)
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10) as client:
        code, returned_state = await _complete_keycloak_login(
            client,
            authorization_url,
            issuer=issuer,
            redirect_uri=redirect,
            username=username,
            password=password,
        )
        if returned_state != state:
            raise AssertionError("Keycloak returned a mismatched OAuth state")
        response = await client.post(
            settings["NGN_IDP_TOKEN_URL"],
            data={
                "grant_type": "authorization_code",
                "client_id": settings["NGN_IDP_BROWSER_CLIENT_ID"],
                "redirect_uri": redirect,
                "code": code,
                "code_verifier": verifier,
            },
        )
    if response.status_code != 200:
        raise AssertionError("Keycloak token exchange failed")
    tokens = response.json()
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")
    if not isinstance(access_token, str) or not isinstance(id_token, str):
        raise AssertionError("Keycloak did not return both browser token classes")
    return access_token, id_token


@pytest.mark.skipif(
    os.environ.get("NGN_A2A_KEYCLOAK_E2E") != "1",
    reason="Set NGN_A2A_KEYCLOAK_E2E=1 to use the private local Keycloak fixture",
)
def test_live_keycloak_a2a_and_browser_approval_are_owner_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        from tests.a2a.test_a2a_http import HTTPProvider
        from tests.a2a.test_a2a_http import object_map
        from tests.a2a.test_a2a_http import rpc
        from tests.a2a.test_a2a_http import send_params
        from tests.a2a.test_a2a_http import task_from

        credentials = _read_idp_fixture()
        if credentials is None:
            pytest.skip("The private local Keycloak fixture is not available")
        issuer_url = credentials["NGN_IDP_ISSUER"]
        discovery = issuer_url + "/.well-known/openid-configuration"
        async with httpx.AsyncClient(trust_env=False, timeout=5) as probe:
            try:
                ready = await probe.get(discovery)
            except httpx.HTTPError:
                pytest.skip("The configured local Keycloak fixture is not running")
        if ready.status_code != 200:
            pytest.skip("The configured local Keycloak fixture is not ready")

        alice_token, alice_id_token = await _keycloak_user_tokens(
            credentials,
            credentials["NGN_IDP_ALICE_USERNAME"],
            credentials["NGN_IDP_ALICE_PASSWORD"],
        )
        bob_token, _ = await _keycloak_user_tokens(
            credentials,
            credentials["NGN_IDP_BOB_USERNAME"],
            credentials["NGN_IDP_BOB_PASSWORD"],
        )
        public_url = credentials["NGN_IDP_BROWSER_REDIRECT_URI"].rsplit("/oauth/callback", 1)[0]
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client_credentials:
            service_token_response = await client_credentials.post(
                credentials["NGN_IDP_TOKEN_URL"],
                data={
                    "grant_type": "client_credentials",
                    "client_id": credentials["NGN_IDP_SERVICE_CLIENT_ID"],
                    "client_secret": credentials["NGN_IDP_SERVICE_CLIENT_SECRET"],
                    "scope": credentials["NGN_IDP_TEST_SCOPES"],
                },
            )
        assert service_token_response.status_code == 200
        service_token = service_token_response.json().get("access_token")
        assert isinstance(service_token, str)
        config = A2AConfig(
            public_url=public_url,
            agent="assistant",
            issuer=IssuerConfig(
                issuer=issuer_url,
                audience=credentials["NGN_IDP_EXPECTED_AUDIENCE"],
                algorithms=("RS256",),
                token_kind_claim="typ",
                token_kind_value="Bearer",
                scope_claim="scope",
            ),
            data_dir=tmp_path / "keycloak-a2a-state",
            loopback_http=True,
            browser_client_id=credentials["NGN_IDP_BROWSER_CLIENT_ID"],
            browser_scopes=tuple(credentials["NGN_IDP_TEST_SCOPES"].split()),
        )
        marker = tmp_path / "live-keycloak-approved.marker"
        secret_argument = "LIVE_KEYCLOAK_PRIVATE_APPROVAL_ARGUMENT"

        async def mark_approved(payload: str) -> str:
            marker.write_text(payload)
            return "operation complete"

        async def script(provider: HTTPProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="keycloak-tool", name="mark_approved", arguments={"payload": secret_argument})
            else:
                yield TextDoneEvent(text="The approved operation completed.")
                yield DoneEvent(final_text="The approved operation completed.")
            del messages

        agents: list[Agent] = []
        providers: list[HTTPProvider] = []

        def factory(context: AgentContext) -> Agent:
            provider = HTTPProvider(script)
            providers.append(provider)
            agent = Agent(
                provider,
                SessionManager(context.data_dir / "sessions.db"),
                tools=[mark_approved],
                system_prompt="Local Keycloak E2E agent.",
                streaming=True,
                compaction_strategy=None,
                save_tool_outputs=False,
            )
            agents.append(agent)
            return agent

        app = create_app(config=config, agent_factory=factory)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=public_url) as client,
        ):
            machine_tasks = await client.post(
                "/rpc",
                json=rpc("ListTasks", {"pageSize": 10}, "machine-principal"),
                headers={"Authorization": f"Bearer {service_token}", "A2A-Version": "1.0"},
            )
            assert machine_tasks.status_code == 200
            machine_result = object_map(machine_tasks.json().get("result"))
            assert machine_result.get("tasks") == []
            assert machine_result.get("totalSize") == 0
            unauthorized = await client.post(
                "/rpc",
                json=rpc("GetTask", {"id": "not-a-task"}, "id-token"),
                headers={"Authorization": f"Bearer {alice_id_token}", "A2A-Version": "1.0"},
            )
            assert unauthorized.status_code == 401

            alice_send = await client.post(
                "/rpc",
                json=rpc("SendMessage", send_params("live-alice", immediate=True)),
                headers={"Authorization": f"Bearer {alice_token}", "A2A-Version": "1.0"},
            )
            assert alice_send.status_code == 200
            alice_task = task_from(alice_send.json())
            alice_task_id = alice_task.get("id")
            assert isinstance(alice_task_id, str)

            async def wait_auth(task_id: str, token: str) -> dict[str, object]:
                last: dict[str, object] = {}
                for _ in range(200):
                    response = await client.post(
                        "/rpc",
                        json=rpc("GetTask", {"id": task_id}, "poll-auth"),
                        headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
                    )
                    last = task_from(response.json())
                    status = last.get("status")
                    if isinstance(status, dict) and status.get("state") == "TASK_STATE_AUTH_REQUIRED":
                        return last
                    await asyncio.sleep(0.01)
                raise AssertionError("A2A task did not reach its approval state")

            alice_pending = await wait_auth(alice_task_id, alice_token)
            alice_status = object_map(alice_pending.get("status"))
            alice_message = object_map(alice_status.get("message"))
            alice_parts = alice_message.get("parts")
            assert isinstance(alice_parts, list) and alice_parts
            alice_url = object_map(alice_parts[0]).get("text")
            assert isinstance(alice_url, str)
            alice_authorization_id = alice_url.rsplit("/", 1)[-1]

            foreign_get = await client.post(
                "/rpc",
                json=rpc("GetTask", {"id": alice_task_id}, "bob-cannot-read"),
                headers={"Authorization": f"Bearer {bob_token}", "A2A-Version": "1.0"},
            )
            assert "result" not in object_map(foreign_get.json())

            login = await client.get(
                "/oauth/login",
                params={"authorization": alice_authorization_id},
                follow_redirects=False,
            )
            external_location = login.headers["location"]
            async with httpx.AsyncClient(trust_env=False, timeout=10) as browser:
                code, state = await _complete_keycloak_login(
                    browser,
                    external_location,
                    issuer=issuer_url,
                    redirect_uri=credentials["NGN_IDP_BROWSER_REDIRECT_URI"],
                    username=credentials["NGN_IDP_ALICE_USERNAME"],
                    password=credentials["NGN_IDP_ALICE_PASSWORD"],
                )
            callback = await client.get(
                "/oauth/callback",
                params={"code": code, "state": state},
                follow_redirects=False,
            )
            assert callback.status_code == 303
            assert callback.headers["location"] == f"/authorize/{alice_authorization_id}"
            review = await client.get(
                f"/authorize/{alice_authorization_id}",
                headers={"Accept": "application/json"},
            )
            assert review.status_code == 200
            decision = await client.post(
                f"/authorize/{alice_authorization_id}/decision",
                data={
                    "approved": "true",
                    "nonce": review.json()["nonce"],
                    "digest": review.json()["digest"],
                },
                headers={"Origin": public_url},
            )
            assert decision.status_code == 200
            for _ in range(200):
                fetched = await client.post(
                    "/rpc",
                    json=rpc("GetTask", {"id": alice_task_id}, "poll-complete"),
                    headers={"Authorization": f"Bearer {alice_token}", "A2A-Version": "1.0"},
                )
                completed = task_from(fetched.json())
                if object_map(completed.get("status")).get("state") == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.01)
            assert marker.read_text() == secret_argument
            assert secret_argument not in json.dumps(completed)
            assert len(agents) == len(providers) == 1

            bob_send = await client.post(
                "/rpc",
                json=rpc("SendMessage", send_params("live-bob", immediate=True)),
                headers={"Authorization": f"Bearer {bob_token}", "A2A-Version": "1.0"},
            )
            bob_task = task_from(bob_send.json())
            bob_task_id = bob_task.get("id")
            assert isinstance(bob_task_id, str)
            bob_pending = await wait_auth(bob_task_id, bob_token)
            bob_status = object_map(bob_pending.get("status"))
            bob_message = object_map(bob_status.get("message"))
            bob_parts = bob_message.get("parts")
            assert isinstance(bob_parts, list) and bob_parts
            bob_url = object_map(bob_parts[0]).get("text")
            assert isinstance(bob_url, str)
            bob_authorization_id = bob_url.rsplit("/", 1)[-1]

            alice_wrong_owner = await client.get(
                "/oauth/login",
                params={"authorization": bob_authorization_id},
                follow_redirects=False,
            )
            async with httpx.AsyncClient(trust_env=False, timeout=10) as browser:
                foreign_code, foreign_state = await _complete_keycloak_login(
                    browser,
                    alice_wrong_owner.headers["location"],
                    issuer=issuer_url,
                    redirect_uri=credentials["NGN_IDP_BROWSER_REDIRECT_URI"],
                    username=credentials["NGN_IDP_ALICE_USERNAME"],
                    password=credentials["NGN_IDP_ALICE_PASSWORD"],
                )
            rejected = await client.get(
                "/oauth/callback",
                params={"code": foreign_code, "state": foreign_state},
                follow_redirects=False,
            )
            assert rejected.status_code == 404
            assert not marker.with_name("live-keycloak-bob.marker").exists()

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.environ.get("NGN_A2A_KEYCLOAK_E2E") != "1",
    reason="Set NGN_A2A_KEYCLOAK_E2E=1 to use the private local Keycloak fixture",
)
def test_live_keycloak_downstream_consent_is_task_and_owner_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        from tests.a2a.test_a2a_http import HTTPProvider
        from tests.a2a.test_a2a_http import object_map
        from tests.a2a.test_a2a_http import rpc
        from tests.a2a.test_a2a_http import send_params
        from tests.a2a.test_a2a_http import task_from

        credentials = _read_idp_fixture()
        if credentials is None:
            pytest.skip("The private local Keycloak fixture is not available")
        issuer_url = credentials["NGN_IDP_ISSUER"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as probe:
            try:
                discovery_response = await probe.get(issuer_url + "/.well-known/openid-configuration")
                if discovery_response.status_code == 200:
                    discovery = discovery_response.json()
                    jwks_url = discovery.get("jwks_uri")
                    assert isinstance(jwks_url, str)
                    jwks_response = await probe.get(jwks_url)
            except httpx.HTTPError:
                pytest.skip("The configured local Keycloak fixture is not running")
        if discovery_response.status_code != 200:
            pytest.skip("The configured local Keycloak fixture is not ready")
        assert jwks_response.status_code == 200

        alice_token, _alice_id_token = await _keycloak_user_tokens(
            credentials, credentials["NGN_IDP_ALICE_USERNAME"], credentials["NGN_IDP_ALICE_PASSWORD"]
        )
        bob_token, _bob_id_token = await _keycloak_user_tokens(
            credentials, credentials["NGN_IDP_BOB_USERNAME"], credentials["NGN_IDP_BOB_PASSWORD"]
        )
        public_url = credentials["NGN_IDP_BROWSER_REDIRECT_URI"].rsplit("/oauth/callback", 1)[0]
        resource_observations: list[dict[str, object]] = []
        received_tokens: list[str] = []
        incoming_tokens = {alice_token, bob_token}
        audience = credentials["NGN_IDP_DOWNSTREAM_AUDIENCE"]
        required_scope = credentials["NGN_IDP_DOWNSTREAM_SCOPE"]
        jwks = jwks_response.json()
        assert isinstance(jwks, dict) and isinstance(jwks.get("keys"), list)

        resource_app = web.Application()

        async def resource(request: web.Request) -> web.Response:
            authorization = request.headers.get("Authorization", "")
            assert authorization.startswith("Bearer ")
            downstream_token = authorization.removeprefix("Bearer ")
            assert downstream_token not in incoming_tokens
            received_tokens.append(downstream_token)
            header = jwt.get_unverified_header(downstream_token)
            keys = jwks["keys"]
            assert isinstance(keys, list)
            jwk = next(item for item in keys if isinstance(item, dict) and item.get("kid") == header.get("kid"))
            claims = jwt.decode(
                downstream_token,
                jwt.PyJWK.from_dict(jwk).key,
                algorithms=["RS256"],
                audience=audience,
                issuer=issuer_url,
            )
            token_audience = claims["aud"]
            assert audience == token_audience or (isinstance(token_audience, list) and audience in token_audience)
            scopes = claims.get("scope", "").split()
            assert required_scope in scopes
            resource_observations.append({"aud": audience, "scope": scopes})
            return web.json_response({"result": "consented resource response"})

        resource_app.router.add_get("/resource", resource)
        async with TestServer(resource_app, host="127.0.0.1") as resource_server:
            resource_url = str(resource_server.make_url("/resource"))
            api_origin = str(resource_server.make_url("/")).rstrip("/")
            config = A2AConfig(
                public_url=public_url,
                agent="assistant",
                issuer=IssuerConfig(
                    issuer=issuer_url,
                    audience=credentials["NGN_IDP_EXPECTED_AUDIENCE"],
                    algorithms=("RS256",),
                    token_kind_claim="typ",
                    token_kind_value="Bearer",
                    scope_claim="scope",
                ),
                data_dir=tmp_path / "keycloak-downstream-state",
                loopback_http=True,
                browser_client_id=credentials["NGN_IDP_BROWSER_CLIENT_ID"],
                services={
                    "demo": ServiceConfig(
                        issuer=issuer_url,
                        client_id=credentials["NGN_IDP_DOWNSTREAM_CLIENT_ID"],
                        scopes=(required_scope,),
                        api_origin=api_origin,
                        resource=resource_url,
                    )
                },
            )
            calls: list[str] = []

            async def fetch_demo() -> str:
                grant = await request_credentials("demo")
                async with httpx.AsyncClient(auth=grant.auth(), trust_env=False, follow_redirects=False) as target:
                    response = await target.get(resource_url)
                response.raise_for_status()
                calls.append("resource fetched")
                result = response.json().get("result")
                if not isinstance(result, str):
                    raise RuntimeError("Downstream service returned no public result")
                return result

            async def script(provider: HTTPProvider, messages: list[Message]) -> AsyncIterator[Event]:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="fetch-demo", name="fetch_demo", arguments={})
                else:
                    yield TextDoneEvent(text="The consented resource returned successfully.")
                    yield DoneEvent(final_text="The consented resource returned successfully.")
                del messages

            providers: list[HTTPProvider] = []
            agents: list[Agent] = []

            def factory(context: AgentContext) -> Agent:
                provider = HTTPProvider(script)
                providers.append(provider)
                agent = Agent(
                    provider,
                    SessionManager(context.data_dir / "sessions.db"),
                    tools=[fetch_demo],
                    system_prompt="Use the configured demo service only when the host provides consent.",
                    streaming=True,
                    compaction_strategy=None,
                    save_tool_outputs=False,
                )
                agents.append(agent)
                return agent

            app = create_app(config=config, agent_factory=factory, read_only_tools=frozenset({"fetch_demo"}))
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=public_url) as client,
            ):
                sent = await client.post(
                    "/rpc",
                    json=rpc("SendMessage", send_params("downstream-consent", immediate=True)),
                    headers={"Authorization": f"Bearer {alice_token}", "A2A-Version": "1.0"},
                )
                assert sent.status_code == 200, sent.text
                task = task_from(sent.json())
                task_id = task.get("id")
                assert isinstance(task_id, str)

                pending: dict[str, object] = {}
                for _ in range(300):
                    fetched = await client.post(
                        "/rpc",
                        json=rpc("GetTask", {"id": task_id}, "downstream-pending"),
                        headers={"Authorization": f"Bearer {alice_token}", "A2A-Version": "1.0"},
                    )
                    pending = task_from(fetched.json())
                    if object_map(pending.get("status")).get("state") == "TASK_STATE_AUTH_REQUIRED":
                        break
                    await asyncio.sleep(0.01)
                assert object_map(pending.get("status")).get("state") == "TASK_STATE_AUTH_REQUIRED"
                assert resource_observations == []
                authorization_message = object_map(object_map(pending["status"]).get("message"))
                parts = authorization_message.get("parts")
                assert isinstance(parts, list) and parts
                authorization_url = object_map(parts[0]).get("text")
                assert isinstance(authorization_url, str)
                authorization_id = authorization_url.rsplit("/", 1)[-1]

                bob_review = await client.get(
                    f"/authorize/{authorization_id}",
                    headers={"Authorization": f"Bearer {bob_token}", "Accept": "application/json"},
                )
                assert bob_review.status_code != 200
                alice_review = await client.get(
                    f"/authorize/{authorization_id}",
                    headers={"Authorization": f"Bearer {alice_token}", "Accept": "application/json"},
                )
                assert alice_review.status_code == 200
                assert alice_review.json()["operation"]["service"] == "demo"
                principal = await app.state.a2a.authenticator.authenticate(alice_token)
                authorization_record = await app.state.a2a.authorizations.inspect(authorization_id, principal)
                credential_path = app.state.a2a.oauth._credential_store(authorization_record, "demo").path
                foreign_connect = await client.post(
                    f"/authorize/{authorization_id}/connect",
                    data={"nonce": alice_review.json()["nonce"], "digest": alice_review.json()["digest"]},
                    headers={"Authorization": f"Bearer {bob_token}", "Origin": public_url},
                    follow_redirects=False,
                )
                assert foreign_connect.status_code != 303
                assert resource_observations == []
                connect = await client.post(
                    f"/authorize/{authorization_id}/connect",
                    data={"nonce": alice_review.json()["nonce"], "digest": alice_review.json()["digest"]},
                    headers={"Authorization": f"Bearer {alice_token}", "Origin": public_url},
                    follow_redirects=False,
                )
                assert connect.status_code == 303
                authorization_endpoint = connect.headers["location"]
                query = parse_qs(urlsplit(authorization_endpoint).query)
                assert query["client_id"] == [credentials["NGN_IDP_DOWNSTREAM_CLIENT_ID"]]
                assert query["scope"] == [required_scope]
                assert query["resource"] == [resource_url]

                async with httpx.AsyncClient(trust_env=False, timeout=10) as browser:
                    code, state = await _complete_keycloak_login(
                        browser,
                        authorization_endpoint,
                        issuer=issuer_url,
                        redirect_uri=credentials["NGN_IDP_BROWSER_REDIRECT_URI"],
                        username=credentials["NGN_IDP_ALICE_USERNAME"],
                        password=credentials["NGN_IDP_ALICE_PASSWORD"],
                        accept_consent=True,
                    )
                callback = await client.get(
                    "/oauth/callback",
                    params={"code": code, "state": state},
                    headers={"Authorization": f"Bearer {alice_token}"},
                    follow_redirects=False,
                )
                assert callback.status_code == 200
                assert "Service connected" in callback.text
                replay = await client.get(
                    "/oauth/callback",
                    params={"code": code, "state": state},
                    headers={"Authorization": f"Bearer {alice_token}"},
                    follow_redirects=False,
                )
                assert replay.status_code == 404
                completed: dict[str, object] = {}
                for _ in range(300):
                    fetched = await client.post(
                        "/rpc",
                        json=rpc("GetTask", {"id": task_id}, "downstream-completed"),
                        headers={"Authorization": f"Bearer {alice_token}", "A2A-Version": "1.0"},
                    )
                    completed = task_from(fetched.json())
                    if object_map(completed.get("status")).get("state") in {
                        "TASK_STATE_COMPLETED",
                        "TASK_STATE_FAILED",
                    }:
                        break
                    await asyncio.sleep(0.01)
                assert object_map(completed.get("status")).get("state") == "TASK_STATE_COMPLETED"
                assert resource_observations == [{"aud": audience, "scope": [required_scope]}]
                assert calls == ["resource fetched"]
                assert len(agents) == len(providers) == 1
                assert len(providers[0].requests) == 2
                assert any("consented resource response" in repr(message) for message in providers[0].requests[-1])
                assert credential_path.stat().st_mode & 0o777 == 0o600
                stored = json.loads(credential_path.read_text(encoding="utf-8"))
                assert stored["access_token"] == received_tokens[0]
                serialized = json.dumps([pending, completed])
                assert "access_token" not in serialized.lower()
                assert "Bearer " not in serialized
                assert len(received_tokens) == 1 and received_tokens[0] not in serialized

    asyncio.run(scenario())
