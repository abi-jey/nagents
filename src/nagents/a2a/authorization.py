"""Short-lived, task-bound out-of-band action authorization.

The bearer that invoked a task never grants tool approval. A pending decision is
bound to the authenticated owner, task and exact operation, and is consumed
once. Only a live executor can use a decision; restart never resumes actions.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import replace
from typing import TYPE_CHECKING

from a2a.types import TaskState

from .auth import AuthenticationError
from .auth import Principal
from .store import TERMINAL

if TYPE_CHECKING:
    from nagents.harness.types import ApprovalRequest
    from nagents.types import ToolArguments

    from .config import A2AConfig
    from .oauth import AccessGrant
    from .store import SQLiteTaskStore

StatusReporter = Callable[[TaskState, str], Awaitable[None]]
InputHandler = Callable[[str], Awaitable[str]]
CredentialHandler = Callable[["AuthorizationSession", str], Awaitable["AccessGrant"]]


class AuthorizationUnavailable(Exception):
    """A safe result for a missing, stale, canceled or foreign transaction."""


class AuthorizationDenied(Exception):
    """The current pending operation was denied; earlier work is unaffected."""


class AuthorizationExpired(Exception):
    """The current pending operation expired without executing it."""


async def unavailable_input(question: str) -> str:
    del question
    raise AuthorizationUnavailable()


async def unavailable_credentials(session: AuthorizationSession, service: str) -> AccessGrant:
    del session, service
    raise AuthorizationUnavailable()


@dataclass(frozen=True)
class AuthorizationRecord:
    id: str
    task_id: str
    owner: str
    tenant: str
    kind: str
    digest: str
    details: dict[str, object]
    expires_at: float
    state: str
    nonce: str


@dataclass
class PendingDecision:
    future: asyncio.Future[bool]
    record: AuthorizationRecord


class AuthorizationRegistry:
    def __init__(self, config: A2AConfig, store: SQLiteTaskStore) -> None:
        self.config = config
        self.store = store
        self._pending: dict[str, PendingDecision] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.store.connection() as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS authorizations (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), "
                "owner TEXT NOT NULL, tenant TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, details TEXT NOT NULL, "
                "expires REAL NOT NULL, state TEXT NOT NULL, nonce TEXT NOT NULL)"
            )
            # A persisted approval is not an execution checkpoint. Pending
            # Futures and tool handles disappear on restart, never replay them.
            await db.execute("UPDATE authorizations SET state='interrupted' WHERE state='pending'")
            await db.commit()

    async def create(self, task_id: str, user: Principal, kind: str, details: dict[str, object]) -> AuthorizationRecord:
        if kind not in {"action", "oauth"}:
            raise ValueError("Unknown authorization kind")
        raw = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > self.config.max_request_bytes:
            raise ValueError("Authorization request exceeds the configured limit")
        record = AuthorizationRecord(
            secrets.token_urlsafe(24),
            task_id,
            user.user_name,
            user.tenant,
            kind,
            hashlib.sha256(raw.encode()).hexdigest(),
            copy.deepcopy(details),
            time.time() + self.config.approval_timeout,
            "pending",
            secrets.token_urlsafe(24),
        )
        async with self._lock:
            async with self.store.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                task = await (await db.execute("SELECT owner,state FROM tasks WHERE id=?", (task_id,))).fetchone()
                if task is None or str(task[0]) != user.user_name or int(task[1]) in TERMINAL:
                    raise AuthorizationUnavailable()
                count = await (
                    await db.execute("SELECT COUNT(*) FROM authorizations WHERE task_id=?", (task_id,))
                ).fetchone()
                if count is None or int(count[0]) >= 32:
                    raise AuthorizationUnavailable()
                if any(p.record.task_id == task_id for p in self._pending.values()):
                    raise AuthorizationUnavailable()
                await db.execute(
                    "INSERT INTO authorizations VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.id,
                        task_id,
                        record.owner,
                        record.tenant,
                        kind,
                        record.digest,
                        raw,
                        record.expires_at,
                        record.state,
                        record.nonce,
                    ),
                )
                await db.commit()
            self._pending[record.id] = PendingDecision(asyncio.get_running_loop().create_future(), record)
        return replace(record, details=copy.deepcopy(record.details))

    @staticmethod
    def _authorize(record: AuthorizationRecord, user: Principal) -> None:
        user.require("ngn:access")
        user.require("ngn:approve")
        if record.owner != user.user_name and not ("ngn:approve:any" in user.scopes and record.tenant == user.tenant):
            # An operator may approve machine work only under a separately
            # configured IdP scope, within the same authenticated tenant.
            raise AuthorizationUnavailable()

    async def inspect(self, identifier: str, user: Principal) -> AuthorizationRecord:
        async with self._lock:
            pending = self._pending.get(identifier)
            if pending is None or pending.record.expires_at <= time.time() or pending.future.done():
                raise AuthorizationUnavailable()
            self._authorize(pending.record, user)
            return replace(pending.record, details=copy.deepcopy(pending.record.details))

    async def decide(self, identifier: str, user: Principal, nonce: str, approved: bool, digest: str) -> None:
        if type(approved) is not bool:
            raise AuthorizationUnavailable()
        async with self._lock:
            pending = self._pending.get(identifier)
            if pending is None or pending.future.done():
                raise AuthorizationUnavailable()
            record = pending.record
            self._authorize(record, user)
            if (
                record.expires_at <= time.time()
                or not hmac.compare_digest(record.nonce, nonce)
                or not hmac.compare_digest(record.digest, digest)
            ):
                raise AuthorizationUnavailable()
            async with self.store.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                task = await (await db.execute("SELECT state FROM tasks WHERE id=?", (record.task_id,))).fetchone()
                if task is None or int(task[0]) in TERMINAL:
                    raise AuthorizationUnavailable()
                cursor = await db.execute(
                    "UPDATE authorizations SET state=? WHERE id=? AND state='pending' AND expires>? AND digest=?",
                    ("approved" if approved else "denied", identifier, time.time(), digest),
                )
                if cursor.rowcount != 1:
                    raise AuthorizationUnavailable()
                await db.commit()
            pending.future.set_result(approved)

    async def _finish(self, identifier: str, state: str) -> None:
        async with self._lock:
            pending = self._pending.pop(identifier, None)
            if pending is not None and not pending.future.done():
                pending.future.cancel()
            async with self.store.connection() as db:
                await db.execute(
                    "UPDATE authorizations SET state=? WHERE id=? AND state='pending'", (state, identifier)
                )
                await db.commit()

    async def wait(self, record: AuthorizationRecord) -> None:
        pending = self._pending.get(record.id)
        if pending is None:
            raise AuthorizationUnavailable()
        state = "canceled"
        try:
            async with asyncio.timeout(max(0, record.expires_at - time.time())):
                approved = await asyncio.shield(pending.future)
            state = "consumed"
            if not approved:
                raise AuthorizationDenied()
        except TimeoutError:
            state = "expired"
            raise AuthorizationExpired() from None
        finally:
            await self._finish(record.id, state)

    async def cancel_task(self, task_id: str) -> None:
        identifiers = [key for key, pending in self._pending.items() if pending.record.task_id == task_id]
        for identifier in identifiers:
            await self._finish(identifier, "canceled")

    async def close(self) -> None:
        for identifier in list(self._pending):
            await self._finish(identifier, "interrupted")


class AuthorizationSession:
    """Host-only access for tools; contains no incoming bearer credentials."""

    def __init__(
        self,
        registry: AuthorizationRegistry,
        task_id: str,
        user: Principal,
        report: StatusReporter,
        *,
        input_handler: InputHandler = unavailable_input,
        credential_handler: CredentialHandler = unavailable_credentials,
    ) -> None:
        self.registry = registry
        self.task_id = task_id
        self.user = user
        self.report = report
        self.request_input = input_handler
        self._credential_handler = credential_handler
        self.failure: Exception | None = None

    async def request_credentials(self, service: str) -> AccessGrant:
        return await self._credential_handler(self, service)

    async def approve(self, request: ApprovalRequest) -> bool:
        return await self._approve(
            {
                "tool": request.tool,
                "description": request.description,
                "arguments": request.arguments,
                "preview": request.preview,
                "agent": request.title or request.session_id,
                "depth": request.depth,
                "mandatory": request.mandatory,
            }
        )

    async def action(self, tool: str, arguments: ToolArguments, description: str) -> bool:
        """Authorize a native Agent operation without requiring ngn/Harness."""
        return await self._approve({"tool": tool, "arguments": arguments, "description": description})

    async def _approve(self, details: dict[str, object]) -> bool:
        record = await self.registry.create(
            self.task_id,
            self.user,
            "action",
            details,
        )
        url = f"{self.registry.config.public_url.rstrip('/')}/authorize/{record.id}"
        try:
            await self.report(
                TaskState.TASK_STATE_AUTH_REQUIRED,
                f"Action approval required. Review the proposed operation at {url}",
            )
            await self.registry.wait(record)
        except (AuthorizationDenied, AuthorizationExpired, AuthorizationUnavailable) as exc:
            self.failure = exc
            # HarnessExecutor's ordinary denial path prevents this exact
            # pending operation. The adapter ends the external task truthfully.
            return False
        await self.report(TaskState.TASK_STATE_WORKING, "Authorization complete; resuming work.")
        return True


current_authorization: ContextVar[AuthorizationSession] = ContextVar("ngn_a2a_authorization")


def authorization_session() -> AuthorizationSession:
    """Return host task scope for an explicitly installed server-side tool."""
    try:
        return current_authorization.get()
    except LookupError:
        raise AuthenticationError("authorization_unavailable", 403) from None
