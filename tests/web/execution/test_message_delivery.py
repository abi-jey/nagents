"""Per-message delivery overrides policy without crossing an unsafe boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Literal
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.providers.factory import HarnessProvider
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.harness.runtime.core import Harness
from nagents.web.http.routing import RoutingStore
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import assert_balanced
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.web.live.test_web_live_bridge import _config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition
    from nagents.web.execution.service import Run


@pytest.mark.requires_posix
@pytest.mark.parametrize("delivery", ["steer", "queue"])
@pytest.mark.parametrize("submit_mode", ["interrupt", "queue"])
def test_delivery_at_complete_tool_batch_or_outer_run_end(
    tmp_path: Path, delivery: str, submit_mode: Literal["interrupt", "queue"]
) -> None:
    async def scenario() -> None:
        streaming, respond, completed = (asyncio.Event() for _ in range(3))
        seen: list[str] = []
        owners: list[Run] = []
        (tmp_path / "fixture.txt").write_text("complete tool batch")

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            assert_balanced(messages)
            assert state.active is not None
            owners.append(state.active)
            prompt = str(messages[-1].content)
            if prompt == "initial":
                seen.append("initial")
                streaming.set()
                await respond.wait()
                yield ToolCallEvent(id="read", name="read_file", arguments={"path": "fixture.txt"})
            elif messages[-1].role == "tool":
                assert delivery == "queue"
                seen.append("original completed")
                yield TextDoneEvent(text="Original answer")
            elif prompt == "followup":
                assert any(message.role == "tool" for message in messages)
                seen.append("followup")
                yield TextDoneEvent(text="Followup answer")
                completed.set()
            else:
                raise AssertionError(prompt)

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            configured = _config(tmp_path)
            profile = configured.provider_profile()
            configured.providers, configured.provider = {"fixture": profile}, "fixture"
            configured.submit_mode = submit_mode
            ScopedProviderRegistryStore(tmp_path).workspace_store.save(
                ProviderRegistry(active="fixture", providers={"fixture": profile}), expected="0" * 64
            )
            async with client_app(tmp_path, config=configured) as (app, client, headers, _):
                state = app.state.web
                root = state.selected_session_id
                first = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": "00000000-0000-0000-0000-000000000001",
                        "prompt": "initial",
                    },
                )
                assert first.status_code == 200, first.text
                async with asyncio.timeout(HANG_GUARD):
                    await streaming.wait()
                active = state.active
                assert active is not None
                body = {
                    "session_id": root,
                    "message_id": "00000000-0000-0000-0000-000000000002",
                    "prompt": "followup",
                    "delivery": delivery,
                }
                try:
                    result = await client.post("/api/messages", headers=headers, json=body)
                    assert result.status_code == 200, result.text
                    assert result.json() == {"session_id": root, "message_id": body["message_id"], "status": "queued"}
                    retry = await client.post("/api/messages", headers=headers, json=body)
                    assert retry.status_code == 200
                    conflict = await client.post(
                        "/api/messages",
                        headers=headers,
                        json={**body, "delivery": "queue" if delivery == "steer" else "steer"},
                    )
                    assert conflict.status_code == 409
                    assert state.active is active and not active.task.cancelling()
                    assert seen == ["initial"]
                    respond.set()
                    async with asyncio.timeout(HANG_GUARD):
                        await completed.wait()
                    if delivery == "steer":
                        assert seen == ["initial", "followup"]
                        assert owners[-1] is active
                    else:
                        assert seen == ["initial", "original completed", "followup"]
                        assert owners[-1] is not active and active.finished
                finally:
                    respond.set()

    asyncio.run(scenario())


def test_delivery_fifo_barrier_persists_and_retry_is_immutable(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            state.mutating = True
            root = state.selected_session_id
            store = state.channels.store
            for number, delivery in enumerate(("queue", "steer"), 1):
                body = {
                    "session_id": root,
                    "message_id": f"00000000-0000-0000-0000-{number:012d}",
                    "prompt": str(number),
                    "delivery": delivery,
                }
                result = await client.post("/api/messages", headers=headers, json=body)
                assert result.status_code == 200, result.text
            assert await store.claim_work(web_only=True, session_id=root) is None
            # The barrier belongs only to this root, not to unrelated chats.
            other = await state.harness.new_session()
            await store.web(other, "other", "other", delivery="steer")
            work = await store.claim_work(web_only=True, session_id=other)
            assert work is not None and work.prompt == "other"
            await store.finish_work(work, "completed")
            # Reopen the durable store without starting an idle worker that could
            # correctly claim the queued message before the assertion.
            store = RoutingStore(store.db_path)
            await store.initialize()
            assert await store.claim_work(web_only=True, session_id=root) is None
            first = await store.claim_work(web_only=True)
            assert first is not None and first.prompt == "1"
            await store.finish_work(first, "completed")
            second = await store.claim_work(web_only=True, session_id=root)
            assert second is not None and second.prompt == "2"

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["interrupt", "", 1, False, [], {}])
def test_invalid_delivery_is_rejected(tmp_path: Path, delivery: object) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            result = await client.post(
                "/api/messages",
                headers=headers,
                json={
                    "session_id": app.state.web.selected_session_id,
                    "message_id": "00000000-0000-0000-0000-000000000001",
                    "prompt": "hello",
                    "delivery": delivery,
                },
            )
            assert result.status_code == 422

    asyncio.run(scenario())
