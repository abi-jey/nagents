"""Signed JWT/OIDC boundaries for the optional A2A resource server."""

from __future__ import annotations

import asyncio
import json
import time
from base64 import b64encode
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast
from urllib.parse import quote_plus

import aiohttp
import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from nagents.a2a.auth import AuthenticationError
from nagents.a2a.auth import Authenticator
from nagents.a2a.auth import Principal
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


SigningPrivateKey = RSAPrivateKey | EllipticCurvePrivateKey


AUDIENCE = "nagents-a2a"


@dataclass
class IssuerState:
    keys: list[dict[str, object]]
    jwks_requests: int = 0
    introspection_status: int = 200
    introspection_payload: dict[str, object] | None = None
    introspection_raw: str | None = None
    introspection_requests: int = 0


@dataclass(frozen=True)
class SignedKey:
    kid: str
    algorithm: str
    private: SigningPrivateKey
    public_jwk: dict[str, object]


def signing_key(algorithm: str, kid: str) -> SignedKey:
    if algorithm == "RS256":
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = cast("dict[str, object]", jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True))
        public_jwk.update({"kid": kid, "alg": algorithm, "use": "sig", "key_ops": ["verify"]})
        return SignedKey(kid, algorithm, private, public_jwk)
    if algorithm == "ES256":
        ec_private = ec.generate_private_key(ec.SECP256R1())
        public_jwk = cast("dict[str, object]", jwt.algorithms.ECAlgorithm.to_jwk(ec_private.public_key(), as_dict=True))
        public_jwk.update({"kid": kid, "alg": algorithm, "use": "sig", "key_ops": ["verify"]})
        return SignedKey(kid, algorithm, ec_private, public_jwk)
    raise AssertionError(f"Unexpected test signing algorithm: {algorithm}")


def access_claims(issuer: str, **changes: object) -> dict[str, object]:
    now = int(time.time())
    values: dict[str, object] = {
        "iss": issuer,
        "sub": "alice-subject",
        "aud": [AUDIENCE, "account"],
        "iat": now,
        "exp": now + 300,
        "scope": "ngn:access",
        "tenant_id": "tenant-a",
        "realm_access": {"roles": ["approver"]},
    }
    values.update(changes)
    return values


def make_token(
    key: SignedKey,
    claims: dict[str, object],
    *,
    header_type: str = "at+jwt",
    header_updates: dict[str, object] | None = None,
) -> str:
    headers: dict[str, object] = {"kid": key.kid, "typ": header_type}
    if header_updates:
        headers.update(header_updates)
    return jwt.encode(claims, key.private, algorithm=key.algorithm, headers=headers)


def issuer_config(
    issuer: str,
    *,
    validation: str = "jwt",
    token_kind_claim: str = "",
    token_kind_value: str = "",
    scope_claim: str = "scope",
    tenant_claim: str = "tenant_id",
    role_claim: str = "realm_access.roles",
    role_scopes: dict[str, tuple[str, ...]] | None = None,
) -> IssuerConfig:
    return IssuerConfig(
        issuer=issuer,
        audience=AUDIENCE,
        algorithms=("RS256", "ES256"),
        validation=validation,
        token_kind_claim=token_kind_claim,
        token_kind_value=token_kind_value,
        scope_claim=scope_claim,
        tenant_claim=tenant_claim,
        role_claim=role_claim,
        role_scopes=role_scopes or {"approver": ("ngn:approve",)},
        introspection_endpoint=f"{issuer}/introspect" if validation == "introspection" else "",
        client_id="ngn-test-resource" if validation == "introspection" else "",
        client_secret_env="NGN_A2A_TEST_INTROSPECTION_SECRET" if validation == "introspection" else "",
    )


@asynccontextmanager
async def local_issuer(state: IssuerState) -> AsyncIterator[tuple[str, IssuerState]]:
    app = web.Application()

    async def discovery(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "issuer": base,
                "jwks_uri": f"{base}/jwks",
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
            }
        )

    async def jwks(_: web.Request) -> web.Response:
        state.jwks_requests += 1
        return web.json_response({"keys": state.keys})

    async def introspect(request: web.Request) -> web.Response:
        state.introspection_requests += 1
        if state.introspection_status == 302:
            return web.Response(status=302, headers={"Location": f"{base}/must-not-follow"})
        if state.introspection_status != 200:
            return web.Response(status=state.introspection_status, text="unavailable")
        encoded_credentials = b64encode(
            f"{quote_plus('ngn-test-resource')}:{quote_plus('synthetic-introspection-secret')}".encode()
        ).decode("ascii")
        if request.headers.get("Authorization") != f"Basic {encoded_credentials}":
            return web.json_response({"error": "unexpected test client"}, status=401)
        if state.introspection_raw is not None:
            return web.Response(text=state.introspection_raw, content_type="application/json")
        return web.json_response(state.introspection_payload or {})

    async def forbidden_follow(_: web.Request) -> web.Response:
        return web.json_response({"active": True, "iss": base, "aud": AUDIENCE, "sub": "must-not-follow"})

    app.router.add_get("/.well-known/openid-configuration", discovery)
    app.router.add_get("/jwks", jwks)
    app.router.add_post("/introspect", introspect)
    app.router.add_get("/must-not-follow", forbidden_follow)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    try:
        yield base, state
    finally:
        await server.close()


def config(issuer: IssuerConfig, path: Path) -> A2AConfig:
    return A2AConfig(
        public_url="http://127.0.0.1:42424",
        agent="assistant",
        issuer=issuer,
        data_dir=path.resolve(),
        loopback_http=True,
    )


@asynccontextmanager
async def authenticator(settings: A2AConfig) -> AsyncIterator[Authenticator]:
    async with aiohttp.ClientSession() as http:
        value = Authenticator(settings, http)
        await value.initialize()
        yield value


@pytest.mark.parametrize("algorithm", ["RS256", "ES256"])
def test_valid_signed_access_jwt_builds_scoped_tenant_principal(tmp_path: Path, algorithm: str) -> None:
    async def scenario() -> None:
        key = signing_key(algorithm, "current-key")
        state = IssuerState([key.public_jwk])
        async with local_issuer(state) as (base, _):
            settings = config(issuer_config(base), tmp_path)
            async with authenticator(settings) as auth:
                principal = await auth.authenticate(make_token(key, access_claims(base)))
                assert isinstance(principal, Principal)
                assert principal.issuer == base
                assert principal.subject == "alice-subject"
                assert principal.tenant == "tenant-a"
                assert principal.scopes == frozenset({"ngn:access", "ngn:approve"})
                principal.require("ngn:access")
                principal.require("ngn:approve")
                with pytest.raises(AuthenticationError) as denied:
                    principal.require("ngn:admin")
                assert denied.value.code == "insufficient_scope" and denied.value.status == 403
                assert len(principal.user_name) == 64
                assert "alice-subject" not in principal.user_name
                same_owner = Principal(base, "alice-subject", "tenant-a", principal.scopes, principal.expires_at)
                other_tenant = Principal(base, "alice-subject", "tenant-b", principal.scopes, principal.expires_at)
                other_issuer = Principal(
                    "https://another-issuer.example",
                    "alice-subject",
                    "tenant-a",
                    principal.scopes,
                    principal.expires_at,
                )
                assert same_owner.user_name == principal.user_name
                assert other_tenant.user_name != principal.user_name
                assert other_issuer.user_name != principal.user_name

    asyncio.run(scenario())


def test_keycloak_bearer_claim_is_explicit_and_id_token_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        key = signing_key("RS256", "keycloak-key")
        state = IssuerState([key.public_jwk])
        async with local_issuer(state) as (base, _):
            settings = config(issuer_config(base, token_kind_claim="typ", token_kind_value="Bearer"), tmp_path)
            async with authenticator(settings) as auth:
                access = await auth.authenticate(make_token(key, access_claims(base, typ="Bearer"), header_type="JWT"))
                assert access.subject == "alice-subject"
                id_token = make_token(
                    key,
                    access_claims(
                        base, typ="ID", nonce="browser-nonce", auth_time=int(time.time()), azp="browser-client"
                    ),
                    header_type="JWT",
                )
                with pytest.raises(AuthenticationError) as rejected:
                    await auth.authenticate(id_token)
                assert rejected.value.code == "invalid_token" and rejected.value.status == 401

    asyncio.run(scenario())


def test_rfc9068_header_rejects_id_token_even_when_audience_matches(tmp_path: Path) -> None:
    async def scenario() -> None:
        key = signing_key("RS256", "token-class-key")
        state = IssuerState([key.public_jwk])
        async with local_issuer(state) as (base, _), authenticator(config(issuer_config(base), tmp_path)) as auth:
            id_token = make_token(
                key,
                access_claims(base, nonce="browser-nonce", auth_time=int(time.time()), azp="browser-client"),
                header_type="JWT",
            )
            with pytest.raises(AuthenticationError) as rejected:
                await auth.authenticate(id_token)
            assert rejected.value.code == "invalid_token"
            assert rejected.value.status == 401

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "wrong_audience",
        "wrong_issuer",
        "expired",
        "not_yet_valid",
        "missing_subject",
        "malformed_scope",
        "missing_tenant",
    ],
)
def test_rejects_wrong_or_malformed_security_claims(tmp_path: Path, case: str) -> None:
    async def scenario() -> None:
        key = signing_key("RS256", "claims-key")
        state = IssuerState([key.public_jwk])
        async with local_issuer(state) as (base, _), authenticator(config(issuer_config(base), tmp_path)) as auth:
            now = int(time.time())
            claim_cases: dict[str, dict[str, object]] = {
                "wrong_audience": {"aud": "other-resource"},
                "wrong_issuer": {"iss": "https://attacker.invalid"},
                "expired": {"exp": now - 61},
                "not_yet_valid": {"nbf": now + 61},
                "missing_subject": {"sub": ""},
                "malformed_scope": {"scope": {"unexpected": "shape"}},
                "missing_tenant": {"tenant_id": ""},
            }
            claim_changes = claim_cases[case]
            token = make_token(key, access_claims(base, **claim_changes))
            with pytest.raises(AuthenticationError) as rejected:
                await auth.authenticate(token)
            assert rejected.value.code == "invalid_token" and rejected.value.status == 401
            assert token not in str(rejected.value)

    asyncio.run(scenario())


def test_rejects_bad_signature_and_symmetric_algorithm_without_leaking_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        trusted = signing_key("RS256", "known-key")
        attacker = signing_key("RS256", "known-key")
        state = IssuerState([trusted.public_jwk])
        async with local_issuer(state) as (base, _), authenticator(config(issuer_config(base), tmp_path)) as auth:
            forged = make_token(attacker, access_claims(base))
            symmetric = jwt.encode(
                access_claims(base),
                "synthetic-key-material-that-is-longer-than-32-bytes",
                algorithm="HS256",
                headers={"kid": "known-key", "typ": "at+jwt"},
            )
            for token in (forged, symmetric):
                with pytest.raises(AuthenticationError) as rejected:
                    await auth.authenticate(token)
                assert rejected.value.code == "invalid_token"
                assert token not in str(rejected.value)

    asyncio.run(scenario())


def test_jwks_rotation_uses_configured_issuer_only_and_ignores_jku(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = signing_key("RS256", "first-key")
        second = signing_key("RS256", "rotated-key")
        state = IssuerState([first.public_jwk])
        attacker_requests = 0
        attacker_app = web.Application()

        async def attacker_jwks(_: web.Request) -> web.Response:
            nonlocal attacker_requests
            attacker_requests += 1
            return web.json_response({"keys": [first.public_jwk]})

        attacker_app.router.add_get("/jwks", attacker_jwks)
        attacker = TestServer(attacker_app, host="127.0.0.1")
        await attacker.start_server()
        attacker_base = str(attacker.make_url("/")).rstrip("/")
        try:
            async with (
                local_issuer(state) as (base, issuer_state),
                authenticator(config(issuer_config(base), tmp_path)) as auth,
            ):
                assert (await auth.authenticate(make_token(first, access_claims(base)))).subject == "alice-subject"
                issuer_state.keys[:] = [second.public_jwk]
                rotated = make_token(second, access_claims(base))
                assert (await auth.authenticate(rotated)).subject == "alice-subject"
                assert issuer_state.jwks_requests == 2

                untrusted = make_token(
                    second,
                    access_claims("https://attacker.invalid"),
                    header_updates={"jku": f"{attacker_base}/jwks", "x5u": f"{attacker_base}/certificate"},
                )
                with pytest.raises(AuthenticationError) as rejected:
                    await auth.authenticate(untrusted)
                assert rejected.value.code == "invalid_token"
                assert attacker_requests == 0
        finally:
            await attacker.close()

    asyncio.run(scenario())


def test_introspection_uses_operator_endpoint_and_rejects_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("NGN_A2A_TEST_INTROSPECTION_SECRET", "synthetic-introspection-secret")
        state = IssuerState(
            [],
            introspection_payload={
                "active": True,
                "token_type": "Bearer",
                "iss": "placeholder",
                "aud": AUDIENCE,
                "sub": "service-subject",
                "exp": int(time.time()) + 300,
                "scope": "ngn:access",
                "tenant_id": "tenant-a",
            },
        )
        async with local_issuer(state) as (base, issuer_state):
            payload = issuer_state.introspection_payload
            assert payload is not None
            payload["iss"] = base
            async with authenticator(config(issuer_config(base, validation="introspection"), tmp_path)) as auth:
                principal = await auth.authenticate("opaque-access-token")
                assert principal.subject == "service-subject"
                assert principal.scopes == frozenset({"ngn:access"})
                assert issuer_state.introspection_requests == 1

                issuer_state.introspection_status = 302
                with pytest.raises(AuthenticationError) as unavailable:
                    await auth.authenticate("another-opaque-token")
                assert unavailable.value.code == "authorization_service_unavailable"
                assert unavailable.value.status == 503
                assert issuer_state.introspection_requests == 2

                issuer_state.introspection_status = 200
                issuer_state.introspection_raw = "{not-json"
                with pytest.raises(AuthenticationError) as malformed:
                    await auth.authenticate("malformed-introspection-token")
                assert malformed.value.code == "authorization_service_unavailable"
                assert malformed.value.status == 503
                assert issuer_state.introspection_requests == 3

    asyncio.run(scenario())


def test_configuration_is_strict_and_loopback_http_is_explicit(tmp_path: Path) -> None:
    issuer_value: dict[str, object] = {
        "issuer": "https://issuer.example",
        "audience": AUDIENCE,
        "algorithms": ["RS256"],
        "validation": "jwt",
    }
    assert IssuerConfig.parse(issuer_value).issuer == "https://issuer.example"
    with pytest.raises(ValueError, match="Unknown A2A issuer configuration field"):
        IssuerConfig.parse({**issuer_value, "jwks_uri": "https://attacker.invalid/jwks"})
    with pytest.raises(ValueError, match="HTTPS"):
        IssuerConfig.parse({**issuer_value, "issuer": "http://issuer.example"}, loopback_http=True)
    assert IssuerConfig.parse({**issuer_value, "issuer": "http://127.0.0.1:4000"}, loopback_http=True)

    path = tmp_path / "a2a.json"
    document = {
        "public_url": "https://a2a.example",
        "agent": "assistant",
        "issuer": issuer_value,
        "data_dir": str(tmp_path / "state"),
    }
    path.write_text(json.dumps({**document, "authorization_endpoint": "https://attacker.invalid/authorize"}))
    with pytest.raises(ValueError, match="Unknown A2A configuration field"):
        A2AConfig.from_file(path)
    path.write_text(json.dumps(document))
    loaded = A2AConfig.from_file(path)
    assert loaded.public_url == "https://a2a.example"
    assert loaded.issuer.issuer == "https://issuer.example"
    assert loaded.data_dir == (tmp_path / "state").resolve()


def test_discovery_issuer_mismatch_fails_initialization(tmp_path: Path) -> None:
    async def mismatch() -> None:
        app = web.Application()
        base = ""

        async def discovery(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "issuer": "https://attacker.invalid",
                    "jwks_uri": f"{base}/jwks",
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                }
            )

        async def jwks(_: web.Request) -> web.Response:
            return web.json_response({"keys": []})

        app.router.add_get("/.well-known/openid-configuration", discovery)
        app.router.add_get("/jwks", jwks)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        base = str(server.make_url("/")).rstrip("/")
        try:
            settings = config(issuer_config(base), tmp_path)
            async with aiohttp.ClientSession() as http:
                with pytest.raises(ValueError, match="does not match"):
                    await Authenticator(settings, http).initialize()
        finally:
            await server.close()

    asyncio.run(mismatch())
