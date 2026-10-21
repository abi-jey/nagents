"""Private user credentials bound to an immutable A2A connection across workspaces."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import ssl
import stat
import time
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypedDict

from nagents.a2a.errors import RemoteAgentError
from nagents.a2a.remote_config import remote_endpoint

from .private_store import ProtectedFileStore
from .private_store import ProtectedStoreError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .remote_profiles import RemoteProfile


class RemoteAuthenticationStatus(TypedDict):
    state: Literal[
        "signed_in",
        "sign_in_required",
        "token_ready",
        "token_missing",
        "not_required",
        "storage_error",
        "host_setup_required",
    ]
    expires_at: float
    error_code: str
    refreshable: bool


@dataclass(frozen=True)
class _SavedGrant:
    access_token: str
    expires_at: float
    refresh_token: str
    token_endpoint: str
    refresh_pending: bool


class RemoteCredentialError(RemoteAgentError, ProtectedStoreError):
    """A safe credential preflight failure, before any remote dispatch."""


class RemoteCredentials(ProtectedFileStore):
    def __init__(self, profile: RemoteProfile, path: Path | None = None) -> None:
        self.profile = profile
        super().__init__(
            path,
            filename=f"a2a/{profile.fingerprint}.json",
            subject="Remote agent",
            error=ProtectedStoreError,
            malformed="Remote agent credentials are unavailable.",
        )

    def save(
        self,
        token: str,
        expires_at: float,
        *,
        refresh_token: str = "",
        token_endpoint: str = "",
    ) -> None:
        if not token or len(token) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ProtectedStoreError("Remote agent returned invalid credentials")
        if not math.isfinite(expires_at) or expires_at <= time.time():
            raise ProtectedStoreError("Remote agent credentials have expired")
        if bool(refresh_token) != bool(token_endpoint):
            raise ProtectedStoreError("Remote agent refresh credentials are incomplete")
        if refresh_token:
            if not self._valid_token(refresh_token) or len(token_endpoint) > 2048:
                raise ProtectedStoreError("Remote agent returned invalid refresh credentials")
            try:
                remote_endpoint(token_endpoint, loopback_http=self.profile.loopback_http)
            except ValueError:
                raise ProtectedStoreError("Remote agent returned invalid refresh credentials") from None
        self._persist_grant(token, expires_at, refresh_token, token_endpoint, refresh_pending=False)

    async def save_login(
        self,
        token: str,
        expires_at: float,
        *,
        refresh_token: str = "",
        token_endpoint: str = "",
    ) -> None:
        """Commit a fresh user login after any refresh already holding the grant lock."""
        async with self._refresh_lock():
            self.save(token, expires_at, refresh_token=refresh_token, token_endpoint=token_endpoint)

    async def token(self) -> str:
        if self.profile.token_env:
            result = os.environ.get(self.profile.token_env, "")
            if not result or len(result) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in result):
                raise RemoteCredentialError(
                    "Remote agent token is missing or invalid in its configured server environment variable",
                    code="A2A_TOKEN_MISSING",
                )
            return result
        grant = self._saved_grant()
        if grant.expires_at > time.time() + 10 and not grant.refresh_pending:
            return grant.access_token
        if not grant.refresh_token or not grant.token_endpoint:
            if grant.refresh_pending:
                raise self._refresh_uncertain()
            raise self._sign_in_required()
        return await self._refresh()

    async def status(self) -> RemoteAuthenticationStatus:
        """Return fixed, secret-free auth state without contacting the remote host."""
        if self.profile.token_env:
            token = os.environ.get(self.profile.token_env, "")
            ready = bool(token) and len(token) <= 16384 and all(33 <= ord(char) <= 126 for char in token)
            return {
                "state": "token_ready" if ready else "token_missing",
                "expires_at": 0.0,
                "error_code": "" if ready else "A2A_TOKEN_MISSING",
                "refreshable": False,
            }
        if not self.profile.issuer:
            return {"state": "not_required", "expires_at": 0.0, "error_code": "", "refreshable": False}
        if not self.profile.client_id:
            return {
                "state": "host_setup_required",
                "expires_at": 0.0,
                "error_code": "A2A_HOST_SETUP",
                "refreshable": False,
            }
        try:
            grant = self._saved_grant()
        except RemoteCredentialError as error:
            if error.code == "A2A_AUTH_STORAGE":
                return {
                    "state": "storage_error",
                    "expires_at": 0.0,
                    "error_code": error.code,
                    "refreshable": False,
                }
            return {
                "state": "sign_in_required",
                "expires_at": 0.0,
                "error_code": "A2A_AUTH_REQUIRED",
                "refreshable": False,
            }
        refreshable = bool(grant.refresh_token and grant.token_endpoint and not grant.refresh_pending)
        if grant.refresh_pending:
            try:
                active, latest = self._refresh_status()
            except RemoteCredentialError as error:
                if error.code == "A2A_AUTH_STORAGE":
                    return {
                        "state": "storage_error",
                        "expires_at": 0.0,
                        "error_code": error.code,
                        "refreshable": False,
                    }
                raise
            if active:
                return {
                    "state": "signed_in",
                    "expires_at": grant.expires_at,
                    "error_code": "",
                    "refreshable": True,
                }
            if latest is None or latest.refresh_pending:
                return {
                    "state": "sign_in_required",
                    "expires_at": grant.expires_at,
                    "error_code": "A2A_AUTH_REFRESH_UNCERTAIN",
                    "refreshable": False,
                }
            grant = latest
            refreshable = bool(grant.refresh_token and grant.token_endpoint)
        if grant.expires_at <= time.time() + 10 and not refreshable:
            return {
                "state": "sign_in_required",
                "expires_at": grant.expires_at,
                "error_code": "A2A_AUTH_REQUIRED",
                "refreshable": False,
            }
        return {
            "state": "signed_in",
            "expires_at": grant.expires_at,
            "error_code": "",
            "refreshable": refreshable,
        }

    def _refresh_status(self) -> tuple[bool, _SavedGrant | None]:
        """Check the existing lock without waiting or creating files, then reread safely."""
        try:
            import fcntl
        except ImportError:
            raise RemoteCredentialError(
                "Remote credential refresh requires protected POSIX storage", code="A2A_AUTH_STORAGE"
            ) from None
        descriptor = -1
        try:
            with self._directory() as directory:
                descriptor = os.open(
                    f".{self.profile.fingerprint}.refresh.lock",
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
        except FileNotFoundError:
            return False, None
        except (OSError, ProtectedStoreError):
            raise RemoteCredentialError(
                "Remote agent credentials could not be loaded safely", code="A2A_AUTH_STORAGE"
            ) from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ProtectedStoreError("Remote agent credential storage is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True, None
            try:
                try:
                    latest = self._saved_grant()
                except RemoteCredentialError as error:
                    if error.code == "A2A_AUTH_STORAGE":
                        raise
                    return False, None
                return False, latest
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (OSError, ProtectedStoreError):
            raise RemoteCredentialError(
                "Remote agent credentials could not be loaded safely", code="A2A_AUTH_STORAGE"
            ) from None
        finally:
            os.close(descriptor)

    def _saved_grant(self) -> _SavedGrant:
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
        except FileNotFoundError:
            raise RemoteCredentialError(
                "Remote agent sign-in is required. Open its Connection settings.", code="A2A_AUTH_REQUIRED"
            ) from None
        except (OSError, ProtectedStoreError):
            raise RemoteCredentialError(
                "Remote agent credentials could not be loaded safely", code="A2A_AUTH_STORAGE"
            ) from None
        try:
            data = json.loads(raw or b"{}")
            token, expires = data.get("access_token"), data.get("expires_at")
            refresh_token = data.get("refresh_token", "")
            token_endpoint = data.get("token_endpoint", "")
            refresh_pending = data.get("refresh_pending", False)
            if (
                not self._valid_token(token)
                or isinstance(expires, bool)
                or not isinstance(expires, int | float)
                or not math.isfinite(expires)
                or type(refresh_pending) is not bool
                or not isinstance(refresh_token, str)
                or not isinstance(token_endpoint, str)
                or len(token_endpoint) > 2048
                or bool(refresh_token) != bool(token_endpoint)
                or (refresh_token and not self._valid_token(refresh_token))
                or (refresh_pending and not refresh_token)
            ):
                raise ValueError
            if token_endpoint:
                remote_endpoint(token_endpoint, loopback_http=self.profile.loopback_http)
            return _SavedGrant(token, float(expires), refresh_token, token_endpoint, refresh_pending)
        except (ValueError, AttributeError):
            raise RemoteCredentialError(
                "Remote agent sign-in is required. Open its Connection settings.", code="A2A_AUTH_REQUIRED"
            ) from None

    @staticmethod
    def _valid_token(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= 16384
            and all(33 <= ord(char) <= 126 for char in value)
        )

    @staticmethod
    def _sign_in_required() -> RemoteCredentialError:
        return RemoteCredentialError(
            "Remote agent sign-in is required. Open its Connection settings.", code="A2A_AUTH_REQUIRED"
        )

    @staticmethod
    def _refresh_uncertain() -> RemoteCredentialError:
        return RemoteCredentialError(
            "Remote credentials could not be safely refreshed. Sign in again; the previous refresh token will not be reused.",
            code="A2A_AUTH_REFRESH_UNCERTAIN",
        )

    def _persist_grant(
        self,
        access_token: str,
        expires_at: float,
        refresh_token: str,
        token_endpoint: str,
        *,
        refresh_pending: bool,
    ) -> None:
        value: dict[str, str | float | bool] = {"access_token": access_token, "expires_at": expires_at}
        if refresh_token and token_endpoint:
            value.update(refresh_token=refresh_token, token_endpoint=token_endpoint)
            if refresh_pending:
                value["refresh_pending"] = True
        self._write_bytes(json.dumps(value).encode(), prefix=".remote-")

    @asynccontextmanager
    async def _refresh_lock(self) -> AsyncIterator[None]:
        try:
            import fcntl
        except ImportError:
            raise RemoteCredentialError(
                "Remote credential refresh requires protected POSIX storage", code="A2A_AUTH_STORAGE"
            ) from None
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            descriptor = -1
            phase = "directory"
            try:
                with self._directory(create=True) as directory:
                    phase = "open_lock"
                    name = f".{self.profile.fingerprint}.refresh.lock"
                    # Avoid macOS openat's create-or-open race on the first
                    # concurrent refresh. Never unlink or replace this lock.
                    try:
                        descriptor = os.open(
                            name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                        )
                    except FileExistsError:
                        descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
                phase = "validate_lock"
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    raise ProtectedStoreError("Remote agent credential storage is unsafe")
                phase = "acquire_lock"
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
                    descriptor = -1
            except (OSError, ProtectedStoreError) as error:
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
                logger.warning(
                    "Remote credential lock failed phase=%s error_type=%s errno=%s",
                    phase,
                    type(error).__name__,
                    error.errno if isinstance(error, OSError) else "unavailable",
                )
                raise RemoteCredentialError(
                    "Remote agent credentials could not be loaded safely", code="A2A_AUTH_STORAGE"
                ) from None
            if asyncio.get_running_loop().time() >= deadline:
                raise self._refresh_uncertain()
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    async def _refresh(self) -> str:
        import httpx

        async with self._refresh_lock():
            current = self._saved_grant()
            if current.refresh_pending:
                raise self._refresh_uncertain()
            if current.expires_at > time.time() + 10:
                return current.access_token
            if not current.refresh_token or not current.token_endpoint:
                raise self._sign_in_required()
            try:
                endpoint = remote_endpoint(current.token_endpoint, loopback_http=self.profile.loopback_http)
            except ValueError:
                raise self._refresh_uncertain() from None
            try:
                self._persist_grant(
                    current.access_token,
                    current.expires_at,
                    current.refresh_token,
                    current.token_endpoint,
                    refresh_pending=True,
                )
            except ProtectedStoreError:
                raise RemoteCredentialError(
                    "Remote credentials could not be safely stored before refresh", code="A2A_AUTH_STORAGE"
                ) from None
            try:
                async with httpx.AsyncClient(
                    verify=self.tls(), trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10)
                ) as client:
                    response = await client.post(
                        endpoint,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": current.refresh_token,
                            "client_id": self.profile.client_id,
                            **({"resource": self.profile.resource} if self.profile.resource else {}),
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise self._refresh_uncertain() from None
            if response.status_code in {400, 401, 403}:
                try:
                    self._persist_grant(
                        current.access_token,
                        current.expires_at,
                        "",
                        "",
                        refresh_pending=False,
                    )
                except ProtectedStoreError:
                    raise self._refresh_uncertain() from None
                raise self._sign_in_required()
            if response.status_code != 200 or len(response.content) > 131072:
                raise self._refresh_uncertain()
            try:
                tokens = response.json()
                if not isinstance(tokens, dict):
                    raise ValueError
                access_token = tokens.get("access_token")
                rotated_token = tokens.get("refresh_token")
                expires = tokens.get("expires_in")
                granted_scopes = tokens.get("scope")
                if isinstance(expires, bool) or not isinstance(expires, int | float):
                    raise ValueError
                expires_value = float(expires)
                if (
                    not isinstance(access_token, str)
                    or not self._valid_token(access_token)
                    or not isinstance(rotated_token, str)
                    or not self._valid_token(rotated_token)
                    or rotated_token == current.refresh_token
                    or not math.isfinite(expires_value)
                    or not 1 <= expires_value <= 86400
                    or str(tokens.get("token_type", "")).lower() != "bearer"
                    or (
                        granted_scopes is not None
                        and (
                            not isinstance(granted_scopes, str)
                            or not set(self.profile.scopes) <= set(granted_scopes.split())
                        )
                    )
                ):
                    raise ValueError
            except (ValueError, AttributeError, TypeError):
                raise self._refresh_uncertain() from None
            try:
                self.save(
                    access_token,
                    time.time() + expires_value,
                    refresh_token=rotated_token,
                    token_endpoint=endpoint,
                )
            except ProtectedStoreError:
                try:
                    saved = self._saved_grant()
                except RemoteCredentialError:
                    raise self._refresh_uncertain() from None
                if (
                    saved.refresh_pending
                    or saved.access_token != access_token
                    or saved.refresh_token != rotated_token
                    or saved.token_endpoint != endpoint
                ):
                    raise self._refresh_uncertain() from None
                return saved.access_token
            return access_token

    def tls(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.profile.ca_file:
            context.load_verify_locations(cafile=self.profile.ca_file)
        return context
