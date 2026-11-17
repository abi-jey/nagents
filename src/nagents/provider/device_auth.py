"""Public OpenAI device login and a private, application-owned credential store.

Enable device authorization in ChatGPT security/workspace settings first. The
user completes login manually in their browser; this module never opens one,
reads another application's cache, or exchanges subscription tokens for API keys.

The store is plaintext protected by POSIX ownership/mode checks (directory 700,
file 600), not a keyring. Cooperative POSIX processes serialize refresh ownership
with flock and reload credentials under the store lock. Durable attempt markers
prevent replay after an ambiguous failure, cancellation or process death; a new
device login is required in that case. JWT claims are unverified routing/expiry
hints, never proof of login. Only the fixed issuer's TLS token exchange establishes
authentication. No API-key fallback or Codex cache discovery takes place here.

Embeddings should always supply an explicit ``path``. Omitting it retains the
legacy ``$XDG_DATA_HOME/ngn/auth/openai.json`` location for backwards compatibility.
The sibling ``.lock``, ``.refresh.lock`` and ``.state.json`` files are private store
ownership/revision metadata, not credentials, and must not be removed while any
process might use the store. Locks are advisory and require a local filesystem
with working POSIX flock/atomic replace/fsync semantics.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import secrets
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from email.utils import parsedate_to_datetime
from time import monotonic
from time import time
from typing import TYPE_CHECKING
from typing import cast

import aiohttp

from nagents.protected_store import ProtectedFileLock
from nagents.protected_store import ProtectedFileStore
from nagents.protected_store import ProtectedStoreError

from .codex.openai import USER_AGENT
from .openai import CodexCredentials

if TYPE_CHECKING:
    from pathlib import Path

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_LOGIN_SECONDS = 15 * 60
_REQUEST_SECONDS = 30.0
_MAX_BYTES = 256 * 1024
_SIGN_IN = "ChatGPT login is unavailable or invalid; sign in again with /login."

__all__ = ["CLIENT_ID", "ISSUER", "DeviceAuthorization", "OpenAIAuth", "OpenAIAuthError"]


class OpenAIAuthError(ProtectedStoreError):
    """Safe, human-readable errors without server bodies or credentials."""


@dataclass
class DeviceAuthorization:
    verification_url: str
    user_code: str = field(repr=False)
    device_auth_id: str = field(repr=False)
    interval: float
    expires_at: float


@dataclass
class _Tokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    expires_at: float = field(repr=False)


@dataclass(frozen=True)
class _StoreState:
    revision: str = ""
    logged_out: bool = False
    refresh_attempt: str = ""


def _fingerprint(tokens: _Tokens) -> str:
    return hashlib.sha256(json.dumps(asdict(tokens), sort_keys=True, allow_nan=False).encode()).hexdigest()


def _secret(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[\x21-\x7e]{1,65536}", value) is None:
        raise OpenAIAuthError(_SIGN_IN)
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, str | float | int):
        raise OpenAIAuthError(_SIGN_IN)
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise OpenAIAuthError(_SIGN_IN) from None
    if not math.isfinite(number):
        raise OpenAIAuthError(_SIGN_IN)
    return number


def _claims(token: str) -> dict[str, object]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        data: object = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return cast("dict[str, object]", data) if isinstance(data, dict) else {}
    except (ValueError, UnicodeError, RecursionError):
        return {}


def _routing(tokens: _Tokens) -> CodexCredentials:
    account_id = ""
    residency = ""
    for token in (tokens.id_token, tokens.access_token):
        claims = _claims(token)
        nested = claims.get("https://api.openai.com/auth")
        auth = nested if isinstance(nested, dict) else {}
        candidate = auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id")
        if not account_id and isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", candidate):
            account_id = candidate
    # Access-token routing reflects refresh changes even when id_token is omitted.
    for token in (tokens.access_token, tokens.id_token):
        claims = _claims(token)
        nested = claims.get("https://api.openai.com/auth")
        auth = nested if isinstance(nested, dict) else {}
        candidate = auth.get("chatgpt_compute_residency") or claims.get("chatgpt_compute_residency")
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate):
            residency = "" if candidate == "no_constraint" else candidate
            break
    return CodexCredentials(tokens.access_token, account_id, residency)


def _tokens(data: dict[str, object], previous: _Tokens | None = None) -> _Tokens:
    access = _secret(data.get("access_token"))
    refresh = _secret(data.get("refresh_token", previous.refresh_token if previous else None))
    identity = _secret(data.get("id_token", previous.id_token if previous else None))
    lifetime = _number(data.get("expires_in", 3600))
    if not 0 < lifetime <= 365 * 86400:
        raise OpenAIAuthError(_SIGN_IN)
    now = time()
    expires = now + lifetime
    claim_exp = _claims(access).get("exp")
    if claim_exp is not None:
        expires = min(expires, _number(claim_exp))
    if expires <= now:
        raise OpenAIAuthError(_SIGN_IN)
    return _Tokens(access, refresh, identity, expires)


def _retry_after(value: str) -> float:
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(1.0, seconds)
    except ValueError:
        pass
    try:
        return max(1.0, parsedate_to_datetime(value).timestamp() - time())
    except (ValueError, TypeError, OverflowError):
        return 5.0


class OpenAIAuth(ProtectedFileStore):
    """Device OAuth for one private store shared by cooperative POSIX processes.

    ``credentials`` is the explicit asynchronous callback for OpenAIProvider.
    Login/logout supersede older pending login/refresh work across instances and
    processes. Synchronous ``logout`` fails fast if a short store mutation is
    busy; ``logout_async`` waits cancellation-safely instead. Neither waits for
    an in-flight network refresh, and neither can resurrect a removed login.
    """

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(
            path,
            filename="openai.json",
            subject="ChatGPT",
            error=OpenAIAuthError,
            malformed=_SIGN_IN,
        )
        self._lock = asyncio.Lock()
        self._revision = 0
        self._store_lock = ProtectedFileLock(
            self.path.with_name(self.path.name + ".lock"), subject="ChatGPT", error=OpenAIAuthError
        )
        self._refresh_lock = ProtectedFileLock(
            self.path.with_name(self.path.name + ".refresh.lock"), subject="ChatGPT", error=OpenAIAuthError
        )
        self._state_store = ProtectedFileStore(
            self.path.with_name(self.path.name + ".state.json"),
            filename="openai.state.json",
            subject="ChatGPT",
            error=OpenAIAuthError,
            malformed=_SIGN_IN,
        )

    def _state(self) -> _StoreState:
        try:
            with self._state_store._directory() as directory:
                raw = self._state_store._read_bytes(directory)
            if raw is None:
                return _StoreState()
            data: object = json.loads(raw)
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "revision", "logged_out", "refresh_attempt"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or not isinstance(data["revision"], str)
                or re.fullmatch(r"(?:[0-9a-f]{32})?", data["revision"]) is None
                or type(data["logged_out"]) is not bool
                or not isinstance(data["refresh_attempt"], str)
                or re.fullmatch(r"(?:[0-9a-f]{64})?", data["refresh_attempt"]) is None
            ):
                raise OpenAIAuthError(_SIGN_IN)
            return _StoreState(data["revision"], data["logged_out"], data["refresh_attempt"])
        except FileNotFoundError:
            return _StoreState()
        except (ValueError, UnicodeError, RecursionError):
            raise OpenAIAuthError(_SIGN_IN) from None
        except OSError:
            raise OpenAIAuthError(self._unsafe) from None

    def _save_state(self, state: _StoreState) -> None:
        self._state_store._write_bytes(
            json.dumps({"version": 1, **asdict(state)}, allow_nan=False).encode(), prefix=".openai-state-"
        )

    def _load(self) -> _Tokens | None:
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
            if raw is None:
                return None
            data: object = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "version",
                "access_token",
                "refresh_token",
                "id_token",
                "expires_at",
            }:
                raise OpenAIAuthError(_SIGN_IN)
            if type(data["version"]) is not int or data["version"] != 1:
                raise OpenAIAuthError(_SIGN_IN)
            expires = _number(data["expires_at"])
            if expires <= 0:
                raise OpenAIAuthError(_SIGN_IN)
            return _Tokens(
                _secret(data["access_token"]), _secret(data["refresh_token"]), _secret(data["id_token"]), expires
            )
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError, RecursionError):
            raise OpenAIAuthError(_SIGN_IN) from None
        except OSError:
            raise OpenAIAuthError(self._unsafe) from None

    def _save(self, tokens: _Tokens) -> None:
        payload = json.dumps({"version": 1, **asdict(tokens)}, allow_nan=False).encode("utf-8")
        self._write_bytes(payload, prefix=".openai-")

    async def _post(
        self, route: str, payload: dict[str, str], deadline: float, *, form: bool = False
    ) -> tuple[int, dict[str, object], float]:
        if route not in {"/api/accounts/deviceauth/usercode", "/api/accounts/deviceauth/token", "/oauth/token"}:
            raise OpenAIAuthError("Unsupported OpenAI authentication endpoint.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise OpenAIAuthError("ChatGPT login timed out or expired; start /login again.")
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=min(_REQUEST_SECONDS, remaining)),
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                ) as client,
                client.post(
                    ISSUER + route,
                    data=payload if form else None,
                    json=None if form else payload,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    allow_redirects=False,
                ) as response,
            ):
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > _MAX_BYTES:
                        raise OpenAIAuthError("OpenAI returned an oversized authentication response; retry /login.")
                try:
                    data: object = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    data = None
                if not isinstance(data, dict):
                    if response.status == 200:
                        raise OpenAIAuthError("OpenAI returned a malformed authentication response; retry /login.")
                    data = {}
                return (
                    response.status,
                    cast("dict[str, object]", data),
                    _retry_after(response.headers.get("Retry-After", "5")),
                )
        except TimeoutError:
            # Event-loop timers can fire before the next monotonic-clock tick.
            # Classify by the limiting budget, not a second clock comparison.
            if remaining <= _REQUEST_SECONDS:
                raise OpenAIAuthError("ChatGPT login timed out or expired; start /login again.") from None
            raise OpenAIAuthError("OpenAI authentication connection failed or timed out; retry /login.") from None
        except (aiohttp.ClientError, ValueError):
            raise OpenAIAuthError("OpenAI authentication connection failed or timed out; retry /login.") from None

    async def _wait(self, delay: float, deadline: float) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise OpenAIAuthError("ChatGPT login timed out or expired; start /login again.")
        await asyncio.sleep(min(delay, remaining))
        if monotonic() >= deadline:
            raise OpenAIAuthError("ChatGPT login timed out or expired; start /login again.")

    @staticmethod
    def _error_code(data: dict[str, object]) -> object:
        error = data.get("error")
        return error.get("code") if isinstance(error, dict) else error

    async def start_device_login(self) -> DeviceAuthorization:
        deadline = monotonic() + _REQUEST_SECONDS
        while True:
            status, data, delay = await self._post(
                "/api/accounts/deviceauth/usercode", {"client_id": CLIENT_ID}, deadline
            )
            if status == 429 or self._error_code(data) == "slow_down":
                await self._wait(delay, deadline)
                continue
            if status != 200 or self._error_code(data):
                raise OpenAIAuthError(
                    f"Device login could not start (HTTP {status}). Check network/service availability and enable "
                    "device authorization in ChatGPT security/workspace settings, then retry /login."
                )
            code = _secret(data.get("user_code", data.get("usercode")))
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", code):
                raise OpenAIAuthError("OpenAI returned a malformed device code; retry /login.")
            try:
                interval = max(1.0, min(_LOGIN_SECONDS, _number(data.get("interval", 5))))
            except OpenAIAuthError:
                interval = 5.0
            return DeviceAuthorization(
                ISSUER + "/codex/device",
                code,
                _secret(data.get("device_auth_id")),
                interval,
                monotonic() + _LOGIN_SECONDS,
            )

    async def _exchange(self, payload: dict[str, str], deadline: float, previous: _Tokens | None = None) -> _Tokens:
        while True:
            status, data, delay = await self._post("/oauth/token", payload, deadline, form=True)
            code = self._error_code(data)
            if status == 429 or code == "slow_down":
                await self._wait(delay, deadline)
                continue
            if status != 200 or code:
                if code == "invalid_grant" or status in {401, 403}:
                    raise OpenAIAuthError("ChatGPT authorization expired or was revoked; sign in again with /login.")
                raise OpenAIAuthError(
                    "OpenAI token exchange failed; retry /login. Existing saved credentials were not replaced."
                )
            return _tokens(data, previous)

    async def complete_device_login(self, authorization: DeviceAuthorization) -> None:
        deadline = min(_number(authorization.expires_at), monotonic() + _LOGIN_SECONDS)
        interval = max(1.0, min(_LOGIN_SECONDS, _number(authorization.interval)))
        revision = self._revision
        async with self._store_lock.acquire():
            store_revision = self._state().revision
        while True:
            status, data, delay = await self._post(
                "/api/accounts/deviceauth/token",
                {
                    "device_auth_id": _secret(authorization.device_auth_id),
                    "user_code": _secret(authorization.user_code),
                },
                deadline,
            )
            code = self._error_code(data)
            if code in ("access_denied", "authorization_declined"):
                raise OpenAIAuthError("ChatGPT device authorization was denied; start /login again if intended.")
            if code in ("expired_token", "expired_device_code", "invalid_grant"):
                raise OpenAIAuthError("ChatGPT device authorization expired; start /login again.")
            if status == 429 or code == "slow_down":
                interval = min(_LOGIN_SECONDS, max(interval + 5, delay))
            elif status == 200 and not code:
                tokens = await self._exchange(
                    {
                        "grant_type": "authorization_code",
                        "code": _secret(data.get("authorization_code")),
                        "redirect_uri": ISSUER + "/deviceauth/callback",
                        "client_id": CLIENT_ID,
                        "code_verifier": _secret(data.get("code_verifier")),
                    },
                    deadline,
                )
                async with self._lock, self._store_lock.acquire():
                    current = self._state()
                    if revision != self._revision or store_revision != current.revision:
                        raise OpenAIAuthError(
                            "ChatGPT login was cancelled by logout or a newer login; start /login again."
                        )
                    if monotonic() >= deadline:
                        raise OpenAIAuthError("ChatGPT login expired; start /login again.")
                    # Reserve the revision before touching credentials. A crash
                    # between the two atomic files must still supersede older
                    # login/refresh writers, while retaining the prior logout
                    # tombstone/attempt marker until installation succeeds.
                    installed = _StoreState(revision=secrets.token_hex(16))
                    self._save_state(replace(current, revision=installed.revision))
                    self._save(tokens)
                    self._save_state(installed)
                return
            elif status not in {403, 404} and code != "authorization_pending":
                raise OpenAIAuthError("ChatGPT device authorization failed; retry /login.")
            await self._wait(interval, deadline)

    async def credentials(self) -> CodexCredentials:
        async with self._lock, self._refresh_lock.acquire():
            async with self._store_lock.acquire():
                # Reload only after ownership: another process may have rotated it.
                state = self._state()
                tokens = self._load()
                if tokens is None or state.logged_out:
                    raise OpenAIAuthError(_SIGN_IN)
                fingerprint = _fingerprint(tokens)
                if state.refresh_attempt == fingerprint:
                    raise OpenAIAuthError("ChatGPT refresh was interrupted or failed; sign in again with /login.")
                if tokens.expires_at > time() + 60:
                    return _routing(tokens)
                revision = self._revision
                # Persist before any refresh request. Failure/cancellation/crash
                # leaves this marker intact: never replay a possibly spent token.
                self._save_state(replace(state, refresh_attempt=fingerprint))
            refreshed = await self._exchange(
                {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token, "client_id": CLIENT_ID},
                monotonic() + _REQUEST_SECONDS,
                tokens,
            )
            async with self._store_lock.acquire():
                current = self._state()
                if revision != self._revision or current.revision != state.revision or current.logged_out:
                    raise OpenAIAuthError("ChatGPT login was removed during refresh; sign in again with /login.")
                if self._load() != tokens or current.refresh_attempt != fingerprint:
                    raise OpenAIAuthError("Saved ChatGPT login changed during refresh; retry with the current login.")
                self._save(refreshed)
                self._save_state(replace(current, refresh_attempt=""))
                return _routing(refreshed)

    def _present(self) -> bool:
        # Avoid creating store directories for a mere absent-login status check.
        if self._load() is None:
            return False
        with self._store_lock.try_acquire():
            return not self._state().logged_out and self._load() is not None

    def logged_in(self) -> bool:
        try:
            return self._present()
        except OpenAIAuthError:
            return False

    def status(self) -> str:
        try:
            return "ChatGPT device login saved" if self._present() else "Not signed in"
        except OpenAIAuthError:
            return "Saved ChatGPT login is unusable; check private storage permissions and sign in again with /login."

    def logout(self) -> None:
        """Invalidate pending work and remove tokens; fail fast on store contention."""
        with self._store_lock.try_acquire():
            self._logout()

    async def logout_async(self) -> None:
        """Wait for short store ownership without blocking, then log out."""
        async with self._store_lock.acquire():
            self._logout()

    def _logout(self) -> None:
        # Tombstone first, so interruption before unlink still fails closed.
        self._save_state(_StoreState(revision=secrets.token_hex(16), logged_out=True))
        self._revision += 1
        self._unlink()

    async def close(self) -> None:
        """Idempotent, including after cancellation; no persistent client or fds.

        Callers own and must cancel/join their pending authentication tasks.
        Their scoped HTTP clients and flock descriptors close on task exit;
        closing another auth instance never releases somebody else's ownership.
        """
