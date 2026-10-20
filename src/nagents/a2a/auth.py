"""OAuth resource-server authentication with no tokens in execution context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from base64 import b64encode
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import aiohttp
import jwt
from a2a.auth.user import User

from .config import A2AConfig
from .config import endpoint
from .config import mapping

if TYPE_CHECKING:
    from collections.abc import Mapping


class AuthenticationError(Exception):
    """A deliberately secret-free HTTP error, suitable for an auth challenge."""

    def __init__(self, code: str = "invalid_token", status: int = 401) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Principal(User):
    issuer: str
    subject: str
    tenant: str
    scopes: frozenset[str]
    expires_at: float

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        """An unambiguous owner key; no caller-controlled session identifiers."""
        raw = json.dumps([self.issuer, self.subject, self.tenant], separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def require(self, scope: str) -> None:
        if self.expires_at <= time.time():
            raise AuthenticationError()
        if scope not in self.scopes:
            raise AuthenticationError("insufficient_scope", 403)


def claim_value(claims: Mapping[str, object], path: str) -> object:
    value: object = claims
    for key in path.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(key, "")
    return value


class Authenticator:
    """Validate one operator-configured issuer, audience, and token class.

    JWT jku/x5u/issuer claims never select a network endpoint. Public keys come
    only from the trusted issuer's discovery document. Incoming bearer tokens
    are not retained or forwarded to Harness, providers, tools, or event data.
    """

    def __init__(self, config: A2AConfig, http: aiohttp.ClientSession) -> None:
        self.config = config
        self.http = http
        self.metadata: dict[str, object] = {}
        self._keys: list[dict[str, object]] = []
        self._keys_at = 0.0
        self._miss_at = float("-inf")
        self._key_lock = asyncio.Lock()

    async def _read(self, response: aiohttp.ClientResponse) -> dict[str, object]:
        if response.status != 200:
            raise AuthenticationError("authorization_service_unavailable", 503)
        raw = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            raw.extend(chunk)
            if len(raw) > 262144:
                raise AuthenticationError("authorization_service_unavailable", 503)
        try:
            return mapping(json.loads(raw), "authorization response")
        except (ValueError, UnicodeError):
            raise AuthenticationError("authorization_service_unavailable", 503) from None

    async def document(self, url: str) -> dict[str, object]:
        endpoint(url, loopback_http=self.config.loopback_http)
        try:
            async with self.http.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as response:
                return await self._read(response)
        except (aiohttp.ClientError, TimeoutError):
            raise AuthenticationError("authorization_service_unavailable", 503) from None

    async def initialize(self) -> None:
        issuer = self.config.issuer.issuer
        metadata = await self.document(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        if metadata.get("issuer") != issuer:
            raise ValueError("OIDC discovery issuer does not match the configured issuer")
        for key in ("jwks_uri", "authorization_endpoint", "token_endpoint"):
            value = metadata.get(key)
            if not isinstance(value, str):
                raise ValueError(f"OIDC discovery is missing {key}")
            endpoint(value, loopback_http=self.config.loopback_http)
        self.metadata = metadata
        if self.config.issuer.validation == "jwt":
            await self._refresh_keys()

    def metadata_url(self, key: str) -> str:
        value = self.metadata.get(key)
        if not isinstance(value, str):
            raise AuthenticationError("authorization_service_unavailable", 503)
        return value

    async def _refresh_keys(self) -> None:
        document = await self.document(self.metadata_url("jwks_uri"))
        keys = document.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 128:
            raise AuthenticationError("authorization_service_unavailable", 503)
        try:
            self._keys = [mapping(item, "JWK") for item in keys]
        except ValueError:
            raise AuthenticationError("authorization_service_unavailable", 503) from None
        self._keys_at = time.monotonic()

    async def _key(self, kid: str, algorithm: str) -> jwt.PyJWK:
        async with self._key_lock:
            matches = [item for item in self._keys if item.get("kid") == kid]
            now = time.monotonic()
            if now - self._keys_at > 300 or (not matches and now - self._miss_at > 5):
                if not matches:
                    self._miss_at = now
                await self._refresh_keys()
                matches = [item for item in self._keys if item.get("kid") == kid]
            if len(matches) != 1:
                raise AuthenticationError()
            key = matches[0]
            if key.get("use", "sig") != "sig" or key.get("alg", algorithm) != algorithm:
                raise AuthenticationError()
            operations = key.get("key_ops", ["verify"])
            if not isinstance(operations, list) or "verify" not in operations:
                raise AuthenticationError()
            try:
                return jwt.PyJWK.from_dict(key, algorithm=algorithm)
            except (jwt.PyJWTError, ValueError, TypeError):
                raise AuthenticationError() from None

    async def _introspect(self, token: str) -> dict[str, object]:
        config = self.config.issuer
        secret = os.environ.get(config.client_secret_env, "")
        if not secret:
            raise AuthenticationError("authorization_service_unavailable", 503)
        try:
            credentials = f"{quote_plus(config.client_id)}:{quote_plus(secret)}".encode()
            async with self.http.post(
                config.introspection_endpoint,
                headers={"Authorization": f"Basic {b64encode(credentials).decode('ascii')}"},
                data={"token": token, "token_type_hint": "access_token"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                claims = await self._read(response)
        except (aiohttp.ClientError, TimeoutError):
            raise AuthenticationError("authorization_service_unavailable", 503) from None
        if claims.get("active") is not True:
            raise AuthenticationError()
        if not config.token_kind_claim and str(claims.get("token_type", "")).lower() != "bearer":
            raise AuthenticationError()
        return claims

    def _principal(self, claims: dict[str, object]) -> Principal:
        config = self.config.issuer
        if claims.get("iss") != config.issuer:
            raise AuthenticationError()
        audience = claims.get("aud")
        if audience != config.audience and not (isinstance(audience, list) and config.audience in audience):
            raise AuthenticationError()
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise AuthenticationError()
        expires = claims.get("exp")
        if (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or expires <= time.time()
        ):
            raise AuthenticationError()
        not_before = claims.get("nbf", 0)
        if (
            isinstance(not_before, bool)
            or not isinstance(not_before, (int, float))
            or not math.isfinite(not_before)
            or not_before > time.time() + 30
        ):
            raise AuthenticationError()
        if config.token_kind_claim and claim_value(claims, config.token_kind_claim) != config.token_kind_value:
            raise AuthenticationError()
        raw_scopes = claim_value(claims, config.scope_claim)
        if isinstance(raw_scopes, str):
            scopes = raw_scopes.split()
        elif isinstance(raw_scopes, list) and all(isinstance(item, str) for item in raw_scopes):
            scopes = [str(item) for item in raw_scopes]
        else:
            raise AuthenticationError()
        if len(scopes) > 256 or any(len(scope) > 128 for scope in scopes):
            raise AuthenticationError()
        roles = claim_value(claims, config.role_claim) if config.role_claim else []
        if isinstance(roles, list):
            for role in roles:
                if isinstance(role, str):
                    scopes.extend(config.role_scopes.get(role, ()))
        tenant = claim_value(claims, config.tenant_claim) if config.tenant_claim else ""
        if not isinstance(tenant, str) or len(tenant) > 256 or (config.tenant_claim and not tenant):
            raise AuthenticationError()
        return Principal(config.issuer, subject, tenant, frozenset(scopes), float(expires))

    async def authenticate(self, token: str) -> Principal:
        if not token or len(token) > 16384 or any(ord(character) < 33 for character in token):
            raise AuthenticationError()
        if self.config.issuer.validation == "introspection":
            return self._principal(await self._introspect(token))
        try:
            header = mapping(jwt.get_unverified_header(token), "JWT header")
            algorithm, kid = header.get("alg"), header.get("kid")
            if not isinstance(algorithm, str) or algorithm not in self.config.issuer.algorithms:
                raise AuthenticationError()
            if not isinstance(kid, str) or not 1 <= len(kid) <= 128:
                raise AuthenticationError()
            # RFC9068 access JWTs identify their class. Other issuer formats
            # require an explicit signed access-token claim (e.g. Keycloak's
            # typ=Bearer). A generic JWT/ID token is never accepted by default.
            if not self.config.issuer.token_kind_claim and header.get("typ") != "at+jwt":
                raise AuthenticationError()
            claims = jwt.decode(
                token,
                key=await self._key(kid, algorithm),
                algorithms=[algorithm],
                audience=self.config.issuer.audience,
                issuer=self.config.issuer.issuer,
                leeway=30,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
            return self._principal(mapping(claims, "access token"))
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AuthenticationError() from None
