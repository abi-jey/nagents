"""The engine routes use the real shared session store and Harness dispatcher."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

import aiohttp
import httpx
import pytest
from starlette.websockets import WebSocket

from nagents.engine import journal
from nagents.engine.lifecycle import create_engine_app
from nagents.engine.registration import REGISTRATION_NAME
from nagents.engine.runtime import EngineRuntime
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.harness.runtime import core
from nagents.protocol import EVENTS_SUBPROTOCOL
from nagents.protocol.events import ApprovalRequested
from nagents.protocol.events import ApprovalResolved
from nagents.protocol.events import EngineConnected
from nagents.protocol.events import EventRecord
from nagents.protocol.events import History
from nagents.protocol.events import RunFinished
from nagents.protocol.events import RunQueued
from nagents.protocol.events import RunStarted
from nagents.protocol.events import SessionUpdated
from nagents.protocol.events import SteeringUpdated
from nagents.protocol.events import TaskFinished
from nagents.protocol.events import TaskStarted
from nagents.protocol.events import TextCompleted
from nagents.protocol.events import TextDelta
from nagents.protocol.events import ToolCompleted
from nagents.protocol.models import Approval
from nagents.protocol.models import EngineSnapshot
from nagents.protocol.models import InputReceipt
from nagents.protocol.models import QueueState
from nagents.protocol.models import Session
from nagents.protocol.models import SteeringReceipt
from nagents.protocol.models import SteeringState
from nagents.protocol.models import TaskInfo
from tests.engine.test_lifecycle import _launch
from tests.engine.test_lifecycle import _registered
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.web.execution.test_web_subscription_lifecycle import Peer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from starlette.types import Message as WireMessage

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.execution.service import WebState
    from nagents.web.http.routing import Work
    from tests.support.providers import Script

pytestmark = pytest.mark.requires_posix
BASE = "/api/v1"
AUTH = {"Authorization": "Bearer engine-runtime-test"}


@asynccontextmanager
async def _app(
    root: Path, *, config: HarnessConfig | None = None
) -> AsyncIterator[tuple[httpx.AsyncClient, WebState, EngineRuntime]]:
    backends: list[EngineRuntime] = []

    def factory(get_state: Callable[[], WebState]) -> EngineRuntime:
        runtime = EngineRuntime(get_state)
        backends.append(runtime)
        return runtime

    app = create_engine_app(
        config or HarnessConfig(workspace=root, data_dir=root / "state", demo=True),
        port=32124,
        token="engine-runtime-test",
        backend_factory=factory,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 42002))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:32124", headers=AUTH) as http,
    ):
        yield http, app.state.web, backends[0]


def _scripted(workspace: Path, monkeypatch: pytest.MonkeyPatch, script: Script) -> HarnessConfig:
    providers: list[FakeProvider] = []

    def factory(config: HarnessConfig, login_store: object = None) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(core, "HarnessProvider", factory)
    monkeypatch.setattr(core, "build_provider", lambda profile, config, auth: factory(config))
    monkeypatch.setenv("OPENAI_API_KEY", "engine-fixture-key-do-not-expose")
    entries = connection(name="fixture", auth="api-key")
    store = ScopedProviderRegistryStore(workspace).workspace_store
    store.save(ProviderRegistry(active="fixture", providers=entries), expected=store.load().revision)
    return HarnessConfig(
        workspace=workspace, data_dir=workspace / "state", provider="fixture", providers=entries, model="fake-model"
    )


async def _create(http: httpx.AsyncClient, title: str = "") -> Session:
    response = await http.post(f"{BASE}/sessions", json={"title": title} if title else {})
    assert response.status_code == 201, response.text
    return Session.model_validate(response.json())


def _input(prompt: str, *, interactive: bool = False, mode: str = "queue") -> dict[str, object]:
    return {"prompt": prompt, "interactive": interactive, "mode": mode, "request_id": str(uuid4())}


async def _start(http: httpx.AsyncClient, root: str, body: dict[str, object]) -> InputReceipt:
    response = await http.post(f"{BASE}/sessions/{root}/runs", json=body)
    assert response.status_code == 202, response.text
    return InputReceipt.model_validate(response.json())


async def _history(http: httpx.AsyncClient, root: str, **query: int) -> History:
    response = await http.get(f"{BASE}/sessions/{root}/history", params=query)
    assert response.status_code == 200, response.text
    return History.model_validate(response.json())


async def _terminal(http: httpx.AsyncClient, root: str, run_id: str) -> History:
    async with asyncio.timeout(HANG_GUARD):
        while True:
            history = await _history(http, root)
            if any(item.run_id == run_id and isinstance(item.event, RunFinished) for item in history.events):
                return history
            await asyncio.sleep(0.01)


async def _pending(http: httpx.AsyncClient, run_id: str) -> Approval:
    async with asyncio.timeout(HANG_GUARD):
        while True:
            snapshot = EngineSnapshot.model_validate((await http.get(f"{BASE}/state")).json())
            for approval in snapshot.approvals:
                if approval.run_id == run_id:
                    return approval
            await asyncio.sleep(0.01)


def test_session_edits_are_persistent_independent_and_use_safe_config(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, state, _):
            selected = state.selected_session_id
            config = (await http.get(f"{BASE}/config")).json()
            assert set(config) == {"workspace", "provider", "model", "read_only"}
            assert config["workspace"] == str(tmp_path) and config["provider"] == "demo"
            assert (await http.get(f"{BASE}/providers")).json() == [{"id": "demo", "kind": "demo"}]
            assert (await http.get(f"{BASE}/providers/demo/models")).json() == [
                {"id": config["model"], "provider_id": "demo", "label": config["model"]}
            ]
            assert (await http.get(f"{BASE}/providers/unknown/models")).status_code == 404

            initial = await _create(http, "First")
            default = await _create(http)
            assert default.title == "New session" and not default.archived and default.updated_at_ms > 0
            assert (await http.get(f"{BASE}/sessions/{initial.id}")).json() == initial.model_dump()
            assert (await http.get(f"{BASE}/sessions/ngn-missing")).status_code == 404
            assert (await http.get(f"{BASE}/sessions/ngn-missing/history")).status_code == 404
            renaming = await http.post(f"{BASE}/sessions/{initial.id}/rename", json={"title": "First edited"})
            assert renaming.json()["title"] == "First edited"

            receipt = await _start(http, initial.id, _input("An independent conversation"))
            assert receipt.run_id is not None
            await _terminal(http, initial.id, receipt.run_id)
            forked = await http.post(f"{BASE}/sessions/{initial.id}/fork", json={})
            assert forked.status_code == 201, forked.text
            copied = Session.model_validate(forked.json())
            assert copied.title == "First edited · fork 1" and copied.id != initial.id
            null_title = await http.post(f"{BASE}/sessions/{initial.id}/fork", json={"title": None})
            assert null_title.status_code == 201 and null_title.json()["title"] == "New session"
            assert (
                await http.post(f"{BASE}/sessions/{initial.id}/fork", json={"title": "independent snapshot"})
            ).json()["title"] == "independent snapshot"
            assert any(
                row["content"] == "An independent conversation" for row in await state.history.snapshot(copied.id)
            )
            assert state.selected_session_id == state.harness.session_id == selected

            first = await _history(http, initial.id, limit=1)
            assert first.has_more and isinstance(first.events[0].event, SessionUpdated)
            following = await _history(http, initial.id, after=first.cursor, limit=1)
            assert following.cursor > first.cursor and following.events[0].event.type == "session.updated"

        async with _app(tmp_path) as (http, _, _):
            assert (await http.get(f"{BASE}/sessions/{default.id}")).json() == default.model_dump()
            assert (await http.get(f"{BASE}/sessions/{copied.id}")).json() == copied.model_dump()
            assert (await _history(http, initial.id, limit=1)).events == first.events

    asyncio.run(scenario())


def test_durable_admission_uses_scoped_idempotency_and_real_queue_controls(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, _, _):
            one, two = await _create(http, "One"), await _create(http, "Two")
            assert (await http.post(f"{BASE}/sessions/{one.id}/queue", json={"action": "pause"})).json()["paused"]
            body = _input("Retain this input")
            lookup = {key: body[key] for key in ("prompt", "request_id")}
            missing = await http.post(f"{BASE}/sessions/{one.id}/receipts", json=lookup)
            assert missing.status_code == 200 and missing.json() is None
            assert (await http.get(f"{BASE}/sessions/{one.id}/queue")).json()["entries"] == []
            left, right = await asyncio.gather(_start(http, one.id, body), _start(http, one.id, body))
            assert left == right and left.status == "queued" and left.run_id is not None
            assert await _start(http, one.id, {**body, "request_id": str(body["request_id"]).upper()}) == left
            known = await http.post(f"{BASE}/sessions/{one.id}/receipts", json=lookup)
            assert known.status_code == 200 and InputReceipt.model_validate(known.json()) == left
            assert (await http.post(f"{BASE}/sessions/{two.id}/receipts", json=lookup)).json() is None
            conflict = await http.post(f"{BASE}/sessions/{one.id}/receipts", json={**lookup, "prompt": "Wrong prompt"})
            assert conflict.status_code == 409
            for changed in (
                {**body, "prompt": "Other prompt"},
                {**body, "interactive": True},
                {**body, "mode": "idle"},
            ):
                assert (await http.post(f"{BASE}/sessions/{one.id}/runs", json=changed)).status_code == 409
            assert (
                await http.post(
                    f"{BASE}/sessions/{one.id}/runs", json=_input("Idle must not bypass pause", mode="idle")
                )
            ).status_code == 409
            independent = await _start(http, two.id, {**body, "mode": "idle"})
            assert independent.run_id is not None and independent.run_id != left.run_id
            await _terminal(http, two.id, independent.run_id)

            queued = QueueState.model_validate((await http.get(f"{BASE}/sessions/{one.id}/queue")).json())
            assert queued.paused and [item.run_id for item in queued.entries] == [left.run_id]
            cancelled = await http.post(f"{BASE}/runs/{left.run_id}/cancel")
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
            assert (await _start(http, one.id, body)).status == "cancelled"
            assert (await http.post(f"{BASE}/sessions/{one.id}/receipts", json=lookup)).json()["status"] == "cancelled"
            pending_body = _input("Clear me")
            pending = await _start(http, one.id, pending_body)
            assert pending.status == "queued"
            cleared = await http.post(f"{BASE}/sessions/{one.id}/queue", json={"action": "clear"})
            assert cleared.status_code == 200 and cleared.json()["paused"] and cleared.json()["entries"] == []
            assert (await _start(http, one.id, pending_body)).status == "cancelled"
            entries = (await _history(http, one.id)).events
            assert [item.run_id for item in entries if isinstance(item.event, RunQueued)] == [
                left.run_id,
                pending.run_id,
            ]
            assert not any(isinstance(item.event, RunStarted) for item in entries)
            assert (await _start(http, two.id, {**body, "mode": "idle"})).status == "completed"
            assert (await http.post(f"{BASE}/runs/run-missing/cancel")).status_code == 404

    asyncio.run(scenario())


def test_approval_nonce_controls_the_real_tool_only_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, _, _):
            root = (await _create(http, "Approval")).id
            admitted = await _start(http, root, _input("demo approval", interactive=True))
            assert admitted.run_id is not None
            approval = await _pending(http, admitted.run_id)
            assert approval.root_session_id == approval.session_id == root
            assert approval.call.name == "demo_preview"
            path = f"{BASE}/approvals/{approval.id}"
            allow = {"run_id": admitted.run_id, "call_id": approval.call.call_id, "allow": True}
            assert (await http.post(f"{BASE}/approvals/not-the-nonce", json=allow)).status_code == 409
            assert (await http.post(path, json={**allow, "call_id": "other-call"})).status_code == 409
            assert (await http.post(path, json={**allow, "run_id": "other-run"})).status_code == 409
            decisions = await asyncio.gather(http.post(path, json=allow), http.post(path, json=allow))
            assert sorted(item.status_code for item in decisions) == [200, 409]
            events = (await _terminal(http, root, admitted.run_id)).events
            assert any(
                isinstance(item.event, ApprovalRequested) and item.event.approval_id == approval.id for item in events
            )
            assert any(
                isinstance(item.event, ApprovalResolved)
                and item.event.approval_id == approval.id
                and item.event.allowed
                for item in events
            )
            assert any(
                isinstance(item.event, ToolCompleted)
                and item.event.result.call_id == approval.call.call_id
                and isinstance(item.event.result.result, dict)
                and item.event.result.result.get("approved") is True
                and item.event.result.result.get("preview_only") is True
                for item in events
            )
            assert (await http.get(f"{BASE}/state")).json() == {"runs": [], "approvals": []}

            unattended = await _start(http, root, _input("demo approval"))
            assert unattended.run_id is not None
            history = await _terminal(http, root, unattended.run_id)
            other = [entry.event for entry in history.events if entry.run_id == unattended.run_id]
            assert not any(isinstance(event, ApprovalRequested | ApprovalResolved) for event in other)
            assert any(
                isinstance(event, ToolCompleted)
                and event.result.name == "demo_preview"
                and isinstance(event.result.result, dict)
                and event.result.result.get("approved") is False
                for event in other
            )

    asyncio.run(scenario())


def test_cancel_active_joins_the_claim_and_holds_following_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, _, _):
            root = (await _create(http, "Cancel")).id
            active = await _start(http, root, _input("demo approval", interactive=True))
            assert active.run_id is not None
            awaiting = await _pending(http, active.run_id)
            after = await _start(http, root, _input("still queued"))
            assert after.run_id is not None
            stopped = await http.post(f"{BASE}/runs/{active.run_id}/cancel")
            assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
            assert (await http.get(f"{BASE}/state")).json() == {"runs": [], "approvals": []}
            stale = {"run_id": active.run_id, "call_id": awaiting.call.call_id, "allow": True}
            assert (await http.post(f"{BASE}/approvals/{awaiting.id}", json=stale)).status_code == 409
            queue = QueueState.model_validate((await http.get(f"{BASE}/sessions/{root}/queue")).json())
            assert queue.paused and [item.run_id for item in queue.entries] == [after.run_id]
            await http.post(f"{BASE}/sessions/{root}/queue", json={"action": "resume"})
            history = await _terminal(http, root, after.run_id)
            assert [item.run_id for item in history.events if isinstance(item.event, RunStarted)] == [
                active.run_id,
                after.run_id,
            ]

    asyncio.run(scenario())


def test_stream_merges_strict_live_ids_and_resumes_only_durable_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, _, runtime):
            one, two = await _create(http, "Filtered"), await _create(http, "Other")
            peer = Peer()
            connected = asyncio.create_task(runtime.events(peer.socket, 0, one.id))
            assert (await peer.message())["subprotocol"] == EVENTS_SUBPROTOCOL
            first, second = await asyncio.gather(
                _start(http, one.id, _input("Stream a response")),
                _start(http, two.id, _input("Separate stream")),
            )
            assert first.run_id is not None and second.run_id is not None
            received: list[EventRecord] = []
            try:
                async with asyncio.timeout(HANG_GUARD):
                    while True:
                        frame = EventRecord.model_validate(await peer.frame())
                        received.append(frame)
                        if isinstance(frame.event, RunFinished):
                            assert frame.run_id == first.run_id
                            break
            finally:
                peer.disconnect()
                await connected
            ids = [item.sequence for item in received]
            assert ids == sorted(set(ids))
            assert {item.session_id for item in received} == {one.id}
            assert isinstance(received[0].event, SessionUpdated)
            assert any(isinstance(item.event, EngineConnected) for item in received)
            deltas = [item.sequence for item in received if isinstance(item.event, TextDelta)]
            assert deltas
            assert any(isinstance(item.event, TextCompleted) for item in received)
            await _terminal(http, two.id, second.run_id)
            durable = await _history(http, one.id)
            assert durable.events == [
                item for item in received if not isinstance(item.event, TextDelta | EngineConnected)
            ]

            resumed = Peer()
            replay = asyncio.create_task(runtime.events(resumed.socket, deltas[0], ""))
            assert (await resumed.message())["type"] == "websocket.accept"
            other = await _history(http, two.id)
            expected = sorted(
                [item for item in [*durable.events, *other.events] if item.sequence > deltas[0]],
                key=lambda item: item.sequence,
            )
            try:
                assert [EventRecord.model_validate(await resumed.frame()) for _item in expected] == expected
                attached = EventRecord.model_validate(await resumed.frame())
                assert isinstance(attached.event, EngineConnected) and attached.sequence > expected[-1].sequence
                assert attached.session_id == "" and attached.run_id == ""
            finally:
                resumed.disconnect()
                await replay

    asyncio.run(scenario())


def test_slow_event_client_reconnects_at_a_bounded_live_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal, "_LIVE_ENTRIES", 2)

    async def scenario() -> None:
        async with _app(tmp_path) as (http, _, runtime):
            root = (await _create(http, "Bounded stream")).id
            peer = Peer()
            blocked, release = asyncio.Event(), asyncio.Event()

            async def send(message: WireMessage) -> None:
                if message["type"] == "websocket.send" and not blocked.is_set():
                    blocked.set()
                    await release.wait()
                await peer.outgoing.put(message)

            socket = WebSocket({"type": "websocket"}, peer.incoming.get, send)
            stream = asyncio.create_task(runtime.events(socket, 0, root))
            try:
                assert (await peer.message())["type"] == "websocket.accept"
                await asyncio.wait_for(blocked.wait(), HANG_GUARD)
                input_receipt = await _start(http, root, _input("Add bounded transient events"))
                assert input_receipt.run_id is not None
                history = await _terminal(http, root, input_receipt.run_id)
                release.set()
                first = EventRecord.model_validate(await peer.frame())
                close = await peer.message()
                assert close["type"] == "websocket.close" and close["code"] == 1013
                assert first == history.events[0]
            finally:
                release.set()
                peer.disconnect()
                await stream
            recovered = await _history(http, root, after=first.sequence)
            assert recovered.events == history.events[1:]
            assert any(isinstance(event.event, TextCompleted) for event in recovered.events)

    asyncio.run(scenario())


def test_real_workspace_relative_reads_and_noninteractive_tool_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "workspace" / "source"
    nested.mkdir(parents=True)
    root = nested.parent
    (nested / "visible.txt").write_text("an in-scope line\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("must not reach the model\n", encoding="utf-8")
    custom_calls: list[bool] = []

    async def custom() -> str:
        """A test side effect that must not be approved unattended."""
        custom_calls.append(True)
        return "not permitted"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if messages[-1].role != "tool":
            yield ToolCallEvent(id="relative-read", name="read_file", arguments={"path": "source/visible.txt"})
            yield ToolCallEvent(id="outside-read", name="read_file", arguments={"path": "../outside.txt"})
            yield ToolCallEvent(id="write", name="write", arguments={"path": "blocked.txt", "content": "blocked"})
            yield ToolCallEvent(id="shell", name="shell", arguments={"command": "printf harmless > shell-blocked.txt"})
            yield ToolCallEvent(id="custom", name="custom", arguments={})
        else:
            yield TextDoneEvent(text="Local tool checks finished.")

    config = _scripted(root, monkeypatch, script)

    async def scenario() -> None:
        async with _app(root, config=config) as (http, state, _):
            state.harness.agent.register_tool(custom)
            grant = state.tool_approvals.binding(state.harness, "shell")
            assert grant is not None
            state.tool_approvals.grant(grant)  # A prior browser allow rule must not bypass the v1 input.
            session = (await _create(http, "Tool boundaries")).id
            for url in (f"{BASE}/config", f"{BASE}/providers", f"{BASE}/providers/fixture/models"):
                response = await http.get(url)
                assert response.is_success and "engine-fixture-key-do-not-expose" not in response.text
            receipt = await _start(http, session, _input("Read and try three approval-gated actions"))
            assert receipt.run_id is not None
            events = (await _terminal(http, session, receipt.run_id)).events
            assert not any(isinstance(item.event, ApprovalRequested) for item in events)
            results = {
                item.event.result.call_id: item.event.result for item in events if isinstance(item.event, ToolCompleted)
            }
            read = results["relative-read"].result
            assert (
                isinstance(read, dict)
                and read["path"] == "source/visible.txt"
                and read["content"] == "1: an in-scope line"
            )
            assert "Path traversal" in results["outside-read"].error
            assert "must not reach the model" not in str(results["outside-read"].result)
            assert all("Approval denied" in results[call].error for call in ("write", "shell", "custom"))
            assert (
                not custom_calls and not (root / "blocked.txt").exists() and not (root / "shell-blocked.txt").exists()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_child", [False, True])
def test_child_approval_and_history_keep_exact_actor_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_child: bool
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index == 0:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="delegate", name="to_agent", arguments={"prompt": "child text", "title": "Child actor"}
                )
            else:
                yield TextDoneEvent(text="Root-only final response")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child-shell", name="shell", arguments={"command": "printf child-preview"})
        else:
            yield TextChunkEvent(chunk="child-only unrelated transcript")
            yield TextDoneEvent(text="child-only unrelated transcript")

    config = _scripted(tmp_path, monkeypatch, script)

    async def scenario() -> None:
        async with _app(tmp_path, config=config) as (http, _, _):
            root = (await _create(http, "Root actor")).id
            receipt = await _start(http, root, _input("Request child participation", interactive=True))
            assert receipt.run_id is not None
            async with asyncio.timeout(HANG_GUARD):
                while True:
                    response = await http.get(f"{BASE}/sessions/{root}/tasks")
                    assert response.status_code == 200, response.text
                    if response.json():
                        break
                    await asyncio.sleep(0.01)
            child = TaskInfo.model_validate(response.json()[0])
            assert child.parent_session_id == root and child.parent_run_id == receipt.run_id
            assert child.root_session_id == root and child.root_run_id == receipt.run_id
            assert child.call_id == "delegate" and child.depth == 1
            pending = await _pending(http, child.run_id)
            assert pending.session_id != root and pending.root_session_id == root
            assert pending.session_id == child.session_id
            assert pending.depth == 1 and pending.call.call_id == "child-shell"
            path = f"{BASE}/approvals/{pending.id}"
            decision = {"run_id": child.run_id, "call_id": pending.call.call_id, "allow": False}
            assert (await http.post(path, json={**decision, "run_id": receipt.run_id})).status_code == 409
            if cancel_child:
                stopped = await http.post(f"{BASE}/runs/{child.run_id}/cancel")
                assert stopped.status_code == 200, stopped.text
                assert stopped.json()["status"] == "cancelled"
                assert (await http.post(path, json=decision)).status_code == 409
            else:
                assert (await http.post(path, json=decision)).status_code == 200
            durable = await _terminal(http, root, receipt.run_id)
            text = [item.event.text for item in durable.events if isinstance(item.event, TextCompleted)]
            assert text and "child-only unrelated transcript" not in text
            assert any(isinstance(item.event, TaskStarted) and item.event.task == child for item in durable.events)
            assert any(
                isinstance(item.event, TaskFinished) and item.event.task.run_id == child.run_id
                for item in durable.events
            )
            records = await _history(http, child.session_id)
            assert all(item.session_id == child.session_id and item.run_id == child.run_id for item in records.events)
            final = records.events[-1].event
            assert isinstance(final, RunFinished)
            assert final.status == ("cancelled" if cancel_child else "completed")
            assert (
                any(
                    isinstance(item.event, TextCompleted) and item.event.text == "child-only unrelated transcript"
                    for item in records.events
                )
                is not cancel_child
            )
            assert "tasks" in (await http.get(f"{BASE}/info")).json()["capabilities"]
            copied = await http.post(f"{BASE}/sessions/{root}/fork", json={})
            assert copied.status_code == 201, copied.text
            fork_id = copied.json()["id"]
            fork = await _history(http, fork_id)
            assert any(isinstance(item.event, TextCompleted) for item in fork.events)
            assert all(
                item.session_id == fork_id
                and item.run_id != receipt.run_id
                and not isinstance(item.event, ApprovalRequested)
                for item in fork.events
            )
            assert (await http.get(f"{BASE}/sessions/{fork_id}/tasks")).json() == []

    asyncio.run(scenario())


def test_stopping_nested_activation_only_joins_its_own_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release_sibling = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            branch = next(message.content for message in messages if message.role == "user")
            if branch == "Root" and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Parent", "title": "Parent"})
                yield ToolCallEvent(id="b", name="to_agent", arguments={"prompt": "Sibling", "title": "Sibling"})
            elif branch == "Parent" and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="nested", name="to_agent", arguments={"prompt": "Grandchild", "title": "Grandchild"}
                )
            elif branch == "Parent":
                await asyncio.Event().wait()
            elif branch == "Grandchild":
                yield ToolCallEvent(
                    id="child-write", name="write", arguments={"path": "never-write.txt", "content": "fixture"}
                )
            elif branch == "Sibling":
                await release_sibling.wait()
                yield TextDoneEvent(text="Independent sibling finished")
            else:
                yield TextDoneEvent(text="Root remained available")

        async with _app(tmp_path, config=_scripted(tmp_path, monkeypatch, script)) as (http, _, _):
            root = (await _create(http, "Root actor")).id
            receipt = await _start(http, root, _input("Root", interactive=True))
            assert receipt.run_id is not None

            async def listed() -> dict[str, TaskInfo]:
                response = await http.get(f"{BASE}/sessions/{root}/tasks")
                assert response.status_code == 200, response.text
                return {task.title: task for record in response.json() if (task := TaskInfo.model_validate(record))}

            async with asyncio.timeout(HANG_GUARD):
                while len(tasks := await listed()) < 3:
                    await asyncio.sleep(0.01)
            parent, grandchild, sibling = (tasks[name] for name in ("Parent", "Grandchild", "Sibling"))
            assert grandchild.parent_run_id == parent.run_id
            assert grandchild.parent_session_id == parent.session_id
            assert grandchild.depth == 2 and grandchild.root_run_id == receipt.run_id
            approval = await _pending(http, grandchild.run_id)
            stopped = await http.post(f"{BASE}/runs/{parent.run_id}/cancel")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "cancelled"
            tasks = await listed()
            assert tasks["Sibling"].run_id == sibling.run_id and tasks["Sibling"].status == "running"
            assert tasks["Grandchild"].status in ("interrupted", "cancelled")
            rejected = await http.post(
                f"{BASE}/approvals/{approval.id}",
                json={"run_id": grandchild.run_id, "call_id": approval.call.call_id, "allow": True},
            )
            assert rejected.status_code == 409 and not (tmp_path / "never-write.txt").exists()
            release_sibling.set()
            history = await _terminal(http, root, receipt.run_id)
            assert all(
                item.run_id == receipt.run_id
                for item in history.events
                if isinstance(item.event, TaskStarted | TaskFinished)
            )
            parent_history = await _history(http, grandchild.session_id)
            assert sum(isinstance(item.event, RunFinished) for item in parent_history.events) == 1
            assert (await listed())["Sibling"].status == "completed"

    asyncio.run(scenario())


def test_steering_is_a_durable_followup_of_only_the_chosen_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, state, _):
            one, two = await _create(http, "One"), await _create(http, "Two")
            active, other = await asyncio.gather(
                _start(http, one.id, _input("demo approval", interactive=True)),
                _start(http, two.id, _input("demo approval", interactive=True)),
            )
            assert active.run_id is not None and other.run_id is not None
            pending, other_pending = await asyncio.gather(_pending(http, active.run_id), _pending(http, other.run_id))
            next_run = await _start(http, one.id, _input("an ordinary run queued first"))
            assert next_run.run_id is not None
            path = f"{BASE}/runs/{active.run_id}/steer"
            body = {"request_id": str(uuid4()), "prompt": "follow up only within this run"}
            current = SteeringState.model_validate((await http.get(path)).json())
            assert current.supported and current.accepting and not current.receipts
            admitted = await http.post(path, json=body)
            assert admitted.status_code < 300, admitted.text
            input_receipt = SteeringReceipt.model_validate(admitted.json())
            assert input_receipt.run_id == active.run_id and input_receipt.status == "pending"
            assert (await http.post(path, json=body)).json() == input_receipt.model_dump()
            assert (await http.post(path, json={**body, "prompt": "wrong body"})).status_code == 409
            assert (await http.get(f"{BASE}/runs/{other.run_id}/steer")).json()["receipts"] == []
            assert (await http.get(f"{BASE}/sessions/{one.id}/queue")).json()["entries"][0]["run_id"] == next_run.run_id

            decision = {"run_id": active.run_id, "call_id": pending.call.call_id, "allow": False}
            assert (await http.post(f"{BASE}/approvals/{pending.id}", json=decision)).status_code == 200
            await _terminal(http, one.id, next_run.run_id)
            saved = SteeringState.model_validate((await http.get(path)).json())
            assert saved.run_status == "completed" and not saved.accepting
            assert len(saved.receipts) == 1 and saved.receipts[0].status == "applied"
            assert (await http.post(path, json=body)).json() == saved.receipts[0].model_dump()
            assert (await http.post(path, json={**body, "request_id": str(uuid4())})).status_code == 409
            messages = [row["content"] for row in await state.history.snapshot(one.id) if row["role"] == "user"]
            assert messages == ["demo approval", body["prompt"], "an ordinary run queued first"]
            assert all(row["content"] != body["prompt"] for row in await state.history.snapshot(two.id))
            changes = [
                item
                for item in (await _history(http, one.id)).events
                if isinstance(item.event, SteeringUpdated) and item.event.input_id == input_receipt.input_id
            ]
            assert len(changes) == 3 and {item.run_id for item in changes} == {active.run_id}
            stale_other = {"run_id": other.run_id, "call_id": other_pending.call.call_id, "allow": False}
            assert (await http.post(f"{BASE}/approvals/{other_pending.id}", json=stale_other)).status_code == 200
            await _terminal(http, two.id, other.run_id)

    asyncio.run(scenario())


def test_steering_commits_both_fifo_inputs_before_the_next_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[list[Message]] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        requests.append([message for message in messages if message.role != "system"])
        if len(requests) == 1:
            for index in range(2):
                yield ToolCallEvent(
                    id=f"write-{index}",
                    name="write",
                    arguments={"path": f"patch-{index}.txt", "content": f"result {index}\n"},
                )
        else:
            yield TextDoneEvent(text="Both follow-ups arrived after both tools.")

    config = _scripted(tmp_path, monkeypatch, script)

    async def scenario() -> None:
        async with _app(tmp_path, config=config) as (http, _, _):
            session = (await _create(http, "Batched steering")).id
            receipt = await _start(http, session, _input("Make two approved changes", interactive=True))
            assert receipt.run_id is not None
            path = f"{BASE}/runs/{receipt.run_id}/steer"
            first = await _pending(http, receipt.run_id)
            assert first.call.call_id == "write-0"
            prompts = ["First in the batch", "Second in the batch"]
            for prompt in prompts:
                response = await http.post(path, json={"request_id": str(uuid4()), "prompt": prompt})
                assert response.is_success and response.json()["status"] == "pending"

            for expected, approval in (("write-0", first), ("write-1", None)):
                approval = approval or await _pending(http, receipt.run_id)
                assert approval.call.call_id == expected
                assert [item["status"] for item in (await http.get(path)).json()["receipts"]] == ["pending"] * 2
                assert len(requests) == 1
                decision = {"run_id": receipt.run_id, "call_id": approval.call.call_id, "allow": True}
                assert (await http.post(f"{BASE}/approvals/{approval.id}", json=decision)).status_code == 200

            history = await _terminal(http, session, receipt.run_id)
            assert len(requests) == 2
            assert [message.role for message in requests[1]] == ["user", "assistant", "tool", "tool", "user", "user"]
            assert [message.content for message in requests[1][-2:]] == prompts
            state = SteeringState.model_validate((await http.get(path)).json())
            assert [item.status for item in state.receipts] == ["applied"] * 2
            assert [item.prompt for item in state.receipts] == prompts
            assert history.events[-1].event == RunFinished(status="completed", error="")
            assert [(tmp_path / f"patch-{index}.txt").read_text() for index in range(2)] == ["result 0\n", "result 1\n"]

    asyncio.run(scenario())


def test_cancelled_offered_batch_is_uncertain_for_each_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with _app(tmp_path) as (http, state, _):
            offered = asyncio.Event()

            async def before_commit(
                session_id: str, run_id: str, works: tuple[Work, ...], messages: tuple[Message, ...]
            ) -> None:
                assert len(works) == len(messages) == 2
                offered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(state.history, "commit_steering_batch", before_commit)
            root = (await _create(http, "Offered steering")).id
            receipt = await _start(http, root, _input("demo approval", interactive=True))
            assert receipt.run_id is not None
            approval = await _pending(http, receipt.run_id)
            path = f"{BASE}/runs/{receipt.run_id}/steer"
            for prompt in ("Unconfirmed first", "Unconfirmed second"):
                response = await http.post(path, json={"request_id": str(uuid4()), "prompt": prompt})
                assert response.is_success and response.json()["status"] == "pending"
            decision = {"run_id": receipt.run_id, "call_id": approval.call.call_id, "allow": False}
            assert (await http.post(f"{BASE}/approvals/{approval.id}", json=decision)).status_code == 200
            await asyncio.wait_for(offered.wait(), HANG_GUARD)
            assert [item["status"] for item in (await http.get(path)).json()["receipts"]] == ["offered"] * 2
            cancelled = await http.post(f"{BASE}/runs/{receipt.run_id}/cancel")
            assert cancelled.is_success and cancelled.json()["status"] == "cancelled"
            assert [item["status"] for item in (await http.get(path)).json()["receipts"]] == ["uncertain"] * 2
            assert not any(str(row["content"]).startswith("Unconfirmed") for row in await state.history.snapshot(root))

    asyncio.run(scenario())


def test_crash_interrupts_the_offered_call_and_holds_unstarted_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = tmp_path / "engine"
        path = directory / REGISTRATION_NAME
        first_body, next_body = _input("demo approval", interactive=True), _input("hold this input on restart")
        async with _launch(tmp_path, directory) as process:
            entry = await _registered(path, process)
            async with httpx.AsyncClient(
                base_url=entry.url, headers={"Authorization": f"Bearer {entry.token}"}, timeout=HANG_GUARD
            ) as http:
                root = (await _create(http, "Restart")).id
                active = await _start(http, root, first_body)
                assert active.run_id is not None
                pending = await _pending(http, active.run_id)
                before = await _history(http, root)
                assert any(isinstance(item.event, ApprovalRequested) for item in before.events)
                steer_path = f"{BASE}/runs/{active.run_id}/steer"
                steering = {"request_id": str(uuid4()), "prompt": "must not reach a later run"}
                assert (await http.post(steer_path, json=steering)).json()["status"] == "pending"
                queued = await _start(http, root, next_body)
                assert queued.status == "queued" and queued.run_id is not None
            process.kill()
            await asyncio.wait_for(process.communicate(), HANG_GUARD)

        async with _launch(tmp_path, directory) as process:
            entry = await _registered(path, process)
            headers = {"Authorization": f"Bearer {entry.token}"}
            async with httpx.AsyncClient(base_url=entry.url, headers=headers, timeout=HANG_GUARD) as http:
                recovered = await _start(http, root, first_body)
                assert recovered.status == "interrupted" and recovered.run_id == active.run_id
                paused = QueueState.model_validate((await http.get(f"{BASE}/sessions/{root}/queue")).json())
                assert paused.paused and [item.run_id for item in paused.entries] == [queued.run_id]
                assert (await http.get(f"{BASE}/state")).json() == {"runs": [], "approvals": []}
                assert (await http.post(steer_path, json=steering)).json()["status"] == "undelivered"
                stale = {"run_id": active.run_id, "call_id": pending.call.call_id, "allow": True}
                assert (await http.post(f"{BASE}/approvals/{pending.id}", json=stale)).status_code == 409
                async with (
                    aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HANG_GUARD)) as sockets,
                    sockets.ws_connect(
                        entry.url + f"{BASE}/events?session_id={root}&after={before.cursor}",
                        headers=headers,
                        protocols=[EVENTS_SUBPROTOCOL],
                    ) as socket,
                ):
                    assert socket.protocol == EVENTS_SUBPROTOCOL
                    replayed = (await _history(http, root, after=before.cursor)).events
                    assert [EventRecord.model_validate(await socket.receive_json()) for _item in replayed] == replayed
                    attached = EventRecord.model_validate(await socket.receive_json())
                    assert isinstance(attached.event, EngineConnected) and attached.sequence > replayed[-1].sequence
                await http.post(f"{BASE}/sessions/{root}/queue", json={"action": "resume"})
                items = (await _terminal(http, root, queued.run_id)).events
                assert [item.run_id for item in items if isinstance(item.event, RunStarted)] == [
                    active.run_id,
                    queued.run_id,
                ]
                assert [
                    item.event.status
                    for item in items
                    if item.run_id == active.run_id and isinstance(item.event, RunFinished)
                ] == ["interrupted"]
                assert not any(
                    isinstance(item.event, ToolCompleted) and item.event.result.call_id == pending.call.call_id
                    for item in items
                )
            process.terminate()
            await asyncio.wait_for(process.communicate(), HANG_GUARD)

    asyncio.run(scenario())
