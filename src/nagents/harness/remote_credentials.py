"""Private outbound credentials bound to one workspace and immutable A2A route."""

from __future__ import annotations

import json
import math
import os
import ssl
import time
from typing import TYPE_CHECKING

from .private_store import ProtectedFileStore
from .private_store import ProtectedStoreError

if TYPE_CHECKING:
    from pathlib import Path

    from .remote_profiles import RemoteProfile


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
            if not result:
                raise ProtectedStoreError(
                    "Remote agent token is missing from its configured server environment variable"
                )
            return result
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
            data = json.loads(raw or b"{}")
            token, expires = data.get("access_token"), data.get("expires_at")
            if (
                not isinstance(token, str)
                or isinstance(expires, bool)
                or not isinstance(expires, int | float)
                or not math.isfinite(expires)
                or expires <= time.time() + 10
            ):
                raise ValueError
            return token
        except (OSError, ValueError, AttributeError, ProtectedStoreError):
            raise ProtectedStoreError("Remote agent sign-in is required. Open its Connection settings.") from None

    def tls(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.profile.ca_file:
            context.load_verify_locations(cafile=self.profile.ca_file)
        return context
