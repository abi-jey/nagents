"""Private outbound credentials bound to one workspace and immutable A2A route."""

from __future__ import annotations

import json
import math
import os
import ssl
import time
from typing import TYPE_CHECKING

from nagents.a2a.errors import RemoteAgentError

from .private_store import ProtectedFileStore
from .private_store import ProtectedStoreError

if TYPE_CHECKING:
    from pathlib import Path

    from .remote_profiles import RemoteProfile


class RemoteCredentialError(RemoteAgentError, ProtectedStoreError):
    """A safe credential preflight failure, before any remote dispatch."""


class RemoteCredentials(ProtectedFileStore):
    def __init__(self, directory: Path, profile: RemoteProfile) -> None:
        self.profile = profile
        super().__init__(
            directory / "remote-auth" / f"{profile.fingerprint}.json",
            filename="remote.json",
            subject="Remote agent",
            error=ProtectedStoreError,
            malformed="Remote agent credentials are unavailable.",
        )

    def save(self, token: str, expires_at: float) -> None:
        if not token or len(token) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ProtectedStoreError("Remote agent returned invalid credentials")
        if not math.isfinite(expires_at) or expires_at <= time.time():
            raise ProtectedStoreError("Remote agent credentials have expired")
        self._write_bytes(json.dumps({"access_token": token, "expires_at": expires_at}).encode(), prefix=".remote-")

    async def token(self) -> str:
        if self.profile.token_env:
            result = os.environ.get(self.profile.token_env, "")
            if not result or len(result) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in result):
                raise RemoteCredentialError(
                    "Remote agent token is missing or invalid in its configured server environment variable",
                    code="A2A_TOKEN_MISSING",
                )
            return result
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
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or isinstance(expires, bool)
                or not isinstance(expires, int | float)
                or not math.isfinite(expires)
                or expires <= time.time() + 10
            ):
                raise ValueError
            return token
        except (ValueError, AttributeError):
            raise RemoteCredentialError(
                "Remote agent sign-in is required. Open its Connection settings.", code="A2A_AUTH_REQUIRED"
            ) from None

    def tls(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.profile.ca_file:
            context.load_verify_locations(cafile=self.profile.ca_file)
        return context
