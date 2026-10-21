"""Remote action approval stays task- and origin-bound at the host boundary."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from nagents.harness.config import AgentProfile
from nagents.harness.remote_approvals import review_action
from nagents.harness.remote_profiles import RemoteProfile
from nagents.harness.remote_store import RemoteAgentCatalog

if TYPE_CHECKING:
    from nagents.harness.types import Notice


class JsonResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value
        self.content = json.dumps(value).encode()

    def json(self) -> dict[str, object]:
        return self.value

    def raise_for_status(self) -> None:
        return None


class RemoteHTTP:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict[str, str], dict[str, object]]] = []

    async def get(self, url: str, *, headers: dict[str, str]) -> JsonResponse:
        self.gets.append((url, headers))
        return JsonResponse(self.value)

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> JsonResponse:
        self.posts.append((url, headers, json))
        return JsonResponse({"accepted": True})


class ApprovalHarness:
    def __init__(
        self,
        *,
        allowed: bool,
        trusted: bool = False,
        mode: str = "build",
        demo: bool = False,
        closed: bool = False,
        root_closed: bool = False,
        allow_subagents: bool = True,
        delegate_enabled: bool = True,
        accepted_profile: RemoteProfile | None = None,
        trust_fingerprint: str = "",
    ) -> None:
        self.config = SimpleNamespace(agent="peer", demo=demo)
        self._closed = closed
        self.mode = mode
        self.allow_subagents = allow_subagents
        self.delegate_enabled = delegate_enabled
        self.tasks = SimpleNamespace(
            root=SimpleNamespace(
                harness=SimpleNamespace(
                    _closed=root_closed,
                    config=SimpleNamespace(demo=demo),
                    allow_subagents=allow_subagents,
                )
            )
        )
        self._remote_profile = accepted_profile or RemoteProfile(url="https://peer.example")
        fingerprint = trust_fingerprint or self._remote_profile.fingerprint
        catalog_profile = AgentProfile(a2a=accepted_profile or self._remote_profile)
        self._agent_catalog = SimpleNamespace(
            remote_store=SimpleNamespace(
                load=lambda: RemoteAgentCatalog(
                    {"peer": catalog_profile}, "revision", frozenset({fingerprint}) if trusted else frozenset()
                )
            )
        )
        self.refreshes = 0
        self.allowed = allowed
        self.requests: list[tuple[str, dict[str, object], str, str, bool]] = []
        self.notices: list[Notice] = []

    def refresh_tool_policy(self) -> None:
        self.refreshes += 1

    def tool_enabled(self, name: str) -> bool:
        return name == "delegate" and self.delegate_enabled

    async def emit(self, event: Notice) -> None:
        self.notices.append(event)

    async def approve(
        self,
        tool: str,
        arguments: dict[str, object],
        description: str,
        *,
        preview: str,
        mandatory: bool,
    ) -> None:
        self.requests.append((tool, arguments, description, preview, mandatory))
        if not self.allowed:
            raise PermissionError("The host denied this operation")


class RemotePeer:
    def __init__(self, record: dict[str, object], *, url: str = "https://peer.example") -> None:
        self.url = url
        self.http = RemoteHTTP(record)


def record(*, task_id: str = "task-123") -> dict[str, object]:
    return {
        "task_id": task_id,
        "kind": "action",
        "nonce": "single-use-nonce",
        "digest": "operation-digest",
        "operation": {"tool": "shell", "arguments": {"command": "printf safe"}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_action_review_is_mandatory_and_nonce_is_only_sent_back_to_remote(allowed: bool) -> None:
    harness = ApprovalHarness(allowed=allowed)
    remote = RemotePeer(record())

    handled = await review_action(
        harness,  # type: ignore[arg-type]
        remote,  # type: ignore[arg-type]
        "task-123",
        "Action approval required. Review the proposed operation at https://peer.example/authorize/approval-1234567890",
    )

    assert handled
    assert len(harness.requests) == 1
    tool, arguments, description, preview, mandatory = harness.requests[0]
    assert tool == "delegate"
    assert mandatory
    assert arguments == {
        "agent": "peer",
        "remote_operation": {"tool": "shell", "arguments": {"command": "printf safe"}},
    }
    assert "single-use-nonce" not in json.dumps(arguments)
    assert "operation-digest" not in json.dumps(arguments)
    assert "single-use-nonce" not in description + preview
    assert len(remote.http.posts) == 1
    post_url, headers, decision = remote.http.posts[0]
    assert post_url.endswith("/authorize/approval-1234567890/decision")
    assert headers == {"Accept": "application/json"}
    assert decision == {
        "approved": allowed,
        "nonce": "single-use-nonce",
        "digest": "operation-digest",
    }


@pytest.mark.asyncio
async def test_review_url_from_another_origin_is_never_contacted() -> None:
    harness = ApprovalHarness(allowed=True)
    remote = RemotePeer(record())

    handled = await review_action(
        harness,  # type: ignore[arg-type]
        remote,  # type: ignore[arg-type]
        "task-123",
        "Action approval required. Review the proposed operation at https://attacker.example/authorize/approval-1234567890",
    )

    assert not handled
    assert harness.requests == []
    assert remote.http.gets == []
    assert remote.http.posts == []


@pytest.mark.asyncio
async def test_task_mismatch_is_rejected_before_approval_or_decision() -> None:
    harness = ApprovalHarness(allowed=True)
    remote = RemotePeer(record(task_id="other-task"))

    with pytest.raises(ValueError, match="does not match"):
        await review_action(
            harness,  # type: ignore[arg-type]
            remote,  # type: ignore[arg-type]
            "task-123",
            "Action approval required. Review the proposed operation at https://peer.example/authorize/approval-1234567890",
        )

    assert remote.http.gets == [
        (
            "https://peer.example/authorize/approval-1234567890",
            {"Accept": "application/json"},
        )
    ]
    assert harness.requests == []
    assert remote.http.posts == []


@pytest.mark.asyncio
async def test_unsafe_review_path_is_not_contacted() -> None:
    harness = ApprovalHarness(allowed=True)
    remote = RemotePeer(record())

    handled = await review_action(
        harness,  # type: ignore[arg-type]
        remote,  # type: ignore[arg-type]
        "task-123",
        "Action approval required. Review the proposed operation at https://peer.example/authorize/../../secrets",
    )

    assert not handled
    assert harness.requests == []
    assert remote.http.gets == []
    assert remote.http.posts == []


@pytest.mark.asyncio
async def test_workspace_connection_trust_approves_only_the_validated_remote_operation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = RemoteProfile(url="https://peer.example")
    harness = ApprovalHarness(allowed=False, trusted=True, accepted_profile=profile)
    remote = RemotePeer(record())
    with caplog.at_level(logging.INFO, logger="nagents.harness.remote_approvals"):
        handled = await review_action(
            harness,  # type: ignore[arg-type]
            remote,  # type: ignore[arg-type]
            "task-123",
            "Action approval required. Review the proposed operation at https://peer.example/authorize/approval-1234567890",
        )

    assert handled
    assert harness.requests == []
    assert harness.refreshes == 1
    assert remote.http.posts[0][2]["approved"] is True
    assert "workspace_remote_tool_trust" in caplog.text
    assert "shell" in caplog.text
    assert "printf safe" not in caplog.text
    assert "single-use-nonce" not in caplog.text
    assert "operation-digest" not in caplog.text
    assert len(harness.notices) == 1
    assert harness.notices[0].text == "Workspace trust automatically approved remote tool shell for peer."
    assert "printf safe" not in harness.notices[0].text
    assert "single-use-nonce" not in harness.notices[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "demo", "closed", "root_closed", "allow_subagents", "delegate_enabled"),
    [
        ("reviewer", False, False, False, True, True),
        ("build", True, False, False, True, True),
        ("build", False, True, False, True, True),
        ("build", False, False, True, True, True),
        ("build", False, False, False, False, True),
        ("build", False, False, False, True, False),
    ],
)
async def test_trusted_remote_tools_still_obey_live_local_policy(
    mode: str,
    demo: bool,
    closed: bool,
    root_closed: bool,
    allow_subagents: bool,
    delegate_enabled: bool,
) -> None:
    profile = RemoteProfile(url="https://peer.example")
    harness = ApprovalHarness(
        allowed=False,
        trusted=True,
        mode=mode,
        demo=demo,
        closed=closed,
        root_closed=root_closed,
        allow_subagents=allow_subagents,
        delegate_enabled=delegate_enabled,
        accepted_profile=profile,
    )
    remote = RemotePeer(record())

    handled = await review_action(
        harness,  # type: ignore[arg-type]
        remote,  # type: ignore[arg-type]
        "task-123",
        "Action approval required. Review the proposed operation at https://peer.example/authorize/approval-1234567890",
    )

    assert handled
    assert harness.requests == []
    preblocked = closed or root_closed or demo or mode != "build" or not allow_subagents
    assert harness.refreshes == (0 if preblocked else 1)
    assert remote.http.posts[0][2]["approved"] is False


@pytest.mark.asyncio
async def test_trusted_binding_change_and_revocation_retain_mandatory_review() -> None:
    active = RemoteProfile(url="https://peer.example")
    changed = RemoteProfile(url="https://other.example")
    harness = ApprovalHarness(
        allowed=False,
        trusted=True,
        accepted_profile=active,
        trust_fingerprint=changed.fingerprint,
    )
    remote = RemotePeer(record())

    await review_action(
        harness,  # type: ignore[arg-type]
        remote,  # type: ignore[arg-type]
        "task-123",
        "Action approval required. Review the proposed operation at https://peer.example/authorize/approval-1234567890",
    )

    assert len(harness.requests) == 1
    assert remote.http.posts[0][2]["approved"] is False


@pytest.mark.asyncio
async def test_workspace_trust_does_not_bypass_task_identity_validation() -> None:
    profile = RemoteProfile(url="https://peer.example")
    harness = ApprovalHarness(allowed=True, trusted=True, accepted_profile=profile)
    remote = RemotePeer(record(task_id="another-task"))

    with pytest.raises(ValueError, match="does not match"):
        await review_action(
            harness,  # type: ignore[arg-type]
            remote,  # type: ignore[arg-type]
            "task-123",
            "Action approval required. Review the proposed operation at https://peer.example/authorize/approval-1234567890",
        )

    assert harness.requests == []
    assert remote.http.posts == []
