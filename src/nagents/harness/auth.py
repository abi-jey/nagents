"""OpenAI's beta device login and ngn's private local credential store.

Enable device authorization in ChatGPT security/workspace settings first. The
user completes login manually in their browser; this module never opens one,
reads another application's cache, or exchanges subscription tokens for API keys.

The store is plaintext protected by POSIX ownership/mode checks (directory 700,
file 600), not a keyring. Refresh is serialized per instance, not across processes;
use one OpenAIAuth instance per store and avoid simultaneous ngn processes when
refreshing. JWT claims are unverified routing/expiry hints, never proof of login.
Only the fixed issuer's TLS token exchange establishes authentication.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic
from time import time
from typing import TYPE_CHECKING
from typing import cast

import aiohttp

from nagents.provider.codex import USER_AGENT
from nagents.provider.codex import CodexCredentials

if TYPE_CHECKING:
    from collections.abc import Iterator

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_LOGIN_SECONDS = 15 * 60
_REQUEST_SECONDS = 30.0
_MAX_BYTES = 256 * 1024
_SIGN_IN = "ChatGPT login is unavailable or invalid; sign in again with /login."
_UNSAFE_STORE = "ChatGPT credential storage is unsafe; use an owned private directory (700) and regular file (600)."


class OpenAIAuthError(RuntimeError):
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


class OpenAIAuth:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
            if not data_home.is_absolute():
                raise OpenAIAuthError("XDG_DATA_HOME must be an absolute path for credential storage.")
            path = data_home / "ngn" / "auth" / "openai.json"
        self.path = path.expanduser().absolute()
        self._lock = asyncio.Lock()
        self._revision = 0

    @contextmanager
    def _directory(self, create: bool = False) -> Iterator[int]:
        if os.name != "posix":
            raise OpenAIAuthError("Protected ChatGPT credential storage currently requires POSIX.")
        descriptor = os.open(self.path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.path.parent.parts[1:]:
                if part in {".", ".."}:
                    raise OpenAIAuthError(_UNSAFE_STORE)
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                writable = stat.S_IMODE(info.st_mode) & 0o022
                trusted_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                if info.st_uid not in {0, os.getuid()} or (writable and not trusted_sticky):
                    raise OpenAIAuthError(_UNSAFE_STORE)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OpenAIAuthError(_UNSAFE_STORE)
            yield descriptor
        finally:
            os.close(descriptor)

    def _read(self, directory: int) -> bytes | None:
        try:
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_BYTES
            ):
                raise OpenAIAuthError(_UNSAFE_STORE)
            data = file.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise OpenAIAuthError(_SIGN_IN)
            return data

    def _load(self) -> _Tokens | None:
        try:
            with self._directory() as directory:
                raw = self._read(directory)
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
            raise OpenAIAuthError(_UNSAFE_STORE) from None

    def _save(self, tokens: _Tokens) -> None:
        temporary = f".openai-{secrets.token_hex(12)}.tmp"
        try:
            with self._directory(create=True) as directory:
                self._read(directory)  # Refuse unsafe existing destinations before replacement.
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                        json.dump({"version": 1, **asdict(tokens)}, file, allow_nan=False)
                        file.flush()
                        os.fsync(file.fileno())
                    self._read(directory)
                    os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=directory)
        except OSError:
            raise OpenAIAuthError(
                "ChatGPT login could not be saved securely; check private storage permissions."
            ) from None

    async def _post(
        self, route: str, payload: dict[str, str], deadline: float, *, form: bool = False
    ) -> tuple[int, dict[str, object], float]:
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
        except (aiohttp.ClientError, TimeoutError, ValueError):
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
                async with self._lock:
                    if revision != self._revision:
                        raise OpenAIAuthError("ChatGPT login was cancelled by logout; start /login again.")
                    if monotonic() >= deadline:
                        raise OpenAIAuthError("ChatGPT login expired; start /login again.")
                    self._save(tokens)
                return
            elif status not in {403, 404} and code != "authorization_pending":
                raise OpenAIAuthError("ChatGPT device authorization failed; retry /login.")
            await self._wait(interval, deadline)

    async def credentials(self) -> CodexCredentials:
        async with self._lock:
            tokens = self._load()
            if tokens is None:
                raise OpenAIAuthError(_SIGN_IN)
            if tokens.expires_at <= time() + 60:
                revision = self._revision
                tokens = await self._exchange(
                    {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token, "client_id": CLIENT_ID},
                    monotonic() + _REQUEST_SECONDS,
                    tokens,
                )
                if revision != self._revision:
                    raise OpenAIAuthError("ChatGPT login was removed during refresh; sign in again with /login.")
                self._save(tokens)
            return _routing(tokens)

    def logged_in(self) -> bool:
        try:
            return self._load() is not None
        except OpenAIAuthError:
            return False

    def status(self) -> str:
        try:
            return "ChatGPT device login saved" if self._load() is not None else "Not signed in"
        except OpenAIAuthError:
            return "Saved ChatGPT login is unusable; check private storage permissions and sign in again with /login."

    def logout(self) -> None:
        self._revision += 1
        try:
            with self._directory() as directory:
                if self._read(directory) is not None:
                    os.unlink(self.path.name, dir_fd=directory)
                    os.fsync(directory)
        except FileNotFoundError:
            pass
        except OSError:
            raise OpenAIAuthError(
                "ChatGPT login could not be removed safely; check private storage permissions."
            ) from None

    async def close(self) -> None:
        """HTTP clients are scoped to requests; no persistent client to close."""
