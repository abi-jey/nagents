"""Lifecycle-owned device authorization, with only public verification data exposed."""

from __future__ import annotations

import asyncio
import math
import re
import secrets
from contextlib import suppress
from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypedDict

from fastapi import HTTPException

from nagents.harness.auth import ISSUER
from nagents.harness.providers import ProviderProfile

from ._async import join_owned

if TYPE_CHECKING:
    from collections.abc import Callable

    from .service import WebState

LoginStatus = Literal["idle", "starting", "pending", "completed", "failed", "cancelled"]


class LoginSnapshot(TypedDict):
    id: str
    status: LoginStatus
    provider: str
    verification_url: str
    user_code: str
    expires_in: int
    message: str


class ProviderLogin:
    def __init__(self, state: WebState, live_active: Callable[[], str]) -> None:
        self.state = state
        self.live_active = live_active
        self.tasks: list[asyncio.Task[None]] = []
        self.ready: list[asyncio.Future[None]] = []
        self.id = ""
        self.status: LoginStatus = "idle"
        self.provider = ""
        self.url = ""
        self.code = ""
        self.expires_at = 0.0
        self.message = ""
        self.closed = False

    def snapshot(self) -> LoginSnapshot:
        return {
            "id": self.id,
            "status": self.status,
            "provider": self.provider,
            "verification_url": self.url,
            "user_code": self.code,
            "expires_in": max(0, math.ceil(self.expires_at - monotonic())) if self.expires_at else 0,
            "message": self.message,
        }

    def clear_code(self) -> None:
        self.url = ""
        self.code = ""
        self.expires_at = 0.0

    async def start(self) -> LoginSnapshot:
        if self.closed:
            raise HTTPException(503, "The server is shutting down.")
        if self.tasks and not self.tasks[-1].done():
            await asyncio.shield(self.ready[-1])
            return self.snapshot()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        ready.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        self.ready[:] = [ready]
        self.tasks[:] = [asyncio.create_task(self.run(ready), name="ngn-web-device-login")]
        # Losing the HTTP response must not strand a provider mutation; GET can
        # recover this one owned flow, and shutdown/cancel always joins it.
        await asyncio.shield(ready)
        return self.snapshot()

    async def run(self, ready: asyncio.Future[None]) -> None:
        try:
            with self.state.idle():
                if self.live_active():
                    raise HTTPException(409, "End the active Live session before signing in.")
                harness = self.state.harness
                if harness.config.demo:
                    raise HTTPException(409, "Device login is unavailable in offline demo mode.")
                store = harness.provider_store
                registry = store.load()
                name = harness.config.provider
                profile = registry.providers.get(name) if name else ProviderProfile(kind="openai", auth="chatgpt")
                if (
                    profile is None
                    or profile.kind not in {"openai", "openai_compatible"}
                    or profile.base_url
                    or profile.api != "auto"
                ):
                    raise HTTPException(
                        409, "Choose a default OpenAI connection without a custom endpoint or API override."
                    )
                if not name:
                    name = "chatgpt"
                    suffix = 1
                    while name in registry.providers:
                        suffix += 1
                        name = f"chatgpt-{suffix}"
                scope = "workspace" if name in store.load_scope("workspace").providers else "global"
                if name not in registry.providers:
                    scope = "workspace"
                revision = store.load_scope(scope).revision
                self.id = secrets.token_urlsafe(24)
                self.provider = name
                self.status = "starting"
                self.message = "Requesting a device code."
                self.clear_code()
                ready.set_result(None)
                authorization = await harness.openai_auth.start_device_login()
                if (
                    authorization.verification_url != ISSUER + "/codex/device"
                    or re.fullmatch(r"[A-Za-z0-9-]{1,64}", authorization.user_code) is None
                    or not math.isfinite(authorization.expires_at)
                    or authorization.expires_at <= monotonic()
                ):
                    raise ValueError("Invalid device authorization")
                self.url = authorization.verification_url
                self.code = authorization.user_code
                self.expires_at = authorization.expires_at
                self.status = "pending"
                self.message = "Approve the device code in your browser."
                # Bound the entire wait even if an auth implementation fails to
                # enforce its own deadline. Tokens stay in the protected store.
                async with asyncio.timeout(min(900.0, authorization.expires_at - monotonic())):
                    await harness.openai_auth.complete_device_login(authorization)
                self.clear_code()

                async def apply() -> None:
                    saved = await harness.save_provider(name, replace(profile, auth="chatgpt"), revision, scope)
                    if not harness.config.provider:
                        await harness.activate_provider(name, saved.revision, scope)
                    self.state.settings.sync_provider()
                    self.status = "completed"
                    self.message = "ChatGPT login saved. The selected connection is ready."

                await join_owned(asyncio.create_task(apply()))
        except asyncio.CancelledError:
            if self.status != "completed":
                self.status = "cancelled"
                self.message = "Device login cancelled."
            if not ready.done():
                ready.set_exception(HTTPException(409, "Device login cancelled."))
            raise
        except HTTPException as error:
            if not ready.done():
                ready.set_exception(error)
            else:
                self.status = "failed"
                self.message = "Login could not be applied. Check the selected connection and try again."
        except Exception:
            self.status = "failed"
            self.message = (
                "Device login failed or expired. Check network access and enable device authorization "
                "in ChatGPT security settings, then try again."
            )
            if not ready.done():
                ready.set_exception(HTTPException(502, self.message))
        finally:
            if self.status != "pending":
                self.clear_code()

    async def cancel(self, identifier: str) -> LoginSnapshot:
        if not identifier or identifier != self.id:
            raise HTTPException(409, "The login attempt changed. Refresh its status.")
        if self.tasks and not self.tasks[-1].done():
            self.tasks[-1].cancel()
            with suppress(asyncio.CancelledError):
                await join_owned(self.tasks[-1])
        return self.snapshot()

    async def close(self) -> None:
        self.closed = True
        if self.tasks and not self.tasks[-1].done():
            self.tasks[-1].cancel()
            with suppress(asyncio.CancelledError):
                await join_owned(self.tasks[-1])
        if self.ready and not self.ready[-1].done():
            self.ready[-1].set_exception(HTTPException(503, "The server is shutting down."))
        self.clear_code()
