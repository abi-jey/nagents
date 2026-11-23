"""Optional first-party OIDC login for the browser workspace."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from dataclasses import field
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from urllib.parse import quote_plus
from urllib.parse import urlencode
from urllib.parse import urlsplit

import aiohttp
from starlette.datastructures import Headers
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse
from starlette.responses import Response

from .security import SECURITY_HEADERS

if TYPE_CHECKING:
    from starlette.types import ASGIApp
    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

CALLBACK = "/oauth2/callback"
LOGOUT = "/oauth2/logout"
SIGNED_OUT = "/oauth2/signed-out"
SESSION_COOKIE = "__Host-ngn-session"
FLOW_COOKIE = "__Host-ngn-login"
DOCUMENTS = {"/", "/dashboard"}
FLOW_SECONDS = 300
SESSION_SECONDS = 3600
HANDLE = re.compile(r"[A-Za-z0-9_-]{43}")


def _https_url(value: str, *, origin_only: bool = False) -> str:
    if not value.isascii() or re.search(r"[\x00-\x20\x7f\\?#]", value):
        raise ValueError("Invalid OIDC HTTPS URL.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or "%" in parsed.netloc:
        raise ValueError("OIDC URLs must use HTTPS without embedded credentials.")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid OIDC HTTPS port.")
    origin = f"https://{parsed.netloc}"
    if origin_only and (
        value != origin or parsed.netloc != parsed.netloc.lower() or parsed.netloc.endswith(":") or parsed.port == 443
    ):
        raise ValueError("OIDC public origin must be a canonical HTTPS origin without a path or trailing slash.")
    return origin


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    public_origin: str
    client_id: str
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        _https_url(self.issuer)
        _https_url(self.public_origin, origin_only=True)
        if not self.client_id.strip() or not self.client_secret.strip():
            raise ValueError("OIDC requires a client ID and client secret.")

    @classmethod
    def from_env(cls) -> "OidcConfig | None":
        names = ("ISSUER", "PUBLIC_ORIGIN", "CLIENT_ID", "CLIENT_SECRET")
        values = [os.environ.get(f"NGN_WEB_OIDC_{name}") for name in names]
        if all(value is None for value in values):
            return None
        if not all(values):
            raise ValueError("Set all four NGN_WEB_OIDC_{ISSUER,PUBLIC_ORIGIN,CLIENT_ID,CLIENT_SECRET} variables.")
        return cls(*[value for value in values if value is not None])


class LoginError(Exception):
    """The provider response could not authenticate this login."""


@dataclass(frozen=True)
class Provider:
    authorize: str
    token: str
    jwks: str
    auth_method: str


async def _json(
    http: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        async with http.request(method, url, data=data, headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                raise LoginError
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > 262144:
                    raise LoginError
            value = json.loads(raw)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise LoginError from None
    if not isinstance(value, dict):
        raise LoginError
    return value


class OidcClient:
    def __init__(self, config: OidcConfig) -> None:
        try:
            import jwt
        except ModuleNotFoundError:
            raise ValueError("OIDC requires PyJWT with its crypto extra; install nagents[web].") from None
        if not jwt.algorithms.has_crypto:
            raise ValueError("OIDC requires PyJWT with its crypto extra; install nagents[web].")
        self.config = config

    @property
    def redirect_uri(self) -> str:
        return self.config.public_origin + CALLBACK

    async def discover(self) -> Provider:
        url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            metadata = await _json(http, "GET", url)
        if metadata.get("issuer") != self.config.issuer:
            raise LoginError
        endpoints: list[str] = []
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = metadata.get(name)
            if not isinstance(value, str):
                raise LoginError
            try:
                _https_url(value)
            except ValueError:
                raise LoginError from None
            endpoints.append(value)
        for name, required in (
            ("response_types_supported", "code"),
            ("code_challenge_methods_supported", "S256"),
            ("id_token_signing_alg_values_supported", "RS256"),
        ):
            offered = metadata.get(name, [required])
            if not isinstance(offered, list) or required not in offered:
                raise LoginError
        methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        if not isinstance(methods, list):
            raise LoginError
        auth_method = next((item for item in ("client_secret_basic", "client_secret_post") if item in methods), "")
        if not auth_method:
            raise LoginError
        return Provider(endpoints[0], endpoints[1], endpoints[2], auth_method)

    async def redeem(self, provider: Provider, code: str, verifier: str, nonce: str) -> None:
        import jwt

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        headers = None
        if provider.auth_method == "client_secret_basic":
            headers = {
                "Authorization": aiohttp.encode_basic_auth(
                    quote_plus(self.config.client_id), quote_plus(self.config.client_secret)
                )
            }
        else:
            data.update(client_id=self.config.client_id, client_secret=self.config.client_secret)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            result = await _json(http, "POST", provider.token, data=data, headers=headers)
            token = result.get("id_token")
            if not isinstance(token, str) or len(token) > 16384:
                raise LoginError
            keys = (await _json(http, "GET", provider.jwks)).get("keys")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or (kid is not None and not isinstance(kid, str)):
                raise LoginError
            if not isinstance(keys, list) or not 1 <= len(keys) <= 128:
                raise LoginError
            candidates = [
                key
                for key in keys
                if isinstance(key, dict)
                and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
                and (kid is None or key.get("kid") == kid)
                and (isinstance(ops := key.get("key_ops", ["verify"]), list) and "verify" in ops)
            ]
            if len(candidates) != 1:
                raise LoginError
            key = jwt.PyJWK.from_dict(candidates[0], algorithm="RS256").key
            if key.key_size < 2048:
                raise LoginError
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.config.client_id,
                issuer=self.config.issuer,
                options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
            )
            if not isinstance(claims["sub"], str) or not claims["sub"] or claims["nonce"] != nonce:
                raise LoginError
            audience = claims["aud"]
            azp = claims.get("azp")
            if (azp is not None or (isinstance(audience, list) and len(audience) > 1)) and azp != self.config.client_id:
                raise LoginError
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise LoginError from None


@dataclass(frozen=True)
class Login:
    state: str
    nonce: str
    verifier: str
    provider: Provider
    destination: str
    expires: float


@dataclass
class Session:
    expires: float
    ended: asyncio.Event = field(default_factory=asyncio.Event)


class OidcLogin:
    """Gate all workspace transports before the existing same-origin boundary."""

    def __init__(self, app: "ASGIApp", *, client: OidcClient) -> None:
        self.app = app
        self.client = client
        self.origin = client.config.public_origin
        self.authority = urlsplit(self.origin).netloc
        self.logins: dict[str, Login] = {}
        self.sessions: dict[str, Session] = {}
        self.discovery: tuple[Provider | None, float] | None = None
        self.discovery_lock = asyncio.Lock()
        self.exchanges = asyncio.Semaphore(16)
        self.audit_windows: dict[str, tuple[float, int]] = {}

    def _audit(self, event: str, *, sampled: bool = False) -> None:
        # Only internal outcome names reach logs. A flood of unauthenticated
        # requests must not produce a log entry for every packet.
        suppressed = 0
        if sampled:
            now = monotonic()
            until, suppressed = self.audit_windows.get(event, (0.0, 0))
            if now < until:
                self.audit_windows[event] = (until, suppressed + 1)
                return
            self.audit_windows[event] = (now + 30, 0)
        logging.getLogger("uvicorn.error").info("web_auth event=%s suppressed=%d", event, suppressed)

    async def _provider(self) -> Provider:
        async with self.discovery_lock:
            now = monotonic()
            if self.discovery is None or self.discovery[1] <= now:
                try:
                    self.discovery = (await self.client.discover(), monotonic() + FLOW_SECONDS)
                except LoginError:
                    self.discovery = (None, monotonic() + 15)
            provider = self.discovery[0]
            if provider is None:
                raise LoginError
            return provider

    def _prune(self) -> None:
        now = monotonic()
        self.logins = {handle: flow for handle, flow in self.logins.items() if flow.expires > now}
        for handle, session in list(self.sessions.items()):
            if session.expires <= now:
                self.sessions.pop(handle).ended.set()
                self._audit("session_expired")

    async def _start(self, destination: str) -> Response:
        try:
            provider = await self._provider()
        except LoginError:
            return JSONResponse({"detail": "Sign-in is temporarily unavailable."}, status_code=503)
        handle = secrets.token_urlsafe(32)
        flow = Login(
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
            provider,
            destination,
            monotonic() + FLOW_SECONDS,
        )
        if len(self.logins) >= 512:
            del self.logins[next(iter(self.logins))]
        self.logins[handle] = flow
        challenge = urlsafe_b64encode(hashlib.sha256(flow.verifier.encode("ascii")).digest()).decode().rstrip("=")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client.config.client_id,
                "scope": "openid",
                "redirect_uri": self.client.redirect_uri,
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(f"{provider.authorize}?{query}", status_code=303)
        response.set_cookie(FLOW_COOKIE, handle, max_age=FLOW_SECONDS, secure=True, httponly=True, samesite="lax")
        return response

    async def _callback(self, scope: "Scope", handle: str) -> Response:
        raw = scope["query_string"]
        scope["query_string"] = b""
        rejected = HTMLResponse(
            '<!doctype html><title>Sign-in denied</title><p>Sign-in denied.</p><a href="/">Try again</a>',
            status_code=403,
        )
        if len(raw) > 8192:
            return rejected
        try:
            pairs = parse_qsl(
                raw.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=8, errors="strict"
            )
        except (ValueError, UnicodeError):
            return rejected
        query = dict(pairs)
        flow = self.logins.get(handle)
        if (
            flow is None
            or len(query) != len(pairs)
            or not HANDLE.fullmatch(query.get("state", ""))
            or not secrets.compare_digest(query["state"], flow.state)
        ):
            return rejected
        del self.logins[handle]
        rejected.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
        code = query.get("code")
        if "error" in query or not code or query.get("iss", self.client.config.issuer) != self.client.config.issuer:
            return rejected
        if self.exchanges.locked():
            return rejected
        try:
            async with self.exchanges:
                await self.client.redeem(flow.provider, code, flow.verifier, flow.nonce)
        except LoginError:
            return rejected
        if len(self.sessions) >= 128:
            self.sessions.pop(next(iter(self.sessions))).ended.set()
        identifier = secrets.token_urlsafe(32)
        self.sessions[identifier] = Session(monotonic() + SESSION_SECONDS)
        response = RedirectResponse(flow.destination, status_code=303)
        response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
        response.set_cookie(
            SESSION_COOKIE, identifier, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="lax"
        )
        return response

    async def _socket(self, session: Session, scope: "Scope", receive: "Receive", send: "Send") -> None:
        async def run() -> None:
            await self.app(scope, receive, send)

        socket = asyncio.create_task(run())
        ended = asyncio.create_task(session.ended.wait())
        try:
            done, _ = await asyncio.wait(
                (socket, ended),
                timeout=max(0, session.expires - monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if socket in done:
                await socket
            else:
                socket.cancel()
                await asyncio.gather(socket, return_exceptions=True)
                await send({"type": "websocket.close", "code": 1008})
        finally:
            socket.cancel()
            ended.cancel()
            await asyncio.gather(socket, ended, return_exceptions=True)

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = Headers(scope=scope)
        host_ok = headers.getlist("host") == [self.authority]
        self._prune()
        cookies = HTTPConnection(scope).cookies
        identifier = cookies.get(SESSION_COOKIE, "")
        session = self.sessions.get(identifier) if HANDLE.fullmatch(identifier) else None
        if kind == "websocket":
            if not host_ok or session is None:
                self._audit("websocket_denied", sampled=True)
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self._socket(session, scope, receive, send)
            return

        method = scope["method"]
        if path == "/healthz" and method == "GET" and not scope["query_string"]:
            response: Response = Response("ok", media_type="text/plain")
        elif not host_ok:
            response = JSONResponse({"detail": "Untrusted Host."}, status_code=403)
        elif path == CALLBACK and method == "GET":
            response = await self._callback(scope, cookies.get(FLOW_COOKIE, ""))
            if response.status_code == 303:
                self._audit("login_succeeded")
            else:
                self._audit("login_denied", sampled=True)
        elif path == SIGNED_OUT and method == "GET" and not scope["query_string"]:
            response = HTMLResponse(
                '<!doctype html><title>Signed out</title><p>Signed out of NGN.</p><a href="/">Sign in</a>'
            )
        elif session is None:
            if (
                path in DOCUMENTS
                and method == "GET"
                and not scope["query_string"]
                and headers.get("sec-fetch-dest") in {None, "document"}
            ):
                response = await self._start(path)
                if response.status_code == 503:
                    self._audit("issuer_unavailable", sampled=True)
            else:
                self._audit("http_unauthenticated", sampled=True)
                response = JSONResponse({"detail": "Sign in to NGN."}, status_code=401)
        elif path == LOGOUT and method == "POST" and not scope["query_string"]:
            if headers.getlist("origin") != [self.origin]:
                response = JSONResponse({"detail": "An exact same-origin Origin header is required."}, status_code=403)
            else:
                self.sessions.pop(identifier).ended.set()
                self._audit("logout")
                response = Response(status_code=204)
                response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
                response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
        else:

            async def secure_send(message: "Message") -> None:
                if message["type"] == "http.response.start":
                    MutableHeaders(scope=message)["Strict-Transport-Security"] = "max-age=31536000"
                await send(message)

            await self.app(scope, receive, secure_send)
            return
        response.headers.update(SECURITY_HEADERS)
        if host_ok:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        await response(scope, receive, send)
