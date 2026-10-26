"""HTTP boundary tests exercise the local OIDC service and SDK JSON-RPC routes."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import cast

import httpx
import pytest
from a2a.types import SendMessageRequest
from google.protobuf.json_format import MessageToDict

from nagents.a2a.server.application import create_app
from nagents.a2a.server.config import A2AConfig
from nagents.a2a.server.config import IssuerConfig
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolDefinition
from tests.a2a.test_a2a_auth import IssuerState
from tests.a2a.test_a2a_auth import access_claims
from tests.a2a.test_a2a_auth import local_issuer
from tests.a2a.test_a2a_auth import make_token
from tests.a2a.test_a2a_auth import signing_key

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.a2a.agent import AgentContext

pytestmark = pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")

AUDIENCE = "nagents-a2a"
PUBLIC_URL = "http://127.0.0.1:42424"


class HTTPProvider(Provider):
    def __init__(self, script: Script) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "local-test-key", "local-test-model")
        self.script = script
        self.requests: list[list[Message]] = []

    async def verify_model(self, force: bool = False) -> bool:
        del force
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        del tools, config, stream, verify_model
        self.requests.append(messages.copy())
        async for event in self.script(self, messages):
            yield event


Script = Callable[[HTTPProvider, list[Message]], AsyncIterator[Event]]


def rpc(method: str, params: dict[str, object], identifier: str = "rpc-1") -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}


def send_params(message_id: str = "http-message", *, immediate: bool = False) -> dict[str, object]:
    from a2a.helpers import new_text_message
    from a2a.types import Role

    message = new_text_message("Write the approved marker", role=Role.ROLE_USER)
    message.message_id = message_id
    request = SendMessageRequest(message=message)
    request.configuration.return_immediately = immediate
    return cast("dict[str, object]", MessageToDict(request))


def task_from(response: dict[str, object]) -> dict[str, object]:
    result = response.get("result")
    assert isinstance(result, dict)
    task = result.get("task")
    return cast("dict[str, object]", task if isinstance(task, dict) else result)


def object_map(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def test_http_auth_rpc_scope_and_approval_flow(tmp_path: Path) -> None:
    async def scenario() -> None:
        key = signing_key("RS256", "http-key")
        async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
            config = A2AConfig(
                public_url=PUBLIC_URL,
                agent="assistant",
                issuer=IssuerConfig(
                    issuer=issuer,
                    audience=AUDIENCE,
                    algorithms=("RS256",),
                    role_claim="realm_access.roles",
                    role_scopes={"approver": ("ngn:approve",)},
                ),
                data_dir=tmp_path / "a2a-state",
                loopback_http=True,
                max_request_bytes=4096,
            )
            marker = tmp_path / "approved-http-marker"
            secret_argument = "PRIVATE_TOOL_ARGUMENT_SHOULD_NOT_APPEAR_IN_TASK"

            async def write_marker(payload: str) -> str:
                marker.write_text(payload)
                return "marker written"

            async def script(provider: HTTPProvider, messages: list[Message]) -> AsyncIterator[Event]:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(
                        id="rpc-write",
                        name="write_marker",
                        arguments={"payload": secret_argument},
                    )
                else:
                    yield TextDoneEvent(text="The approved marker was written.")
                    yield DoneEvent(final_text="The approved marker was written.")
                del messages

            providers: list[HTTPProvider] = []
            agents: list[Agent] = []

            def factory(context: AgentContext) -> Agent:
                provider = HTTPProvider(script)
                providers.append(provider)
                agent = Agent(
                    provider,
                    SessionManager(context.data_dir / "sessions.db"),
                    tools=[write_marker],
                    system_prompt="Test agent.",
                    streaming=True,
                    compaction_strategy=None,
                    save_tool_outputs=False,
                )
                agents.append(agent)
                return agent

            app = create_app(config=config, agent_factory=factory)
            access_token = make_token(
                key,
                access_claims(
                    issuer,
                    scope="ngn:access ngn:invoke ngn:read ngn:cancel",
                    realm_access={"roles": ["approver"]},
                ),
            )
            read_only_token = make_token(
                key,
                access_claims(issuer, scope="ngn:access ngn:read", realm_access={"roles": []}),
            )
            invoke_only_token = make_token(
                key,
                access_claims(issuer, scope="ngn:access ngn:invoke", realm_access={"roles": []}),
            )
            access_only_token = make_token(
                key,
                access_claims(issuer, scope="ngn:access", realm_access={"roles": []}),
            )
            other_owner_token = make_token(
                key,
                access_claims(
                    issuer,
                    sub="bob-subject",
                    scope="ngn:access ngn:read",
                    realm_access={"roles": []},
                ),
            )
            headers = {"Authorization": f"Bearer {access_token}", "A2A-Version": "1.0"}
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL) as client,
            ):
                card = await client.get("/.well-known/agent-card.json")
                assert card.status_code == 200
                card_data = card.json()
                assert card_data["name"] == "ngn"
                assert len(card_data["skills"]) == 1
                assert "private" not in card.text.lower()
                competing_app = create_app(config=config, agent_factory=factory)
                with pytest.raises(ValueError, match="already owned"):
                    async with competing_app.router.lifespan_context(competing_app):
                        pass

                missing = await client.post("/rpc", json=rpc("SendMessage", send_params()))
                assert missing.status_code == 401
                assert "Bearer" in missing.headers["www-authenticate"]
                denied_scope = await client.post(
                    "/rpc",
                    json=rpc("SendMessage", send_params("scope-denied")),
                    headers={"Authorization": f"Bearer {read_only_token}"},
                )
                assert denied_scope.status_code == 403
                assert 'error="insufficient_scope"' in denied_scope.headers["www-authenticate"]
                for index, method in enumerate(
                    (
                        "SendMessage",
                        "SendStreamingMessage",
                        "GetTask",
                        "ListTasks",
                        "SubscribeToTask",
                        "CancelTask",
                        "GetExtendedAgentCard",
                        "CreateTaskPushNotificationConfig",
                        "GetTaskPushNotificationConfig",
                        "ListTaskPushNotificationConfigs",
                        "DeleteTaskPushNotificationConfig",
                    )
                ):
                    forbidden = await client.post(
                        "/rpc",
                        json=rpc(method, {}, f"scope-matrix-{index}"),
                        headers={"Authorization": f"Bearer {access_only_token}", "A2A-Version": "1.0"},
                    )
                    assert forbidden.status_code == 403, method
                assert (await client.get("/api/settings")).status_code == 404
                assert (await client.get("/", params={"q": "x" * 17000})).status_code == 414
                assert (
                    await client.get("/.well-known/agent-card.json", headers={"Host": "attacker.invalid"})
                ).status_code == 400
                assert (
                    await client.post(
                        "/rpc",
                        json=rpc("SendMessage", send_params("origin-denied")),
                        headers={**headers, "Origin": "https://attacker.invalid"},
                    )
                ).status_code == 403

                oversized = {"jsonrpc": "2.0", "id": "large", "method": "SendMessage", "params": {"pad": "x" * 5000}}
                assert (await client.post("/rpc", json=oversized, headers=headers)).status_code == 413

                response = await client.post(
                    "/rpc",
                    json=rpc("SendMessage", send_params(immediate=True)),
                    headers=headers,
                )
                assert response.status_code == 200
                initial = task_from(response.json())
                task_id = initial["id"]
                assert isinstance(task_id, str)

                pending: dict[str, object] = {}
                for _ in range(100):
                    fetched = await client.post(
                        "/rpc",
                        json=rpc("GetTask", {"id": task_id}, "get-pending"),
                        headers=headers,
                    )
                    pending = task_from(fetched.json())
                    status = pending.get("status")
                    assert isinstance(status, dict)
                    if status.get("state") == "TASK_STATE_AUTH_REQUIRED":
                        break
                    await asyncio.sleep(0.01)
                pending_status = object_map(pending.get("status"))
                assert pending_status.get("state") == "TASK_STATE_AUTH_REQUIRED"
                status_message = object_map(pending_status.get("message"))
                parts = status_message.get("parts")
                assert isinstance(parts, list) and parts
                first_part = object_map(parts[0])
                approval_url = first_part.get("text")
                assert isinstance(approval_url, str)
                authorization_id = approval_url.rsplit("/", 1)[-1]
                denied_cancel = await client.post(
                    "/rpc",
                    json=rpc("CancelTask", {"id": task_id}, "cancel-denied"),
                    headers={"Authorization": f"Bearer {read_only_token}", "A2A-Version": "1.0"},
                )
                assert denied_cancel.status_code == 403
                denied_read = await client.post(
                    "/rpc",
                    json=rpc("GetTask", {"id": task_id}, "read-denied"),
                    headers={"Authorization": f"Bearer {invoke_only_token}", "A2A-Version": "1.0"},
                )
                assert denied_read.status_code == 403
                review = await client.get(
                    f"/authorize/{authorization_id}",
                    headers={**headers, "Accept": "application/json"},
                )
                assert review.status_code == 200
                operation = object_map(review.json()).get("operation")
                assert isinstance(operation, dict)
                assert object_map(operation).get("arguments") == {"payload": secret_argument}
                decision = await client.post(
                    f"/authorize/{authorization_id}/decision",
                    json={"approved": True, "nonce": review.json()["nonce"], "digest": review.json()["digest"]},
                    headers={**headers, "Origin": PUBLIC_URL},
                )
                assert decision.status_code == 200

                for _ in range(100):
                    fetched = await client.post(
                        "/rpc",
                        json=rpc("GetTask", {"id": task_id}, "get-complete"),
                        headers=headers,
                    )
                    completed = task_from(fetched.json())
                    completed_status = object_map(completed.get("status"))
                    if completed_status.get("state") == "TASK_STATE_COMPLETED":
                        break
                    await asyncio.sleep(0.01)
                assert object_map(completed.get("status")).get("state") == "TASK_STATE_COMPLETED"
                assert marker.read_text() == secret_argument
                assert secret_argument not in json.dumps(completed)
                foreign = await client.post(
                    "/rpc",
                    json=rpc("GetTask", {"id": task_id}, "foreign-get"),
                    headers={"Authorization": f"Bearer {other_owner_token}"},
                )
                assert foreign.status_code == 200
                foreign_body = object_map(foreign.json())
                error = object_map(foreign_body.get("error"))
                assert "result" not in foreign_body and error.get("code") != 0
                assert len(agents) == len(providers) == 1

    asyncio.run(scenario())
