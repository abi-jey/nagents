"""Remote action approval stays task- and origin-bound at the host boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nagents.harness.remote_approvals import review_action


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
    def __init__(self, *, allowed: bool) -> None:
        self.config = SimpleNamespace(agent="peer")
        self.allowed = allowed
        self.requests: list[tuple[str, dict[str, object], str, str, bool]] = []

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
    def __init__(self, record: dict[str, object]) -> None:
        self.url = "https://peer.example"
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
