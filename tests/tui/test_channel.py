"""Offline real-Harness execution and Textual presentation of explicit sends."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Static

from nagents.channels.local_delivery import discard_notification
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelFile
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelSendCapabilities
from nagents.events import CompactionDoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.types import TranscriptAnchor
from nagents.session import SessionManager
from nagents.tui import NagentsApp
from nagents.tui.channel import TuiChannelHost
from nagents.tui.delivery import DeliveryWidget
from nagents.tui.screens import ApprovalModal
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import Turn
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import setup_harness
from tests.support.tui import idle
from tests.support.tui import send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import HarnessEvent
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


async def approve(request: ApprovalRequest) -> bool:
    return True


def entries(app: NagentsApp) -> list[str]:
    return [
        "delivery" if isinstance(widget, DeliveryWidget) else "tool" if isinstance(widget, ToolCard) else "turn"
        for widget in app.query_one("#conversation").children
        if isinstance(widget, (DeliveryWidget, ToolCard, Turn))
    ]


@pytest.mark.parametrize("missed", [False, True])
def test_live_reload_identical_sends_and_reused_call_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missed: bool
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) <= 2:
                yield TextChunkEvent(chunk=f"before {len(provider.requests)}")
                for call in ("first", "second"):
                    yield ToolCallEvent(
                        id=call,
                        name="channel_send",
                        arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "identical"},
                    )
            else:
                yield TextChunkEvent(chunk="final response")
                yield TextDoneEvent(text="final response")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert app._backend_ready, app._last_error
            host = app._channel_host
            assert host is not None
            harness.approval_handler = approve
            if missed:
                host.channel.notify = discard_notification
            await send(app, pilot, "send twice per round")
            await idle(app, pilot)
            assert not app._last_error
            widgets = list(app.query(DeliveryWidget))
            assert len(widgets) == 4
            ids = [widget.receipt.delivery_id for widget in widgets]
            assert len(set(ids)) == 4
            assert {widget.receipt.text for widget in widgets} == {"identical"}
            expected = [
                "turn",
                "turn",
                "tool",
                "delivery",
                "tool",
                "delivery",
                "turn",
                "tool",
                "delivery",
                "tool",
                "delivery",
                "turn",
            ]
            assert entries(app) == expected
            cards = list(app.query(ToolCard))
            cards[0].collapsed = False
            title = cards[0].query_one("CollapsibleTitle")
            title.focus()
            await host.channel.notify(widgets[0].receipt.notification())
            await app._reconcile_deliveries(force=True)
            assert list(app.query(DeliveryWidget)) == widgets
            assert not cards[0].collapsed and app.focused is title
            assert len(providers[0].requests) == 3
            assert not [m for m in await harness.history() if m.content == "identical"]
            assert harness.session_id in str(providers[0].requests[0][0].content)
            await app._clear_conversation()
            await app._load_history()
            assert entries(app) == expected
            assert [w.receipt.delivery_id for w in app.query(DeliveryWidget)] == ids
        assert harness.agent.tool_registry.get("channel_send") is None
        assert harness._delivery_definition is None

        restored, _ = setup_harness(tmp_path, monkeypatch, script)
        reloaded = NagentsApp(restored, resume_session=harness.session_id)
        async with reloaded.run_test() as pilot:
            await idle(reloaded, pilot)
            assert entries(reloaded) == expected
            assert [w.receipt.delivery_id for w in reloaded.query(DeliveryWidget)] == ids

    asyncio.run(scenario())


@pytest.mark.parametrize("missed", [False, True])
def test_retried_generation_anchors_only_its_exact_streamed_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missed: bool
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) <= 2:
                yield ToolCallEvent(
                    id="reused",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "once"},
                )
                if len(provider.requests) == 1:
                    yield ErrorEvent(message="Retry this generation", recoverable=True)
            else:
                yield TextChunkEvent(chunk="final")
                yield TextDoneEvent(text="final")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = approve
            host = app._channel_host
            assert host is not None
            if missed:
                host.channel.notify = discard_notification
            await send(app, pilot, "retry")
            await idle(app, pilot)
            assert len(providers[0].requests) == 3
            assert len(app.query(DeliveryWidget)) == 1
            widget = app.query_one(DeliveryWidget)
            abandoned, successful = app.query(ToolCard)
            assert abandoned.has_class("cancelled")
            assert successful.has_class("complete")
            assert entries(app) == ["turn", "tool", "tool", "delivery", "turn"]
            children = list(app.query_one("#conversation").children)
            assert children.index(widget) == children.index(successful) + 1
            receipt = widget.receipt
            assert app._delivery_anchors == {(receipt.origin.anchor_message_id, 0): widget}
            assert not app._unanchored_tools
            history = await host.snapshot(harness.session_id)
            assert history.deliveries.current == (receipt,)
            assert [row for row, message in history.rows if message.tool_calls] == [receipt.origin.anchor_message_id]
            await app._reconcile_deliveries(force=True)
            assert app.query_one(DeliveryWidget) is widget
            await app._clear_conversation()
            await app._load_history()
            assert entries(app) == ["turn", "tool", "delivery", "turn"]
            assert app.query_one(DeliveryWidget).receipt == receipt
            assert not app._unanchored_tools
        restored, _ = setup_harness(tmp_path, monkeypatch, script)
        reloaded = NagentsApp(restored, resume_session=harness.session_id)
        async with reloaded.run_test() as pilot:
            await idle(reloaded, pilot)
            assert entries(reloaded) == ["turn", "tool", "delivery", "turn"]
            assert reloaded.query_one(DeliveryWidget).receipt == receipt

    asyncio.run(scenario())


def test_reused_call_id_across_submissions_has_distinct_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) % 2:
                yield ToolCallEvent(
                    id="reused",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "identical"},
                )
            else:
                yield TextDoneEvent(text="final")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = approve
            for text in ("first submission", "second submission"):
                await send(app, pilot, text)
                await idle(app, pilot)
            assert len(providers[0].requests) == 4
            receipts = [widget.receipt for widget in app.query(DeliveryWidget)]
            assert len(receipts) == 2
            assert len({receipt.origin.host_run_id for receipt in receipts}) == 2
            assert len({receipt.origin.anchor_message_id for receipt in receipts}) == 2
            assert len({receipt.delivery_id for receipt in receipts}) == 2
            assert {receipt.origin.call_id for receipt in receipts} == {"reused"}
            assert entries(app) == ["turn", "tool", "delivery", "turn"] * 2
            await app._clear_conversation()
            await app._load_history()
            assert entries(app) == ["turn", "tool", "delivery", "turn"] * 2
            assert [widget.receipt for widget in app.query(DeliveryWidget)] == receipts

    asyncio.run(scenario())


def test_anchor_rejects_equal_but_unrelated_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            call = ToolCallEvent(id="same", name="channel_send", arguments={"text": "same"})
            await app._event(call)
            with pytest.raises(RuntimeError, match="no matching streamed tool card"):
                await app._event(TranscriptAnchor(harness.session_id, 123, (replace(call),)))
            assert not app._delivery_anchors
            assert len(app._unanchored_tools) == 1

    asyncio.run(scenario())


def test_delivery_arrival_and_compaction_preserve_inspected_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        awaiting_approval = asyncio.Event()
        release = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="send",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "arriving"},
                )
            else:
                yield TextDoneEvent(text="final")

        async def delayed_approval(request: ApprovalRequest) -> bool:
            awaiting_approval.set()
            await asyncio.wait_for(release.wait(), HANG_GUARD)
            return True

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = delayed_approval
            await send(app, pilot, "deliver")
            await asyncio.wait_for(awaiting_approval.wait(), HANG_GUARD)
            async with asyncio.timeout(HANG_GUARD):
                while not app.query(ToolCard):
                    await pilot.pause(0.01)
            card = app.query_one(ToolCard)
            title = card.query_one("CollapsibleTitle")
            await pilot.click(title)
            await pilot.pause()
            assert not card.collapsed and app.focused is title
            assert not app.query(DeliveryWidget)
            release.set()
            await idle(app, pilot)
            delivery = app.query_one(DeliveryWidget)
            assert app.query_one(ToolCard) is card
            assert not card.collapsed and app.focused is title
            assert entries(app) == ["turn", "tool", "delivery", "turn"]
            await harness.agent.session.replace_context(
                harness.session_id, [Message(role="compaction_summary", content="summary")]
            )
            await app._event(CompactionDoneEvent(original_message_count=4, new_message_count=1))
            await pilot.pause()
            assert app.query_one(DeliveryWidget) is delivery
            assert app.query_one(ToolCard) is card
            assert not card.collapsed and app.focused is title
            assert entries(app) == ["delivery", "turn", "tool", "turn"]
            assert app.query_one(".earlier-deliveries", Static).content == "Deliveries from earlier context"

    asyncio.run(scenario())


@pytest.mark.parametrize("attachment", ["image.png", "notes.txt", "movie.mp4"])
def test_files_rejected_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attachment: str) -> None:
    async def scenario() -> None:
        (tmp_path / attachment).write_bytes(b"file bytes")

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="send",
                    name="channel_send",
                    arguments={
                        "channel": "builtin.tui",
                        "destination": harness.session_id,
                        "text": "must not appear",
                        "attachments": [attachment],
                    },
                )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = approve
            await send(app, pilot, "send file")
            await idle(app, pilot)
            assert not app.query(DeliveryWidget)
            assert app.query_one(ToolCard).has_class("failed")
            assert app._channel_host is not None
            assert not (await app._channel_host.journal.history(harness.session_id)).current

    asyncio.run(scenario())


def test_denial_focus_and_no_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="send",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "denied"},
                )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("f6")
            conversation = app.query_one("#conversation")
            assert app.focused is conversation
            app._launch(lambda: app._send("send"), "Working")
            async with asyncio.timeout(HANG_GUARD):
                while not isinstance(app.screen, ApprovalModal):
                    await pilot.pause(0.01)
            await pilot.press("escape")
            await idle(app, pilot)
            assert app.focused is conversation
            assert not app.query(DeliveryWidget)
            assert app._channel_host is not None
            assert not (await app._channel_host.journal.history(harness.session_id)).current

    asyncio.run(scenario())


@pytest.mark.parametrize("collision", ["channel_list", "channel_send", "channel_action", "builtin.tui"])
def test_registration_collisions_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collision: str) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        host = TuiChannelHost(harness, discard_notification)

        async def existing() -> str:
            return "existing"

        try:
            registry = harness.agent.tool_registry
            if collision == "builtin.tui":
                harness.agent.add_channel(host.channel)
            else:
                definition = registry.register(existing, name=collision)
            with pytest.raises(ChannelError, match="collision"):
                host.open()
            if collision == "builtin.tui":
                assert harness.agent._channels.pop(collision) is host.channel
            else:
                assert registry.get(collision) is definition
                registry.unregister(collision)
            host.open()
            assert harness._delivery_definition is registry.get("channel_send")
            list_tool = registry.get("channel_list")
            assert list_tool is not None and list_tool.func is not None
            catalog = await list_tool.func()
            assert catalog[0]["capabilities"] == ["send_text"]
            assert catalog[0]["content_capabilities"]["receive"]["via"] == "host"
            with pytest.raises(ChannelError, match="host-managed"):
                await host.channel.listen(lambda message: asyncio.sleep(0))
            registry.unregister("channel_list")
            replacement = registry.register(existing, name="channel_list")
            host.close()
            assert registry.get("channel_list") is replacement
            assert registry.get("channel_send") is None
            assert registry.get("channel_action") is None
            assert harness._delivery_definition is None
        finally:
            host.close()
            await harness.close()

    asyncio.run(scenario())


def test_registration_follows_trusted_extension_initialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        plugin = tmp_path / "trusted.py"
        plugin.write_text(
            "async def custom() -> str:\n    return 'extension owned'\n"
            "def setup(harness):\n    harness.agent.register_tool(custom, name='channel_send')\n"
        )
        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        harness.config.plugins = (f"{plugin}:setup",)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert harness.loaded_plugins == [f"{plugin}:setup"]
            assert "collision" in app._last_error
            assert app._channel_host is None
            definition = harness.agent.tool_registry.get("channel_send")
            assert definition is not None
            assert harness._delivery_definition is None
            assert harness.agent.tool_registry.get("channel_list") is None
        assert harness.agent.tool_registry.get("channel_send") is definition

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["destination", "thread_id", "reply_to"])
def test_execution_destination_and_reference_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                arguments = {"channel": "builtin.tui", "destination": harness.session_id, "text": "rejected"}
                arguments[field] = "foreign"
                yield ToolCallEvent(id="send", name="channel_send", arguments=arguments)
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        host = TuiChannelHost(harness, discard_notification)
        host.open()
        harness.approval_handler = approve
        try:
            with pytest.raises(ChannelError, match="unavailable"):
                await host.channel.send(ChannelSend(destination=harness.session_id, text="outside execution"))
            events = await collect(harness)
            assert any(isinstance(event, ToolResultEvent) and event.error for event in events)
            assert not (await host.journal.history(harness.session_id)).current
        finally:
            host.close()
            await harness.close()

    asyncio.run(scenario())


def test_historic_media_and_compaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="send",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "sent"},
                )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = approve
            await send(app, pilot, "send")
            await idle(app, pilot)
            host = app._channel_host
            assert host is not None
            original_widget = app.query_one(DeliveryWidget)
            original = original_widget.receipt
            # Shared historic journal fixture: no browser adapter is installed.
            media = await host.journal.prepare(
                replace(original.origin, invocation_id="historic"),
                "builtin.web",
                ChannelSend(
                    destination=harness.session_id,
                    text="[red]literal[/red]",
                    files=(ChannelFile("[red]photo.png", "image/png", b"historic bytes"),),
                ),
                ChannelSendCapabilities(text=True, file_media_types=("image/png",)),
            ).commit()
            await harness.agent.session.replace_context(
                harness.session_id, [Message(role="compaction_summary", content="summary")]
            )
            await app._event(CompactionDoneEvent(original_message_count=4, new_message_count=1))
            assert app.query(DeliveryWidget).first() is original_widget
            assert len(app.query(DeliveryWidget)) == 2
            await app._clear_conversation()
            await app._load_history()
            widgets = list(app.query(DeliveryWidget))
            assert [w.receipt.delivery_id for w in widgets] == [original.delivery_id, media.delivery_id]
            notes = [str(w.content) for w in app.query(Static)]
            assert "Deliveries from earlier context" in notes
            assert "[red]literal[/red]" in notes
            assert any("[red]photo.png (image/png) via builtin.web" in note for note in notes)
            assert all("historic bytes" not in note for note in notes)
            children = list(app.query_one("#conversation").children)
            assert children.index(widgets[-1]) < next(
                i
                for i, w in enumerate(children)
                if isinstance(w, Static) and "Earlier context was compacted" in str(w.content)
            )

    asyncio.run(scenario())


def test_custom_adapter_ordinary_history_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="send",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "unanchored"},
                )
            else:
                yield TextDoneEvent(text="ordinary reply")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        harness.agent.session = SessionManager(harness.agent.session.db_path)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = approve
            await send(app, pilot, "hello")
            await idle(app, pilot)
            assert not app.query(DeliveryWidget)
            assert app.query_one(ToolCard).has_class("failed")
            await app._clear_conversation()
            await app._load_history()
            assert len(app.query(Turn)) == 2

    asyncio.run(scenario())


def test_children_do_not_inherit_channel_tools_or_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                yield TextDoneEvent(text="child result")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "inspect"})
            else:
                yield TextDoneEvent(text="ordinary parent response")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.approval_handler = approve
            await send(app, pilot, "delegate")
            await idle(app, pilot)
            assert len(providers) == 2
            assert "channel_send" in providers[0].schemas[0]
            assert not {"channel_send", "channel_list", "channel_action"}.intersection(providers[1].schemas[0])
            assert all("builtin.tui" not in str(message.content) for message in providers[1].requests[0])
            assert not app.query(DeliveryWidget)
            assert app._channel_host is not None
            assert not (await app._channel_host.journal.history(harness.session_id)).current

    asyncio.run(scenario())


@pytest.mark.parametrize("lagging", [False, True])
def test_cancel_recovers_missed_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lagging: bool) -> None:
    async def scenario() -> None:
        committed = asyncio.Event()
        release_ui = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="send",
                    name="channel_send",
                    arguments={"channel": "builtin.tui", "destination": harness.session_id, "text": "survives cancel"},
                )
            else:
                await asyncio.Event().wait()
                yield TextDoneEvent(text="unreachable")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            host = app._channel_host
            assert host is not None
            harness.approval_handler = approve
            if lagging:
                original_event = app._event

                async def delayed_event(event: HarnessEvent) -> None:
                    if isinstance(event, ToolCallEvent):
                        await asyncio.wait_for(release_ui.wait(), HANG_GUARD)
                    await original_event(event)

                monkeypatch.setattr(app, "_event", delayed_event)

            async def lost(notice: object) -> None:
                committed.set()
                await asyncio.Event().wait()

            host.channel.notify = lost
            await send(app, pilot, "send")
            await asyncio.wait_for(committed.wait(), HANG_GUARD)
            app.action_cancel()
            release_ui.set()
            await idle(app, pilot)
            assert len(app.query(DeliveryWidget)) == 1
            assert entries(app) == ["turn", "tool", "delivery"]

    asyncio.run(scenario())
