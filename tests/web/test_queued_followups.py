"""Typed and voice messages share durable FIFO admission at model boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.live.delegation import ClientDelegationRequest
from nagents.web.deletion import delete_session
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.service import Run
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import assert_balanced
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.test_web_live_bridge import _config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.live.delegation import LiveAppendKind
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition
    from nagents.web.routing import Work


@pytest.mark.requires_posix
@pytest.mark.parametrize("typed_first", [False, True])
def test_typed_and_voice_fifo_reach_next_model_round_after_returned_tools(tmp_path: Path, typed_first: bool) -> None:
    async def scenario() -> None:
        streaming, respond, finished = (asyncio.Event() for _ in range(3))
        seen: list[str] = []
        (tmp_path / "fixture.txt").write_text("tool result before queued inputs")

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            assert_balanced(messages)
            prompt = str(messages[-1].content)
            if "voice A" in prompt and "voice C" not in prompt:
                seen.append("A")
                streaming.set()
                await respond.wait()
                yield ToolCallEvent(id="read-a", name="read_file", arguments={"path": "fixture.txt"})
            elif prompt == "typed B":
                assert messages[-2].role == "tool" and "tool result before queued inputs" in str(messages[-2].content)
                seen.append("B")
                yield TextDoneEvent(text="Answer B")
            elif "voice C" in prompt:
                seen.append("C")
                yield TextDoneEvent(text="Answer C")
            elif prompt == "other root":
                seen.append("other")
                yield TextDoneEvent(text="Other answer")
                finished.set()
            else:
                raise AssertionError("A queued input missed the next safe model boundary")

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            return f"session.{kind}.append"

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            configured = _config(tmp_path)
            profile = configured.provider_profile()
            configured.providers, configured.provider = {"fixture": profile}, "fixture"
            configured.submit_mode = "interrupt" if typed_first else "queue"
            ScopedProviderRegistryStore(tmp_path).workspace_store.save(
                ProviderRegistry(active="fixture", providers={"fixture": profile}), expected="0" * 64
            )
            async with client_app(tmp_path, config=configured) as (app, client, headers, _):
                state = app.state.web
                root = state.selected_session_id
                other = await state.harness.new_session()
                await state.harness.resume(root)
                bridge = MainAgentBridge(state, root)
                bridge.attach(append)
                try:
                    if typed_first:
                        first = await client.post(
                            "/api/messages",
                            headers=headers,
                            json={
                                "session_id": root,
                                "message_id": "22222222-2222-2222-2222-222222222222",
                                "prompt": "voice A",
                            },
                        )
                        assert first.status_code == 200
                    else:
                        await bridge.handle_request(ClientDelegationRequest("a", "voice A"))
                    async with asyncio.timeout(HANG_GUARD):
                        await streaming.wait()
                    active = state.active
                    assert active is not None and active.voice is not typed_first
                    result = await client.post(
                        "/api/messages",
                        headers=headers,
                        json={
                            "session_id": root,
                            "message_id": "11111111-1111-1111-1111-111111111111",
                            "prompt": "typed B",
                        },
                    )
                    assert result.status_code == 200, result.text
                    await state.queued_inputs.submit(other, "other-root", "other root")
                    await bridge.handle_request(ClientDelegationRequest("c", "voice C"))
                    assert state.active is active and not active.task.done()
                    respond.set()
                    async with asyncio.timeout(HANG_GUARD):
                        await finished.wait()
                    assert seen == ["A", "B", "C", "other"]
                    rows = await state.history.snapshot(root)
                    users = [row for row in rows if row["role"] == "user"]
                    assert [row["content"] for row in users] == ["voice A", "typed B", "voice C"]
                    assert users[1]["message_id"] == "11111111-1111-1111-1111-111111111111"
                    assert all(
                        users[index]["voice_verified"] and users[index]["message_id"]
                        for index in ((2,) if typed_first else (0, 2))
                    )
                finally:
                    respond.set()
                    await bridge.close()

    asyncio.run(scenario())


def test_queued_voice_survives_restart_with_display_and_inbox_receipt(tmp_path: Path) -> None:
    async def scenario() -> None:
        spoken: list[str] = []
        completed = asyncio.Event()

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            spoken.append(content)
            return f"session.{kind}.append"

        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            state.mutating = True
            root = state.selected_session_id
            bridge = MainAgentBridge(state, root)
            bridge.attach(append)
            await bridge.handle_request(ClientDelegationRequest("saved-request", "The caller's original words"))
            await bridge.close()
            before_restart = list(spoken)

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            assert "The caller's original words" in str(messages[-1].content)
            yield TextDoneEvent(text="Recovered queued work")
            completed.set()

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
                async with asyncio.timeout(HANG_GUARD):
                    await completed.wait()
                rows = await app.state.web.history.snapshot(root)
                user = next(row for row in rows if row["role"] == "user")
                assert user["content"] == "The caller's original words"
                assert user["voice_verified"] and user["voice_session_id"] == bridge.voice_session_id
                assert user["message_id"].startswith("voice-")
                assert spoken == before_restart

    asyncio.run(scenario())


@pytest.mark.parametrize("permanent", [False, True])
def test_deleted_chat_terminates_detached_queued_voice_and_releases_observer(tmp_path: Path, permanent: bool) -> None:
    async def scenario() -> None:
        sent: list[str] = []
        reports: list[dict[str, object]] = []

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            sent.append(content)
            return f"session.{kind}.append"

        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            state.mutating = True
            root = state.selected_session_id
            other = await state.harness.new_session()
            await state.harness.resume(root)
            bridge = MainAgentBridge(state, root, report=reports.append)
            other_bridge = MainAgentBridge(state, other)
            bridge.attach(append)
            other_bridge.attach(append)
            try:
                await bridge.handle_request(ClientDelegationRequest("never-executed", "Queued request"))
                await other_bridge.handle_request(ClientDelegationRequest("other-request", "Other queued request"))
                await bridge.close()
                await other_bridge.close()
                assert bridge.updates.observe in state.run_observers
                assert other_bridge.updates.observe in state.run_observers
                before_delete = list(sent)
                state.mutating = False
                # delete_session owns the idle reservation before its first await;
                # the channel worker cannot claim either queued request meanwhile.
                await delete_session(state, root, permanent=permanent)
                state.mutating = True
                assert state.active is None
                assert bridge.updates.observe not in state.run_observers
                assert other_bridge.updates.observe in state.run_observers
                assert not bridge._pending_requests
                assert other_bridge._pending_requests == {"other-request"}
                assert [record["status"] for record in reports if "status" in record] == ["queued", "cancelled"]
                assert sent == before_delete
                rows = await state.channels.store._transaction(
                    lambda db: db.execute("SELECT status FROM ngn_web_inbox WHERE session_id = ?", (root,)).fetchall()
                )
                assert rows == ([] if permanent else [("interrupted",)])
                assert await bridge.handle_request(ClientDelegationRequest("late", "Must not run")) == ""
                if not permanent:
                    trash = (await state.trash.snapshot())["items"]
                    assert isinstance(trash, list) and len(trash) == 1
                    state.mutating = False
                    await state.trash.restore(root, str(trash[0]["deletion_id"]))
                    state.mutating = True
                    assert bridge.updates.observe not in state.run_observers
                    assert not bridge._pending_requests
                    assert sent == before_delete
            finally:
                await bridge.close()
                await other_bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_followup_claim_cancellation_or_finished_owner_never_strands_running_row(tmp_path: Path, cancel: bool) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            state.mutating = True
            root = state.selected_session_id
            await state.channels.store.web(root, "pending-input", "queued text")
            run = Run(root, server_owned=True)
            state.active = run
            claimed, release = asyncio.Event(), asyncio.Event()
            original = state.channels.store.claim_work

            async def delayed(*, web_only: bool = False, session_id: str = "") -> Work | None:
                work: Work | None = await original(web_only=web_only, session_id=session_id)
                claimed.set()
                await release.wait()
                return work

            with patch.object(state.channels.store, "claim_work", delayed):
                task = asyncio.create_task(state.queued_inputs.pull(run))
                try:
                    async with asyncio.timeout(HANG_GUARD):
                        await claimed.wait()
                        if cancel:
                            task.cancel()
                        else:
                            run.finished = True
                            state.active = None
                        release.set()
                        if cancel:
                            with pytest.raises(asyncio.CancelledError):
                                await task
                        else:
                            assert await task is None
                    status = await state.channels.store._transaction(
                        lambda db: db.execute(
                            "SELECT status FROM ngn_web_inbox WHERE message_id = 'pending-input'"
                        ).fetchone()[0]
                    )
                    assert status == "queued"
                finally:
                    state.active = None
                    release.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
