"""Task-bound authorization decisions are private, scoped and one-shot."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
from a2a.helpers import new_text_message
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import TaskState

from nagents.a2a.auth import AuthenticationError
from nagents.a2a.auth import Principal
from nagents.a2a.authorization import AuthorizationDenied
from nagents.a2a.authorization import AuthorizationExpired
from nagents.a2a.authorization import AuthorizationRegistry
from nagents.a2a.authorization import AuthorizationSession
from nagents.a2a.authorization import AuthorizationUnavailable
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.store import MessageClaim
from nagents.a2a.store import SQLiteTaskStore
from nagents.harness.types import ApprovalRequest

if TYPE_CHECKING:
    from pathlib import Path

ISSUER = "https://issuer.example"


def principal(
    subject: str = "alice", tenant: str = "tenant-a", *scopes: str, expires_at: float | None = None
) -> Principal:
    return Principal(
        ISSUER,
        subject,
        tenant,
        frozenset({"ngn:access", *scopes}),
        time.time() + 3600 if expires_at is None else expires_at,
    )


def message(message_id: str) -> SendMessageRequest:
    value = new_text_message(f"Prompt {message_id}", role=Role.ROLE_USER)
    value.message_id = message_id
    return SendMessageRequest(message=value)


async def environment(path: Path, *, approval_timeout: int = 30) -> tuple[SQLiteTaskStore, AuthorizationRegistry]:
    config = A2AConfig(
        public_url="https://a2a.example",
        agent="assistant",
        issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
        data_dir=path,
        approval_timeout=approval_timeout,
    )
    store = SQLiteTaskStore(config)
    await store.initialize()
    registry = AuthorizationRegistry(config, store)
    await registry.initialize()
    return store, registry


async def task_claim(store: SQLiteTaskStore, user: Principal, message_id: str) -> MessageClaim:
    return await store.reserve(message(message_id), user)


def test_decision_is_bound_to_owner_task_nonce_digest_and_fresh_scopes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "approval-1")
        record = await registry.create(claim.task_id, alice, "action", {"tool": "edit", "path": "notes.md"})

        before_approval = principal("alice", "tenant-a", expires_at=time.time() - 1)
        with pytest.raises(AuthenticationError):
            await registry.inspect(record.id, before_approval)
        no_scope = principal("alice", "tenant-a")
        with pytest.raises(AuthenticationError):
            await registry.inspect(record.id, no_scope)
        with pytest.raises(AuthenticationError):
            await registry.decide(record.id, no_scope, record.nonce, True, record.digest)
        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(record.id, principal("mallory", "tenant-a", "ngn:approve"))
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(record.id, alice, "wrong-nonce", True, record.digest)
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(record.id, alice, record.nonce, True, "0" * 64)
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(record.id, alice, record.nonce, 1, record.digest)  # type: ignore[arg-type]

        waiting = asyncio.create_task(registry.wait(record))
        await registry.decide(record.id, alice, record.nonce, True, record.digest)
        await waiting
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(record.id, alice, record.nonce, True, record.digest)
        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(record.id, alice)
        await registry.close()

    asyncio.run(scenario())


def test_explicit_approve_any_is_limited_to_same_tenant_and_one_shot(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "shared-approval")
        record = await registry.create(claim.task_id, alice, "oauth", {"client": "browser"})
        operator = principal("operator", "tenant-a", "ngn:approve", "ngn:approve:any")
        assert await registry.inspect(record.id, operator) == record
        waiting = asyncio.create_task(registry.wait(record))
        await registry.decide(record.id, operator, record.nonce, True, record.digest)
        await waiting

        other_tenant = principal("operator", "tenant-b", "ngn:approve", "ngn:approve:any")
        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(record.id, other_tenant)
        await registry.close()

    asyncio.run(scenario())


def test_denied_and_canceled_pending_decisions_never_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "deny-approval")
        denied = await registry.create(claim.task_id, alice, "action", {"tool": "shell"})
        denial_wait = asyncio.create_task(registry.wait(denied))
        await registry.decide(denied.id, alice, denied.nonce, False, denied.digest)
        with pytest.raises(AuthorizationDenied):
            await denial_wait
        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(denied.id, alice)

        canceled_claim = await task_claim(store, alice, "cancel-approval")
        canceled = await registry.create(canceled_claim.task_id, alice, "action", {"tool": "edit"})
        await registry.cancel_task(canceled_claim.task_id)
        with pytest.raises(AuthorizationUnavailable):
            await registry.wait(canceled)
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(canceled.id, alice, canceled.nonce, True, canceled.digest)
        await registry.close()

    asyncio.run(scenario())


def test_expired_authorization_cannot_be_inspected_or_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data", approval_timeout=1)
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "expired-approval")
        record = await registry.create(claim.task_id, alice, "action", {"tool": "edit"})
        monkeypatch.setattr(
            "nagents.a2a.authorization.time",
            SimpleNamespace(time=lambda: record.expires_at + 1),
        )
        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(record.id, alice)
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(record.id, alice, record.nonce, True, record.digest)
        with pytest.raises(AuthorizationExpired):
            await registry.wait(record)
        await registry.close()

    asyncio.run(scenario())


def test_pending_authorization_is_interrupted_on_registry_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "restart-approval")
        record = await registry.create(claim.task_id, alice, "action", {"tool": "edit"})
        await registry.close()

        reopened = AuthorizationRegistry(registry.config, store)
        await reopened.initialize()
        with pytest.raises(AuthorizationUnavailable):
            await reopened.inspect(record.id, alice)
        with pytest.raises(AuthorizationUnavailable):
            await reopened.decide(record.id, alice, record.nonce, True, record.digest)

        async with store.connection() as db:
            row = await (await db.execute("SELECT state FROM authorizations WHERE id=?", (record.id,))).fetchone()
        assert row is not None and row[0] == "interrupted"
        await reopened.close()

    asyncio.run(scenario())


def test_approval_report_excludes_private_arguments_and_records_resume_states(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "private-report")
        reports: list[tuple[TaskState, str]] = []

        async def report(state: TaskState, text: str) -> None:
            reports.append((state, text))

        session = AuthorizationSession(registry, claim.task_id, alice, report)
        private_value = "do-not-publish-this-secret"
        approval = asyncio.create_task(
            session.approve(
                ApprovalRequest(
                    id="tool-call-1",
                    tool="edit",
                    description="Update the specified file",
                    arguments={"path": "draft.md", "content": private_value},
                    preview="Replace the draft text",
                    mandatory=True,
                )
            )
        )
        for _ in range(100):
            if reports:
                break
            await asyncio.sleep(0.005)
        assert reports and reports[0][0] == TaskState.TASK_STATE_AUTH_REQUIRED
        assert private_value not in reports[0][1]
        assert "draft.md" not in reports[0][1]
        authorization_id = urlsplit(reports[0][1].rsplit(" ", 1)[-1]).path.rsplit("/", 1)[-1]
        record = await registry.inspect(authorization_id, alice)
        assert record.details["arguments"] == {"path": "draft.md", "content": private_value}
        assert record.details["mandatory"] is True
        await registry.decide(record.id, alice, record.nonce, True, record.digest)
        assert await approval
        assert [state for state, _ in reports] == [
            TaskState.TASK_STATE_AUTH_REQUIRED,
            TaskState.TASK_STATE_WORKING,
        ]
        assert private_value not in " ".join(text for _, text in reports)
        await registry.close()

    asyncio.run(scenario())


def test_mutating_nested_authorization_details_cannot_change_private_operation_or_digest(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "nested-approval")
        details: dict[str, object] = {
            "tool": "write_file",
            "arguments": {
                "path": "reports/current.md",
                "options": {"mode": "replace", "labels": ["approved", "reviewed"]},
            },
            "preview": {"changes": [{"line": 3, "text": "Keep the approved value."}]},
        }
        expected = {
            "tool": "write_file",
            "arguments": {
                "path": "reports/current.md",
                "options": {"mode": "replace", "labels": ["approved", "reviewed"]},
            },
            "preview": {"changes": [{"line": 3, "text": "Keep the approved value."}]},
        }
        record = await registry.create(claim.task_id, alice, "action", details)
        original_digest = record.digest
        original_nonce = record.nonce

        supplied_arguments = details["arguments"]
        assert isinstance(supplied_arguments, dict)
        supplied_options = supplied_arguments["options"]
        assert isinstance(supplied_options, dict)
        supplied_options["labels"].append("caller-mutated")

        returned_arguments = record.details["arguments"]
        assert isinstance(returned_arguments, dict)
        returned_options = returned_arguments["options"]
        assert isinstance(returned_options, dict)
        returned_options["labels"].append("returned-record-mutated")

        inspected = await registry.inspect(record.id, alice)
        assert inspected.digest == original_digest
        assert inspected.details == expected
        inspected_preview = inspected.details["preview"]
        assert isinstance(inspected_preview, dict)
        inspected_changes = inspected_preview["changes"]
        assert isinstance(inspected_changes, list)
        inspected_changes[0]["text"] = "inspected snapshot mutation"

        second_inspection = await registry.inspect(record.id, alice)
        assert second_inspection.details == expected
        assert second_inspection.digest == original_digest

        waiting = asyncio.create_task(registry.wait(record))
        await registry.decide(record.id, alice, original_nonce, True, original_digest)
        await waiting
        async with store.connection() as db:
            row = await (
                await db.execute("SELECT digest,details,state FROM authorizations WHERE id=?", (record.id,))
            ).fetchone()
        assert row is not None
        assert row[0] == original_digest
        assert row[1] == (
            '{"arguments":{"options":{"labels":["approved","reviewed"],"mode":"replace"},'
            '"path":"reports/current.md"},"preview":{"changes":[{"line":3,'
            '"text":"Keep the approved value."}]},"tool":"write_file"}'
        )
        assert row[2] == "approved"
        await registry.close()

    asyncio.run(scenario())


def test_authorization_history_is_bounded_per_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, registry = await environment(tmp_path / "data")
        alice = principal("alice", "tenant-a", "ngn:approve")
        claim = await task_claim(store, alice, "bounded-history")
        for index in range(32):
            record = await registry.create(claim.task_id, alice, "action", {"index": index})
            waiting = asyncio.create_task(registry.wait(record))
            await registry.decide(record.id, alice, record.nonce, True, record.digest)
            await waiting
        with pytest.raises(AuthorizationUnavailable):
            await registry.create(claim.task_id, alice, "action", {"index": 32})
        await registry.close()

    asyncio.run(scenario())
