"""Remote A2A failures reach the parent as safe, actionable task outcomes."""

from __future__ import annotations

import json
import os
import ssl
import time
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import httpx
import pytest
from a2a.types import Message as A2AMessage
from a2a.types import Part
from a2a.types import Role
from a2a.types import StreamResponse
from a2a.types import Task
from a2a.types import TaskState
from a2a.types import TaskStatus
from google.protobuf.json_format import MessageToDict

from nagents.a2a import client as a2a_client
from nagents.a2a.application import agent_card
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.conversation import RemoteConversation
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote_credentials import RemoteCredentials
from nagents.harness.remote_profiles import RemoteProfile
from nagents.harness.types import TaskActivity
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskNotification
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.a2a.client import RemoteAgent
    from nagents.harness.types import HarnessEvent
    from nagents.types import Message


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Remote-agent credential storage requires POSIX")


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home, data_home = tmp_path / "config", tmp_path / "data"
    config_home.mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    config_home.chmod(0o700)
    data_home.chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


def _card(origin: str, directory: Path) -> dict[str, object]:
    config = A2AConfig(
        public_url=origin,
        agent="peer",
        issuer=IssuerConfig(issuer="https://issuer.example", audience="remote-peer"),
        data_dir=directory,
    )
    return cast("dict[str, object]", MessageToDict(agent_card(config)))


def _task_payload(state: TaskState, *, status_text: str = "") -> dict[str, object]:
    task = Task(
        id="remote-task-fixture",
        context_id="remote-context-fixture",
        status=TaskStatus(
            state=state,
            message=A2AMessage(role=Role.ROLE_AGENT, parts=[Part(text=status_text)] if status_text else []),
        ),
    )
    return cast("dict[str, object]", MessageToDict(StreamResponse(task=task)))


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    actual = a2a_client.RemoteAgent

    def with_transport(url: str, tokens: object = None, **kwargs: object) -> RemoteAgent:
        kwargs["transport"] = handler
        return actual(url, tokens, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(a2a_client, "RemoteAgent", with_transport)


async def _delegated_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: str,
    token_env: str = "NGN_REMOTE_FAILURE_TOKEN",
    status_state: TaskState = TaskState.TASK_STATE_UNSPECIFIED,
) -> tuple[list[HarnessEvent], list[FakeProvider], list[tuple[str, str, str]], tuple[str, int] | None, str]:
    origin = "https://peer.example"
    card = _card(origin, tmp_path / "card-state")
    requests: list[tuple[str, str, str]] = []
    secret_token = "PRIVATE_REMOTE_BEARER_MARKER"
    secret_body = "PRIVATE_UPSTREAM_BODY_MARKER"
    secret_exception = "PRIVATE_EXCEPTION_AND_QUERY_MARKER"

    def respond(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        requests.append((method, path, request.headers.get("authorization", "")))
        if method == "GET" and path == "/.well-known/agent-card.json":
            if scenario in {"card-401", "card-403"}:
                status = 401 if scenario == "card-401" else 403
                return httpx.Response(status, json={"detail": secret_body}, request=request)
            if scenario == "connect":
                raise httpx.ConnectError(
                    f"GET {origin}/.well-known/agent-card.json?token={secret_exception}", request=request
                )
            if scenario == "tls":
                raise httpx.ConnectError("certificate failure", request=request) from ssl.SSLCertVerificationError(
                    secret_exception
                )
            return httpx.Response(200, json=card, request=request)
        if method != "POST" or path != "/rpc":
            return httpx.Response(404, json={"detail": secret_body}, request=request)
        payload = json.loads(request.content)
        status_codes = {"http-401": 401, "http-403": 403, "http-429": 429, "http-503": 503}
        if scenario in status_codes:
            return httpx.Response(status_codes[scenario], json={"detail": secret_body}, request=request)
        if scenario == "timeout":
            raise httpx.ReadTimeout(secret_exception, request=request)
        if scenario == "protocol":
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=secret_body, request=request
            )
        if scenario in {"failed", "rejected", "cancelled", "auth-required"}:
            result = _task_payload(status_state, status_text=secret_body)
            rpc = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
            content = f"data: {json.dumps(rpc)}\n\n"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=content,
                request=request,
            )
        return httpx.Response(500, json={"detail": secret_body}, request=request)

    _install_transport(monkeypatch, httpx.MockTransport(respond))
    parent_inputs: list[str] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if notifications(messages):
            parent_inputs.extend(str(note.content) for note in notifications(messages))
            yield TextDoneEvent(text="The parent received the remote failure and continued safely.")
        elif provider.index == 0 and len(provider.requests) == 1:
            yield ToolCallEvent(
                id="delegate-remote-failure",
                name="to_agent",
                arguments={"agent": "peer", "prompt": "Check the configured remote task."},
            )
        else:
            yield TextDoneEvent(text="Waiting for the remote task outcome.")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    oauth = scenario in {"missing-oauth", "expired-oauth", "unsafe-storage"}
    remote = RemoteProfile(
        url=origin,
        issuer="https://issuer.example" if oauth else "",
        client_id="fixture-public-client" if oauth else "",
        token_env="" if oauth else token_env,
    )
    store = harness._agent_catalog.remote_store
    store.save({"peer": AgentProfile(description="Remote test peer", a2a=remote)}, expected=store.load().revision)
    assert harness.refresh_agent_definitions()
    if scenario not in {"missing-token", "missing-oauth", "expired-oauth", "unsafe-storage"}:
        monkeypatch.setenv(token_env, secret_token)
    else:
        monkeypatch.delenv(token_env, raising=False)

    async def approve_remote_call(request: object) -> bool:
        del request
        return True

    harness.approval_handler = approve_remote_call
    try:
        if oauth:
            await harness.initialize()
            credentials = RemoteCredentials(remote)
            if scenario != "missing-oauth":
                data_home = tmp_path / "data"
                for directory in credentials.path.parents:
                    if directory == data_home:
                        break
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    directory.chmod(0o700)
                credentials.path.write_text(
                    json.dumps({"access_token": "PRIVATE_EXPIRED_OAUTH_TOKEN", "expires_at": time.time() - 1}),
                    encoding="utf-8",
                )
                credentials.path.chmod(0o644 if scenario == "unsafe-storage" else 0o600)
        events = await collect(harness, "Delegate one remote task and report its outcome.")
        completion = next(event for event in events if isinstance(event, TaskCompleted))
        expected_status = "cancelled" if scenario == "cancelled" else "failed"
        assert completion.status == expected_status
        assert completion.result == ""
        notification = next(event for event in events if isinstance(event, TaskNotification))
        assert notification.text == completion.error
        failure_status = [
            event.payload
            for event in events
            if isinstance(event, TaskActivity)
            and event.payload.get("type") == "status"
            and event.payload.get("phase") == "failed"
        ]
        assert any(
            payload.get("detail") == completion.error and isinstance(payload.get("code"), str)
            for payload in failure_status
        )
        assert any(isinstance(event, TextDoneEvent) and "continued safely" in event.text for event in events)
        assert any(isinstance(event, DoneEvent) for event in events)
        assert all(not provider.requests for provider in providers[1:]), (
            "remote child work must not invoke a local model provider"
        )
        assert len(providers[0].requests) >= 2, "the parent must receive a child failure notification next round"
        public_records = repr(events) + repr(parent_inputs) + repr(await harness.history()) + repr(harness.tasks.list())
        for marker in (secret_token, secret_body, secret_exception, "PRIVATE_EXPIRED_OAUTH_TOKEN"):
            assert marker not in public_records
        assert "PRIVATE" not in notification.text
        child = harness.tasks._children[completion.session_id]
        async with (
            aiosqlite.connect(child.agent.session.db_path) as db,
            db.execute(
                "SELECT task_id,pending FROM ngn_remote_sessions WHERE session_id=?", (child.session_id,)
            ) as cursor,
        ):
            row = await cursor.fetchone()
        remote_record = None if row is None else (str(row[0]), int(row[1]))
        return events, providers, requests, remote_record, completion.error
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "guidance", "send_count", "pending"),
    [
        ("missing-token", "configured server token is missing", 0, None),
        ("missing-oauth", "sign-in is missing or expired", 0, None),
        ("expired-oauth", "sign-in is missing or expired", 0, None),
        ("unsafe-storage", "credential storage permissions", 0, None),
        ("card-401", "rejected the access token", 0, None),
        ("card-403", "denied access", 0, None),
        ("http-401", "rejected the access token", 1, 0),
        ("http-403", "denied access", 1, 0),
        ("http-429", "rate limited", 1, 0),
        ("http-503", "remote server returned an error", 1, 1),
        ("connect", "server address", 0, None),
        ("tls", "TLS connection could not be verified", 0, None),
        ("timeout", "may have reached the remote agent", 1, 1),
        ("protocol", "invalid or unsupported response", 1, 1),
    ],
)
async def test_remote_credential_and_transport_failures_reach_parent_without_leaking_or_resending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    guidance: str,
    send_count: int,
    pending: int | None,
) -> None:
    _, _, requests, remote_record, error = await _delegated_failure(tmp_path, monkeypatch, scenario=scenario)
    assert guidance.lower() in error.lower()
    assert sum(method == "POST" and path == "/rpc" for method, path, _ in requests) == send_count
    if pending is None:
        assert remote_record is None, "pre-dispatch failures must not create a pending remote task record"
        assert "No task was sent." in error
    else:
        assert remote_record is not None and remote_record[1] == pending
        if pending == 0 and scenario in {"http-401", "http-403", "http-429"}:
            assert remote_record[0] == "", "a definite auth/rate rejection must not claim a task ID"
            assert "The remote server did not accept a task. This request was not automatically retried." in error
        elif pending == 1:
            assert "The request may have reached the remote agent." in error
    if scenario in {"card-401", "card-403", "http-401", "http-403", "http-429", "http-503"}:
        assert sum(method == "GET" and path.endswith("agent-card.json") for method, path, _ in requests) == 1
        assert all(auth == "Bearer PRIVATE_REMOTE_BEARER_MARKER" for _, _, auth in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "status", "guidance"),
    [
        ("failed", TaskState.TASK_STATE_FAILED, "reported that its task failed"),
        ("rejected", TaskState.TASK_STATE_REJECTED, "rejected this task"),
        ("cancelled", TaskState.TASK_STATE_CANCELED, "was cancelled"),
        ("auth-required", TaskState.TASK_STATE_AUTH_REQUIRED, "sign-in"),
    ],
)
async def test_remote_terminal_and_auth_states_are_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    status: TaskState,
    guidance: str,
) -> None:
    _, _, requests, remote_record, error = await _delegated_failure(
        tmp_path, monkeypatch, scenario=scenario, status_state=status
    )
    assert guidance.lower() in error.lower()
    assert sum(method == "POST" and path == "/rpc" for method, path, _ in requests) == 1
    assert remote_record == ("remote-task-fixture", 0)


@pytest.mark.asyncio
async def test_existing_task_401_does_not_read_or_resubscribe_previous_task_before_sign_in(
    tmp_path: Path,
) -> None:
    origin = "https://peer.example"
    card = _card(origin, tmp_path / "card-state")
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=card, request=request)
        method = str(json.loads(request.content)["method"])
        methods.append(method)
        return httpx.Response(401, json={"detail": "PRIVATE_REAUTH_BODY"}, request=request)

    async def token() -> str:
        return "PRIVATE_REAUTH_TOKEN"

    async with a2a_client.RemoteAgent(origin, token, transport=httpx.MockTransport(respond)) as remote:
        conversation = RemoteConversation(remote, task_id="existing-task", context_id="existing-context")
        with pytest.raises(a2a_client.RemoteAgentError) as rejected:
            _ = [
                update async for update in conversation.exchange("new follow-up", message_id="stable-follow-up-message")
            ]

    assert rejected.value.code == "A2A_AUTH_REJECTED"
    assert conversation.task_id == "existing-task"
    assert methods == ["SendStreamingMessage"]
    assert "PRIVATE_REAUTH_BODY" not in str(rejected.value)
    assert "PRIVATE_REAUTH_TOKEN" not in str(rejected.value)
