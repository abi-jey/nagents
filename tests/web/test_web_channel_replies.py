"""Opt-in replies use real admission, persistence, approval and dispatch boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.types import ApprovalRequest
from nagents.types import ToolCall
from nagents.web.catalog import ConnectionInput
from nagents.web.catalog import SavedCatalog
from nagents.web.channel_host import ChannelHost
from nagents.web.service import Run
from tests.support.channels import site

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.routing import ChatOwner
    from tests.support.channels import Site
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


def automatic_events(app: Site) -> list[dict[str, object]]:
    return [
        record
        for frame, _ in app.state.bus.ring
        if isinstance((record := frame.get("record")), dict) and record.get("automatic") is True
    ]


def test_opted_in_original_list_and_send_reply_without_subscribers_only_to_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.configure("other-connection", auto_reply=True)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role != "tool":
                yield ToolCallEvent(id="inspect-source", name="channel_list", arguments={})
                yield ToolCallEvent(
                    id="reply",
                    name="channel_send",
                    arguments={
                        "channel": "fixture",
                        "destination": "chat-a",
                        "text": "Reply in the originating chat",
                        "thread_id": "origin-thread",
                        "reply_to": "remote-id",
                    },
                )
            else:
                yield TextDoneEvent(text="sent")

        app.providers[0].script = script
        app.emit("Please reply", thread="origin-thread")
        app.idle()
        assert not app.state.bus.subscribers
        assert app.state.selected_session_id == app.main  # Web selection is an unrelated root.
        assert len(app.channels[0].deliveries) == 1 and not app.channels[1].deliveries
        delivery = app.channels[0].deliveries[0]
        assert (delivery.destination, delivery.thread_id, delivery.reply_to) == ("chat-a", "origin-thread", "remote-id")
        records = automatic_events(app)
        assert [record["tool"] for record in records] == ["channel_list", "channel_send"]
        root = app.bindings()["chat-a"]
        assert all(
            record["event"] == "notice" and record["session_id"] == root and record["run_id"] for record in records
        )
        assert all("approval_id" not in record for record in records)
        history = app.history(root)["history"]
        assert isinstance(history, list)
        listing = next(row for row in history if row["role"] == "tool" and row["tool_call_id"] == "inspect-source")
        assert "other-connection" not in listing["content"]


@pytest.mark.parametrize(
    "case",
    [
        "opt-out",
        "disabled",
        "not-running",
        "source-stopped",
        "wrong-channel",
        "wrong-chat",
        "wrong-thread",
        "wrong-reply",
        "unowned",
        "conflicted",
        "source-spoof",
        "ingress-spoof",
        "missing-ingress",
        "unverified-history",
        "background",
        "replaced-definition",
        "mutated-callable",
        "foreign-bound-callable",
        "unknown-tool",
    ],
)
def test_auto_reply_never_grants_outside_verified_source_or_original_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=case != "opt-out")
        app.configure("other-connection", auto_reply=True)
        invoked: list[str] = []

        async def replacement(
            channel: str, destination: str, text: str, thread_id: str = "", reply_to: str = ""
        ) -> str:
            invoked.append("replacement")
            return "must require approval"

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role == "tool":
                yield TextDoneEvent(text="finished")
                return
            state = app.state
            active = state.active
            assert active is not None
            arguments = {"channel": "fixture", "destination": "chat-a", "text": "reply", "thread_id": "origin-thread"}
            tool = "channel_send"
            if case == "disabled":
                state.channels.catalog.connections["fixture"].enabled = False
            elif case == "not-running":
                state.channels.catalog.status["fixture"] = ("idle", "")
            elif case == "source-stopped":
                source = state.channels.sources["fixture"]
                source.cancel()
                await asyncio.gather(source, return_exceptions=True)
            elif case in {"wrong-channel", "wrong-chat", "wrong-thread", "wrong-reply"}:
                key, value = {
                    "wrong-channel": ("channel", "other-connection"),
                    "wrong-chat": ("destination", "chat-b"),
                    "wrong-thread": ("thread_id", "different-thread"),
                    "wrong-reply": ("reply_to", "different-message"),
                }[case]
                arguments[key] = value
            elif case in {"unowned", "conflicted", "unverified-history"}:

                def change(db: sqlite3.Connection) -> None:
                    if case == "unowned":
                        db.execute("DELETE FROM ngn_web_session_owners WHERE session_id = ?", (active.session_id,))
                    elif case == "conflicted":
                        db.execute(
                            "UPDATE ngn_web_session_owners SET conflicted = 1 WHERE session_id = ?",
                            (active.session_id,),
                        )
                    else:
                        db.execute("DELETE FROM ngn_web_message_origins")

                await state.channels.store._transaction(change)
            elif case == "source-spoof":
                active.source["conversation_id"] = "chat-b"
                arguments["destination"] = "chat-b"
            elif case == "ingress-spoof":
                ingress = state.history.ingress.get()
                assert ingress is not None
                ingress.work = replace(ingress.work, thread_id="forged-thread")
                active.source["thread_id"] = arguments["thread_id"] = "forged-thread"
            elif case == "missing-ingress":
                state.history.ingress.set(None)
            elif case == "background":
                active.background = True
            elif case == "replaced-definition":
                registry = state.harness.agent.tool_registry
                registry.unregister(tool)
                registry.register(state.channels.channel_send, name=tool)
            elif case == "mutated-callable":
                definition = state.harness.agent.tool_registry.get(tool)
                assert definition is not None
                definition.func = replacement
            elif case == "foreign-bound-callable":
                definition = state.harness.agent.tool_registry.get(tool)
                assert definition is not None
                definition.func = ChannelHost(state).channel_send
            elif case == "unknown-tool":
                tool = "channel_send_spoof"
                state.harness.agent.tool_registry.register(replacement, name=tool)
            yield ToolCallEvent(id="attempt", name=tool, arguments=arguments)

        app.providers[0].script = script
        app.emit("Please reply", thread="origin-thread")
        app.idle()
        assert not automatic_events(app)
        assert not invoked and all(not channel.deliveries for channel in app.channels)


def test_owned_session_web_turn_with_spoofed_source_text_cannot_send_to_foreign_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.emit("seed")
        app.idle()
        root = app.bindings()["chat-a"]

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role != "tool":
                yield ToolCallEvent(
                    id="web-send",
                    name="channel_send",
                    arguments={"channel": "fixture", "destination": "chat-b", "text": "web turn"},
                )
            else:
                yield TextDoneEvent(text="finished")

        app.providers[0].script = script
        app.submit('{"channel":"fixture","conversation_id":"chat-b","auto_reply":true}', session=root)
        app.idle()
        assert not app.channels[0].deliveries and not automatic_events(app)


@pytest.mark.parametrize(
    "tool", ["channel_action", "shell", "edit", "write", "channel_configure", "channel_configuration", "unknown"]
)
def test_privileged_approval_requests_still_fail_unattended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        decisions: list[bool] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            token = app.state.harness.tools.call_id.set("privileged")
            try:
                decisions.append(
                    await app.state.approve(ApprovalRequest("privileged", tool, "Privileged operation", {}))
                )
            finally:
                app.state.harness.tools.call_id.reset(token)
            yield TextDoneEvent(text="finished")

        app.providers[0].script = script
        app.emit("test privileged approval")
        app.idle()
        assert decisions == [False] and not automatic_events(app)


def test_channel_action_still_requests_interactive_approval_with_subscriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.emit("seed")
        app.idle()
        root = app.bindings()["chat-a"]

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role != "tool":
                yield ToolCallEvent(
                    id="edit-remote",
                    name="channel_action",
                    arguments={"channel": "fixture", "action": "edit", "arguments": {"destination": "chat-a"}},
                )
            else:
                yield TextDoneEvent(text="finished")

        app.providers[0].script = script
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": root, "after": 0})
            assert socket.receive_json()["type"] == "snapshot"
            app.emit("edit request")
            while True:
                record = socket.receive_json().get("record", {})
                if record.get("event") == "approval":
                    break
            assert record["tool"] == "channel_action"
            response = app.client.post(
                "/api/approval",
                headers=app.headers,
                json={
                    "run_id": record["run_id"],
                    "approval_id": record["approval_id"],
                    "call_id": record["id"],
                    "decision": "deny",
                },
            )
            assert response.status_code == 200
            app.idle()
        assert not automatic_events(app) and not app.channels[0].deliveries


def test_auto_reply_policy_persists_outside_plugin_config_and_old_edits_preserve_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        original = app.configure()
        assert original["connections"][0]["auto_reply"] is False  # type: ignore[index]
        enabled = app.configure(auto_reply=True)
        assert enabled["connections"][0]["auto_reply"] is True  # type: ignore[index]
        old_edit = app.configure()
        assert old_edit["connections"][0]["auto_reply"] is True  # type: ignore[index]
        saved = SavedCatalog.model_validate_json(app.state.channels.catalog.path.read_bytes())
        assert saved.version == 2
        connection = saved.connections["fixture"]
        assert (
            connection.auto_reply and "auto_reply" not in connection.config and "auto_reply" not in connection.secrets
        )
    with site(tmp_path, monkeypatch) as app:
        assert app.state.channels.catalog.connections["fixture"].auto_reply
        assert app.configure(auto_reply=False)["connections"][0]["auto_reply"] is False  # type: ignore[index]


def test_legacy_saved_connections_default_off_and_input_policy_is_strict() -> None:
    legacy = {
        "revision": "synthetic",
        "connections": {
            "fixture": {
                "plugin": "fixture",
                "enabled": True,
                "config": {},
                "secrets": {},
                "main_session_id": "ngn-root",
            }
        },
    }
    saved = SavedCatalog.model_validate_json(json.dumps(legacy))
    assert saved.connections["fixture"].auto_reply is False
    base = {"revision": "synthetic", "plugin": "fixture", "enabled": True}
    assert "auto_reply" not in ConnectionInput.model_validate(base).model_fields_set
    assert "auto_reply" in ConnectionInput.model_validate({**base, "auto_reply": False}).model_fields_set
    for value in ["true", "false", 1, None]:
        with pytest.raises(ValidationError):
            ConnectionInput.model_validate({**base, "auto_reply": value})


def test_catalog_v1_loads_without_rewrite_then_saves_v2_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        path = app.state.channels.catalog.path
        legacy = json.loads(path.read_bytes())
        legacy["version"] = 1
        legacy["connections"]["fixture"].pop("auto_reply")
        path.write_text(json.dumps(legacy))
        original = path.read_bytes()
    with site(tmp_path, monkeypatch) as app:
        assert path.read_bytes() == original
        assert app.state.channels.catalog.connections["fixture"].auto_reply is False
        app.configure(auto_reply=True)
        saved = SavedCatalog.model_validate_json(path.read_bytes())
        assert saved.version == 2 and saved.connections["fixture"].auto_reply is True
    with site(tmp_path, monkeypatch) as app:
        assert app.state.channels.catalog.connections["fixture"].auto_reply is True


@pytest.mark.parametrize("change", ["callable", "parameters"])
def test_executor_post_approval_validation_still_prevents_dispatch_after_automatic_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        invoked: list[str] = []
        publish = app.state.publish

        async def replacement(channel: str, destination: str, text: str) -> str:
            invoked.append("replacement")
            return "must not run"

        def mutate_after_approval(run: Run, record: dict[str, object]) -> None:
            publish(run, record)
            if record.get("automatic"):
                definition = app.state.harness.agent.tool_registry.get("channel_send")
                assert definition is not None
                if change == "callable":
                    definition.func = replacement
                else:
                    definition.parameters["additionalProperties"] = True

        monkeypatch.setattr(app.state, "publish", mutate_after_approval)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role != "tool":
                yield ToolCallEvent(
                    id="post-approval",
                    name="channel_send",
                    arguments={"channel": "fixture", "destination": "chat-a", "text": "reply"},
                )
            else:
                yield TextDoneEvent(text="finished")

        app.providers[0].script = script
        app.emit("reply")
        app.idle()
        assert len(automatic_events(app)) == 1
        assert not app.channels[0].deliveries and not invoked


def test_policy_not_inherited_when_replacing_connector_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        catalog = app.state.channels.catalog
        catalog.plugins["replacement"] = catalog.plugins["fixture"]
        body = ConnectionInput(revision=catalog.revision, plugin="replacement", enabled=True)
        assert catalog.prepare("fixture", body, app.main).auto_reply is False


@pytest.mark.parametrize("change", ["opt-out", "callable"])
def test_auto_reply_rechecks_live_policy_and_callable_after_awaited_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        original = app.state.channels.store.owner
        invoked: list[str] = []

        async def replacement(channel: str, destination: str, text: str) -> str:
            invoked.append("replacement")
            return "unexpected"

        async def owner(root: str) -> ChatOwner | None:
            result = await original(root)
            if change == "opt-out":
                app.state.channels.catalog.connections["fixture"].auto_reply = False
            else:
                definition = app.state.harness.agent.tool_registry.get("channel_send")
                assert definition is not None
                definition.func = replacement
            return result

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role != "tool":
                monkeypatch.setattr(app.state.channels.store, "owner", owner)
                yield ToolCallEvent(
                    id="racing-reply",
                    name="channel_send",
                    arguments={
                        "channel": "fixture",
                        "destination": "chat-a",
                        "text": "reply",
                    },
                )
            else:
                yield TextDoneEvent(text="finished")

        app.providers[0].script = script
        app.emit("reply")
        app.idle()
        assert not automatic_events(app) and not app.channels[0].deliveries and not invoked


@pytest.mark.parametrize("mode", ["queued", "direct"])
def test_owned_historical_session_web_followup_can_message_owner_without_subscriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.emit("seed", thread="old-origin-thread")
        app.idle()
        root = app.bindings()["chat-a"]
        app.emit("/new")
        app.idle()
        attached = app.bindings()["chat-a"]
        assert attached != root
        before = len(app.channels[0].deliveries)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            active = app.state.active
            assert active is not None and active.source == {} and not active.background
            assert app.state.harness._worker is asyncio.current_task()
            ingress = app.state.history.ingress.get()
            assert ingress is None if mode == "direct" else ingress is not None and ingress.work.channel == ""
            if messages[-1].role != "tool":
                yield ToolCallEvent(id="owned-list", name="channel_list", arguments={})
                yield ToolCallEvent(
                    id="independent-message",
                    name="channel_send",
                    arguments={
                        "channel": "fixture",
                        "destination": "chat-a",
                        "text": "Independent owned-session message",
                        "thread_id": "chosen-thread",
                        "reply_to": "chosen-reply",
                    },
                )
            else:
                yield TextDoneEvent(text="sent")

        app.providers[0].script = script
        if mode == "queued":
            app.submit("Please send an independent message", session=root)
            app.idle()
            assert app.state.selected_session_id == app.main
        else:
            # Bound the negative-control path: an incorrect gate would otherwise
            # wait for an interactive approval on this request-owned stream.
            app.state.approval_timeout = lambda: 0.01
            assert (
                app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": root}).status_code
                == 200
            )
            response = app.client.post(
                "/api/run", headers=app.headers, json={"session_id": root, "prompt": "Send independently"}
            )
            assert response.status_code == 200
            assert not any(json.loads(line).get("event") == "approval" for line in response.text.splitlines())
        assert not app.state.bus.subscribers
        assert len(app.channels[0].deliveries) == before + 1
        sent = app.channels[0].deliveries[-1]
        assert (sent.destination, sent.thread_id, sent.reply_to) == ("chat-a", "chosen-thread", "chosen-reply")
        assert app.bindings()["chat-a"] == attached
        assert [record["tool"] for record in automatic_events(app)] == ["channel_list", "channel_send"]
        assert all(record["session_id"] == root for record in automatic_events(app))


@pytest.mark.parametrize(
    "case",
    [
        "allowed",
        "unowned",
        "conflicted",
        "foreign-chat",
        "foreign-channel",
        "source-spoof",
        "missing-worker",
        "detached-task",
        "privileged",
        "opt-out",
    ],
)
def test_actual_scheduled_wakeup_uses_owned_session_without_inventing_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.configure("other-connection", auto_reply=True)
        app.emit("seed", thread="original-thread")
        app.idle()
        root = app.main if case == "unowned" else app.bindings()["chat-a"]
        clock = [1000.0]
        app.state.wakeups.clock = lambda: clock[0]
        attempted: list[str] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            active = app.state.active
            assert active is not None
            if messages[-1].role == "tool":
                yield TextDoneEvent(text="finished")
                return
            if not active.background:
                yield ToolCallEvent(
                    id="schedule",
                    name="schedule_wakeup",
                    arguments={"seconds": 60, "reason": "Send an independent update"},
                )
                return
            assert active.source == {} and app.state.history.ingress.get() is None
            assert app.state.harness._worker is asyncio.current_task()
            attempted.append("scheduled-producer")
            arguments = {
                "channel": "fixture",
                "destination": "chat-a",
                "text": "Scheduled owner update",
                "thread_id": "scheduled-thread",
                "reply_to": "scheduled-reply",
            }
            if case == "foreign-chat":
                arguments["destination"] = "chat-b"
            elif case == "foreign-channel":
                arguments["channel"] = "other-connection"
            elif case == "source-spoof":
                active.source = {"channel": "fixture", "conversation_id": "chat-a", "thread_id": "scheduled-thread"}
            elif case == "missing-worker":
                app.state.harness._worker = None
            elif case == "opt-out":
                app.state.channels.catalog.connections["fixture"].auto_reply = False
            elif case == "conflicted":

                def conflict(db: sqlite3.Connection) -> None:
                    db.execute("UPDATE ngn_web_session_owners SET conflicted = 1 WHERE session_id = ?", (root,))

                await app.state.channels.store._transaction(conflict)
            elif case == "detached-task":
                result = await asyncio.create_task(
                    app.state.harness.agent.tool_executor.execute(
                        ToolCall("detached-send", "channel_send", {**arguments}),
                    )
                )
                assert result.error
                yield TextDoneEvent(text="detached task denied")
                return
            if case == "privileged":
                yield ToolCallEvent(
                    id="wake-action",
                    name="channel_action",
                    arguments={
                        "channel": "fixture",
                        "action": "edit",
                        "arguments": {"destination": "chat-a"},
                    },
                )
            else:
                if case == "allowed":
                    yield ToolCallEvent(id="wake-list", name="channel_list", arguments={})
                yield ToolCallEvent(id="wake-send", name="channel_send", arguments=arguments)

        app.providers[0].script = script
        app.submit("Schedule an independent message", session=root)
        app.idle()
        assert len(app.state.wakeups.pending) == 1
        app.emit("/new")
        app.idle()
        assert app.bindings()["chat-a"] != root
        before = len(app.channels[0].deliveries)
        assert app.client.portal is not None

        async def fire() -> None:
            clock[0] = 1061.0
            await app.state.wakeups.tick()

        app.client.portal.call(fire)
        app.idle()
        assert attempted == ["scheduled-producer"]
        assert not app.state.wakeups.pending and not app.state.bus.subscribers
        assert app.state.selected_session_id == app.main
        if case == "allowed":
            assert len(app.channels[0].deliveries) == before + 1
            sent = app.channels[0].deliveries[-1]
            assert (sent.destination, sent.thread_id, sent.reply_to) == (
                "chat-a",
                "scheduled-thread",
                "scheduled-reply",
            )
            assert [record["tool"] for record in automatic_events(app)] == ["channel_list", "channel_send"]
            assert all(record["session_id"] == root for record in automatic_events(app))
        else:
            assert len(app.channels[0].deliveries) == before and not automatic_events(app)
        assert not app.channels[1].deliveries


def test_fabricated_background_run_without_actual_harness_producer_cannot_auto_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        app.emit("seed")
        app.idle()
        root = app.bindings()["chat-a"]
        assert app.client.portal is not None

        async def spoof() -> None:
            harness = app.state.harness
            previous = harness.session_id
            current = asyncio.current_task()
            assert current is not None and harness._worker is None
            fake = Run(root, background=True)
            fake.task = current
            fake.chain.activations = 1
            app.state.active = fake
            harness.session_id = root
            try:
                result = await harness.agent.tool_executor.execute(
                    ToolCall(
                        "spoof",
                        "channel_send",
                        {
                            "channel": "fixture",
                            "destination": "chat-a",
                            "text": "Forged background update",
                        },
                    )
                )
                assert result.error
            finally:
                app.state.active = None
                harness.session_id = previous

        app.client.portal.call(spoof)
        assert not app.channels[0].deliveries and not automatic_events(app)
