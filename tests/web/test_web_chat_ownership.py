"""Permanent chat ownership, legacy quarantine, and root-scoped outbound tools."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from nagents.channels.runtime import _envelope
from nagents.channels.types import ChannelAction
from nagents.channels.types import ChannelActivity
from nagents.channels.types import ChannelCommand
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelMessage
from nagents.channels.types import ChannelValue
from nagents.types import Message
from nagents.types import ToolCall
from nagents.web.deletion import delete_session
from nagents.web.routing import ChatOwner
from nagents.web.routing import RoutingStore
from nagents.web.routing import Work
from nagents.web.service import Run
from nagents.web.service import WebState
from nagents.web.wakeups import Chain
from tests.support.channels import FakeChannel
from tests.support.channels import site
from tests.support.web import client_app
from tests.web.test_web_deletion import quiet
from tests.web.test_web_deletion import rows
from tests.web.test_web_routing_recovery import store_at

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.types import ApprovalRequest
    from tests.support.providers import FakeProvider


async def receive(store: RoutingStore, room: str, id: str, text: str = "/session", channel: str = "bridge") -> Work:
    command = None
    if text.startswith("/"):
        name, _, argument = text[1:].partition(" ")
        command = ChannelCommand(name, argument)
    await store.receive(channel, _envelope(channel, ChannelMessage(id, room, "participant", text)), "ngn-main", command)

    def read(db: sqlite3.Connection) -> Work:
        row = db.execute(
            "SELECT id, session_id, channel, message_id, prompt, conversation_id, thread_id, reply_to, acknowledgement "
            "FROM ngn_web_inbox WHERE channel = ? AND message_id = ?",
            (channel, id),
        ).fetchone()
        assert row is not None
        return Work(*row)

    return await store._transaction(read)


def test_commands_list_chat_history_and_adopt_only_unowned_roots(tmp_path: Path) -> None:
    async def run() -> None:
        store = await store_at(tmp_path / "owners.db")
        first = (await receive(store, "room-a", "a")).session_id
        foreign = (await receive(store, "room-b", "b")).session_id
        second = (await receive(store, "room-a", "new", "/new Owned task")).session_id
        assert len({first, second, foreign, "ngn-main"}) == 4
        assert await store.owner(first) == await store.owner(second) == ChatOwner("bridge", "room-a")
        assert await store.owner("ngn-main") is None
        listed = await receive(store, "room-a", "list", "/sessions")
        assert first in listed.acknowledgement and second in listed.acknowledgement
        assert "Available to attach" in listed.acknowledgement and "ngn-main" in listed.acknowledgement
        # A foreign-owned root is never listed or adopted.
        assert foreign not in listed.acknowledgement
        rejected: list[str] = []
        for index, argument in enumerate((foreign, "ngn-unknown", f"default {foreign}")):
            denied = await receive(store, "room-a", f"deny-{index}", f"/session {argument}")
            assert denied.session_id == second
            rejected.append(denied.acknowledgement)
        assert len(set(rejected)) == 1
        # An unowned web-created root is adopted on attach and then permanently owned.
        adopted = await receive(store, "room-a", "adopt", "/session ngn-main")
        assert adopted.session_id == "ngn-main"
        assert await store.owner("ngn-main") == ChatOwner("bridge", "room-a")
        steal = await receive(store, "room-b", "steal", "/session ngn-main")
        assert steal.session_id == foreign
        assert await store.owner("ngn-main") == ChatOwner("bridge", "room-a")
        assert (await receive(store, "room-a", "default", "/session default")).session_id == first
        assert (await receive(store, "room-a", "set-default", f"/session default {second}")).session_id == first
        assert (await receive(store, "room-a", "back", "/session default")).session_id == second
        assert await store.owner(foreign) == ChatOwner("bridge", "room-b")

    asyncio.run(run())


def test_default_session_command_adopts_an_unowned_root(tmp_path: Path) -> None:
    async def run() -> None:
        store = await store_at(tmp_path / "defaults.db")
        first = (await receive(store, "room-a", "a")).session_id
        assert await store.owner(first) == ChatOwner("bridge", "room-a")
        selected = await receive(store, "room-a", "set-default", "/session default ngn-main")
        assert selected.session_id == first
        assert selected.acknowledgement == "Default session: ngn-main"
        assert await store.owner("ngn-main") == ChatOwner("bridge", "room-a")
        assert (await receive(store, "room-a", "use-default", "/session default")).session_id == "ngn-main"

    asyncio.run(run())


def test_concurrent_first_admission_and_competing_management_assignment_are_atomic(tmp_path: Path) -> None:
    async def run() -> None:
        store = await store_at(tmp_path / "concurrent.db")
        admitted = await asyncio.gather(*(receive(store, "room-a", f"event-{index}") for index in range(8)))
        assert len({item.session_id for item in admitted}) == 1
        results = await asyncio.gather(
            store.assign_owner("ngn-main", "bridge", "room-a"),
            store.assign_owner("ngn-main", "bridge", "room-b"),
            return_exceptions=True,
        )
        assert results.count(None) == 1
        error = next(result for result in results if result is not None)
        assert isinstance(error, HTTPException) and error.status_code == 409
        owner = await store.owner("ngn-main")
        assert owner in (ChatOwner("bridge", "room-a"), ChatOwner("bridge", "room-b"))
        assert owner is not None
        await store.assign_owner("ngn-main", owner.channel, owner.conversation_id)
        reopened = await store_at(store.db_path)
        assert await reopened.owner("ngn-main") == owner
        with pytest.raises(HTTPException):
            await reopened.assign_owner("ngn-main", "other-connection", owner.conversation_id)
        assert await reopened.owner("ngn-main") == owner

    asyncio.run(run())


def test_migration_uses_default_bindings_and_old_inbox_roots_and_quarantines_mixed_history(tmp_path: Path) -> None:
    async def run() -> None:
        store = await store_at(tmp_path / "legacy.db")

        def legacy(db: sqlite3.Connection) -> None:
            db.execute("DROP TABLE ngn_web_session_owners")
            for id in ("ngn-current", "ngn-default", "ngn-history-only", "ngn-mixed", "ngn-incomplete"):
                db.execute("INSERT INTO v2_sessions(id, user_id) VALUES (?, 'harness')", (id,))
                db.execute("INSERT INTO harness_sessions VALUES (?, ?)", (id, id))
            db.execute("INSERT INTO ngn_web_bindings VALUES ('bridge', 'room-a', 'ngn-current', 'ngn-default')")
            db.execute("INSERT INTO ngn_web_bindings VALUES ('bridge', 'room-b', 'ngn-mixed', 'ngn-mixed')")
            for id, room, status in (
                ("ngn-history-only", "room-a", "completed"),
                ("ngn-mixed", "room-a", "queued"),
                ("ngn-mixed", "room-b", "running"),
                ("ngn-incomplete", "", "queued"),
            ):
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, conversation_id, status) "
                    "VALUES (?, 'bridge', ?, 'legacy envelope', ?, ?)",
                    (id, id + room, room, status),
                )
            db.execute(
                "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, conversation_id, acknowledgement) "
                "VALUES ('ngn-current', 'bridge', 'old-list', '/sessions', 'room-a', 'OTHER_ROOM_PRIVATE_TITLE')"
            )

        await store._transaction(legacy)
        await store.initialize()
        for id in ("ngn-current", "ngn-default", "ngn-history-only"):
            assert await store.owner(id) == ChatOwner("bridge", "room-a")
        mixed = await store.owner("ngn-mixed")
        incomplete = await store.owner("ngn-incomplete")
        assert mixed is not None and mixed.conflicted
        assert incomplete is not None and incomplete.conflicted
        assert await store.owner("ngn-main") is None
        with pytest.raises(HTTPException):
            await store.web("ngn-mixed", "web-input", "must not execute")
        old_ack = await store.claim_work(available_channels=("bridge",))
        assert old_ack is not None and old_ack.message_id == "old-list"
        assert "OTHER_ROOM_PRIVATE_TITLE" not in old_ack.acknowledgement
        await store.finish_work(old_ack, "completed")
        recovered = await receive(store, "room-b", "recover", "must not execute on mixed history")
        assert recovered.session_id != "ngn-mixed" and "please send your request again" in recovered.acknowledgement
        assert await store.owner(recovered.session_id) == ChatOwner("bridge", "room-b")
        safe = await receive(store, "room-c", "healthy", "normal new input")
        assert not safe.acknowledgement and safe.session_id != recovered.session_id
        await store.initialize()
        assert await store.owner("ngn-mixed") == mixed  # Removing a bad binding never un-quarantines old history.

    asyncio.run(run())


def test_ownership_survives_trash_restore_purge_and_same_id_recreation(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            store = state.channels.store
            old = (await receive(store, "room-a", "first")).session_id
            new = (await receive(store, "room-a", "new", "/new")).session_id
            await receive(store, "room-a", "default", f"/session default {new}")

            def complete(db: sqlite3.Connection) -> None:
                db.execute("UPDATE ngn_web_inbox SET status = 'completed'")

            await store._transaction(complete)
            await state.history.add_message(old, Message(role="user", content="OWN_ROOM_HISTORY"))
            item = (await delete_session(state, old))["trash"]
            assert isinstance(item, dict)
            assert await store.owner(old) == ChatOwner("bridge", "room-a")
            await state.trash.restore(old, str(item["deletion_id"]))
            with pytest.raises(HTTPException):
                await store.assign_owner(old, "bridge", "room-b")
            await delete_session(state, old, permanent=True)
            assert not await rows(state, "SELECT * FROM v2_sessions WHERE id = ?", old)
            assert await store.owner(old) == ChatOwner("bridge", "room-a")
            await state.harness.create_session(old)  # Simulate a restored backup/import reusing the same ID.
            with pytest.raises(HTTPException):
                await store.assign_owner(old, "bridge", "room-b")
            assert await store.owner(old) == ChatOwner("bridge", "room-a")

    asyncio.run(run())


class AddressedChannel(FakeChannel):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.calls: list[tuple[str, dict[str, ChannelValue]]] = []
        self.actions = (
            ChannelAction(
                "edit_message",
                "Edit a message in its destination",
                {
                    "type": "object",
                    "properties": {"destination": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["destination", "text"],
                    "additionalProperties": False,
                },
            ),
            ChannelAction("global_action", "Not destination-addressed", {"type": "object", "properties": {}}),
        )

    async def action(self, name: str, arguments: dict[str, ChannelValue]) -> dict[str, ChannelValue]:
        self.calls.append((name, arguments))
        return {"ok": True}


@pytest.mark.parametrize(
    "operation",
    [
        "send-foreign-room",
        "send-foreign-connection",
        "action-foreign-room",
        "action-missing-destination",
        "action-unaddressed-schema",
        "send-owned",
        "action-owned",
    ],
)
def test_executor_cannot_send_owned_context_outside_true_active_root_even_with_approval(
    tmp_path: Path, operation: str
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            host = state.channels
            root = (await receive(host.store, "room-a", "first")).session_id
            assert root != state.selected_session_id == state.harness.session_id
            first, second = AddressedChannel("bridge"), AddressedChannel("other")
            host.channels.update(bridge=first, other=second)
            host.update_tools()
            approvals: list[str] = []

            async def approve(request: ApprovalRequest) -> bool:
                approvals.append(request.tool)
                return True

            state.harness.config.demo = False
            state.harness.approval_handler = approve
            active = Run(root)  # Empty source: browser-origin follow-up in a chat-owned root.
            state.active = active
            try:
                call = ToolCall(
                    "outbound", "channel_send", {"channel": "bridge", "destination": "room-a", "text": "OWNED_CONTEXT"}
                )
                if operation == "send-foreign-room":
                    call.arguments["destination"] = "room-b"
                elif operation == "send-foreign-connection":
                    call.arguments["channel"] = "other"
                elif operation.startswith("action"):
                    args: dict[str, ChannelValue] = {"destination": "room-a", "text": "OWNED_CONTEXT"}
                    if operation == "action-foreign-room":
                        args["destination"] = "room-b"
                    if operation == "action-missing-destination":
                        args.pop("destination")
                    call = ToolCall(
                        "outbound",
                        "channel_action",
                        {
                            "channel": "bridge",
                            "action": "global_action" if operation == "action-unaddressed-schema" else "edit_message",
                            "arguments": args,
                        },
                    )
                result = await state.harness.agent.tool_executor.execute(call)
                allowed = operation in {"send-owned", "action-owned"}
                assert (result.error is None) is allowed
                assert approvals == [call.name]
                assert len(first.deliveries) + len(first.calls) == int(allowed)
                assert not second.deliveries and not second.calls
                assert [entry["name"] for entry in await host.channel_list()] == ["bridge"]
            finally:
                state.active = None

    asyncio.run(run())


def test_foreign_binding_cannot_receive_activity_or_frozen_acknowledgement(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            host = state.channels
            owned = await receive(host.store, "room-a", "a")
            channel = AddressedChannel("bridge")
            host.channels["bridge"] = channel

            def corrupt(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_bindings VALUES ('bridge', 'room-b', ?, ?)",
                    (owned.session_id, owned.session_id),
                )

            await host.store._transaction(corrupt)
            await host.activity(owned.session_id, True)
            assert [event.conversation_id for event in channel.activities] == ["room-a"]
            bad = Work(owned.id, owned.session_id, "bridge", "a", "", "room-b", "", "", "OWNED_CONTEXT")
            with pytest.raises(HTTPException):
                await host.acknowledgement(bad)
            assert not channel.deliveries
            await host.activity(owned.session_id, False)
            assert all(event.conversation_id == "room-a" for event in channel.activities)

    asyncio.run(run())


def test_conflicted_root_rejects_web_admission_and_all_outbound_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            host = state.channels
            root = (await receive(host.store, "room-a", "first")).session_id

            def legacy(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, conversation_id, status) "
                    "VALUES (?, 'bridge', 'other-history', 'legacy content', 'room-b', 'completed')",
                    (root,),
                )

            await host.store._transaction(legacy)
            await host.store.initialize()
            assert (
                await client.post("/api/run", headers=headers, json={"session_id": root, "prompt": "do not execute"})
            ).status_code == 409
            assert (
                await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": "12345678-1234-1234-1234-123456789abc",
                        "prompt": "do not execute",
                    },
                )
            ).status_code == 409
            channel = AddressedChannel("bridge")
            host.channels["bridge"] = channel
            host.update_tools()
            state.active = Run(root)
            try:
                with pytest.raises(ChannelError) as error:
                    await host.channel_send("bridge", "room-a", "MIXED_CONTEXT")
                assert not error.value.outcome_unknown
                with pytest.raises(ChannelError):
                    await host.channel_action(
                        "bridge", "edit_message", {"destination": "room-a", "text": "MIXED_CONTEXT"}
                    )
                assert not channel.deliveries and not channel.calls
            finally:
                state.active = None

    asyncio.run(run())


def test_cached_acknowledgement_cannot_leak_a_foreign_catalog_or_session_id(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            host = state.channels
            owned = await receive(host.store, "room-a", "a")
            foreign = await receive(host.store, "room-b", "b")
            channel = AddressedChannel("bridge")
            host.channels["bridge"] = channel
            await host.acknowledgement(
                replace(owned, acknowledgement=f"Sessions:\n{foreign.session_id} — FOREIGN_TITLE")
            )
            assert owned.session_id in channel.deliveries[-1].text
            assert foreign.session_id not in channel.deliveries[-1].text
            assert "FOREIGN_TITLE" not in channel.deliveries[-1].text
            await host.acknowledgement(replace(owned, acknowledgement=f"Session: {foreign.session_id}"))
            assert foreign.session_id not in channel.deliveries[-1].text
            assert all(delivery.destination == "room-a" for delivery in channel.deliveries)

    asyncio.run(run())


@pytest.mark.requires_posix
def test_connector_disable_delete_readd_and_restart_preserve_chat_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("/session")
        app.idle()
        owned = app.bindings()["chat-a"]
        app.configure(enabled=False)
        snapshot = app.client.get("/api/channels", headers=app.headers).json()
        assert (
            app.client.request(
                "DELETE", "/api/channels/fixture", headers=app.headers, json={"revision": snapshot["revision"]}
            ).status_code
            == 200
        )
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit(f"/session {owned}", "chat-b")
        app.idle()
        assert app.bindings()["chat-b"] != owned
        assert "Session not found" in app.channels[0].deliveries[-1].text
        app.emit("/sessions", "chat-a")
        app.idle()
        assert owned in app.channels[0].deliveries[-1].text
        assert app.bindings()["chat-b"] not in app.channels[0].deliveries[-1].text


@pytest.mark.requires_posix
def test_conflicted_chat_recovery_does_not_stop_shared_connector_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("/session")
        app.idle()
        old = app.bindings()["chat-a"]
        assert app.client.portal is not None

        async def migrate() -> None:
            def evidence(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, conversation_id, status) "
                    "VALUES (?, 'fixture', 'other-history', 'legacy input', 'chat-b', 'completed')",
                    (old,),
                )

            await app.state.channels.store._transaction(evidence)
            await app.state.channels.store.initialize()

        app.client.portal.call(migrate)
        app.emit("Do not execute on old mixed history", id="recover")
        app.idle()
        assert app.bindings()["chat-a"] != old
        assert "please send your request again" in app.channels[0].deliveries[-1].text
        assert not app.providers[0].requests
        app.emit("Unrelated chat still works", "chat-c")
        app.idle()
        assert len(app.providers[0].requests) == 1
        assert not app.state.channels.sources["fixture"].done()


@pytest.mark.parametrize("invalid", ["unowned", "conflicted", "trashed", "missing-history", "removed-connector"])
def test_owner_typing_requires_unconflicted_live_root_and_connected_transport(tmp_path: Path, invalid: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            host = state.channels
            root = (await receive(host.store, "room-a", "owned")).session_id
            channel = AddressedChannel("bridge")
            if invalid != "removed-connector":
                host.channels["bridge"] = channel
            if invalid == "unowned":
                root = state.selected_session_id
            elif invalid == "conflicted":

                def conflict(db: sqlite3.Connection) -> None:
                    db.execute("UPDATE ngn_web_session_owners SET conflicted = 1 WHERE session_id = ?", (root,))

                await host.store._transaction(conflict)
            elif invalid == "missing-history":

                def remove_history(db: sqlite3.Connection) -> None:
                    db.execute("DELETE FROM v2_sessions WHERE id = ?", (root,))

                await host.store._transaction(remove_history)
            elif invalid == "trashed":
                current = (await receive(host.store, "room-a", "new", "/new")).session_id
                await receive(host.store, "room-a", "default", f"/session default {current}")

                def complete(db: sqlite3.Connection) -> None:
                    db.execute("UPDATE ngn_web_inbox SET status = 'completed'")

                await host.store._transaction(complete)
                await delete_session(state, root)
                assert await host.store.owner(root) == ChatOwner("bridge", "room-a")
            await host.activity(root, True)
            await host.activity(root, False)
            assert not channel.activities
            assert not host.activities.current

    asyncio.run(run())


@pytest.mark.requires_posix
@pytest.mark.parametrize("origin", ["web", "scheduled"])
def test_historical_owned_root_keeps_typing_after_chat_selects_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    from nagents.events import TextChunkEvent

    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("/session")
        app.idle()
        old = app.bindings()["chat-a"]
        app.emit("/new")
        current = app.bindings()["chat-a"]
        app.emit(f"/session default {current}")
        app.idle()
        assert app.client.portal is not None
        started = app.client.portal.call(asyncio.Event)

        async def holding(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            started.set()
            yield TextChunkEvent(chunk="Historical owned work")
            await asyncio.Event().wait()

        app.providers[0].script = holding
        if origin == "web":
            app.submit("Historical owned work", old)
        else:
            app.client.portal.call(
                app.state.wakeups.schedule, old, "scheduled-origin", Chain(), "", 0.001, "Historical owned work"
            )
        app.client.portal.call(asyncio.wait_for, started.wait(), 5)
        active = app.state.active
        assert active is not None and active.session_id == old
        assert active.background is (origin == "scheduled")
        assert app.bindings()["chat-a"] == current
        assert app.channels[0].activities == [ChannelActivity("chat-a", True, session_id=old)]
        assert app.client.post("/api/cancel", headers=app.headers, json={"run_id": active.id}).status_code == 200
        app.idle()
        assert app.channels[0].activities == [ChannelActivity("chat-a", flag, session_id=old) for flag in (True, False)]


@pytest.mark.requires_posix
@pytest.mark.parametrize("removal", ["disabled", "removed"])
def test_historical_owner_does_not_reactivate_a_disabled_or_removed_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removal: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("/session")
        app.idle()
        owned = app.bindings()["chat-a"]
        channel = app.channels[0]
        if removal == "disabled":
            app.configure(enabled=False)
        else:
            snapshot = app.client.get("/api/channels", headers=app.headers).json()
            response = app.client.request(
                "DELETE", "/api/channels/fixture", headers=app.headers, json={"revision": snapshot["revision"]}
            )
            assert response.status_code == 200
        app.submit("Follow-up in the owned session", owned)
        app.idle()
        assert channel.closed and not channel.activities
        assert len(app.providers[0].requests) == 1
        assert app.client.portal is not None
        assert app.client.portal.call(app.state.channels.store.owner, owned) == ChatOwner("fixture", "chat-a")
