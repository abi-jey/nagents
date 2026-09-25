"""Local delivery receipts and integration with owned execution and persistence."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from nagents.channels import Channel
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelDispatcher
from nagents.channels import ChannelReceiver
from nagents.channels import local_delivery
from nagents.channels.delivery_types import DeliveryAsset
from nagents.channels.delivery_types import DeliveryOrigin
from nagents.channels.delivery_types import DeliveryReceipt
from nagents.channels.local_delivery import LocalDeliveryService
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelSendCapabilities
from nagents.channels.types import ChannelValue
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import ApprovalRequest
from nagents.harness.execution import bind_channel_send
from nagents.harness.execution import delivery_origin
from nagents.harness.execution import host_run
from nagents.session.deliveries import DeliveryJournal
from nagents.session.manager import SessionManager
from nagents.types import Message
from tests.session.test_deliveries import CAPS
from tests.session.test_deliveries import SEND
from tests.session.test_deliveries import setup
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness


def test_delivery_receipts_and_notices_do_not_duplicate_content() -> None:
    origin = DeliveryOrigin(
        root_session_id="root",
        actor_session_id="root",
        host_run_id="host-run",
        turn_id="turn",
        task_id="",
        activation=0,
        invocation_id="invocation",
        anchor_message_id=4,
        call_position=1,
        call_id="send",
        tool_name="channel_send",
    )
    receipt = DeliveryReceipt(
        delivery_id="delivery",
        sequence=2,
        channel="builtin.web",
        origin=origin,
        text="large delivery body " * 10000,
        created_at="2026-09-25 12:00:00",
        assets=(DeliveryAsset("asset", 0, "screenshot.png", "image/png", 20 * 1024 * 1024),),
    )
    delivery = receipt.channel_delivery()
    assert delivery.message_ids == ("delivery",)
    assert delivery.metadata == {
        "delivery_id": "delivery",
        "channel": "builtin.web",
        "destination": "root",
        "asset_ids": ["asset"],
    }
    assert receipt.notification() == {
        "delivery_id": "delivery",
        "session_id": "root",
        "channel": "builtin.web",
        "sequence": 2,
    }
    assert len(json.dumps(receipt.notification())) < 256
    assert len(json.dumps(delivery.metadata)) < 256
    notice = receipt.notification()
    notice["delivery_id"] = "changed"
    delivery.metadata.clear()
    assert receipt.notification()["delivery_id"] == "delivery"
    assert receipt.channel_delivery().metadata["asset_ids"] == ["asset"]


@pytest.mark.parametrize("failure", ["exception", "self_cancel", "timeout"])
def test_notification_failure_preserves_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setattr(local_delivery, "NOTIFICATION_TIMEOUT", 0.01)

    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        notices: list[dict[str, ChannelValue]] = []

        def authority(channel: str, destination: str) -> DeliveryOrigin:
            assert (channel, destination) == ("web", "root")
            return origin

        async def notify(notice: dict[str, ChannelValue]) -> None:
            notices.append(notice)
            if failure == "exception":
                raise RuntimeError("notification failed")
            if failure == "self_cancel":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            await asyncio.Event().wait()

        service = LocalDeliveryService(journal, authority)
        delivery = await asyncio.wait_for(service.send("web", SEND, CAPS, notify=notify), HANG_GUARD)
        history = await journal.history("root")
        assert len(history.current) == 1
        receipt = history.current[0]
        assert delivery.message_ids == (receipt.delivery_id,)
        assert notices == [receipt.notification()]
        assert await journal.read_asset("root", receipt.delivery_id, receipt.assets[0].asset_id) == b"saved"

    asyncio.run(run())


def test_authority_failure_happens_before_storage(tmp_path: Path) -> None:
    def deny(channel: str, destination: str) -> DeliveryOrigin:
        raise ChannelError("No live approved invocation")

    async def run() -> None:
        path = tmp_path / "absent.db"
        service = LocalDeliveryService(DeliveryJournal(path), deny)
        with pytest.raises(ChannelError, match="No live approved"):
            await service.send("web", ChannelSend("root", "text"), ChannelSendCapabilities(text=True))
        assert not path.exists()

    asyncio.run(run())


def test_changed_execution_rejected_before_commit(tmp_path: Path) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        checks = 0

        def authority(channel: str, destination: str) -> DeliveryOrigin:
            nonlocal checks
            checks += 1
            return origin if checks == 1 else replace(origin, invocation_id="another-invocation")

        with pytest.raises(ChannelError, match="execution changed"):
            await LocalDeliveryService(journal, authority).send("web", SEND, CAPS)
        assert not (await journal.history("root")).current

    asyncio.run(run())


def test_cancellation_during_notification_retains_committed_delivery(tmp_path: Path) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def notify(notice: dict[str, ChannelValue]) -> None:
            entered.set()
            await release.wait()

        service = LocalDeliveryService(journal, lambda channel, destination: origin)
        task = asyncio.create_task(service.send("web", SEND, CAPS, notify=notify))
        try:
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            history = await journal.history("root")
            assert len(history.current) == 1
            receipt = history.current[0]
            assert await journal.read_asset("root", receipt.delivery_id, receipt.assets[0].asset_id) == b"saved"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.requires_posix
@pytest.mark.parametrize("mode", ["success", "denied", "custom_adapter"])
def test_real_harness_dispatch_to_durable_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    async def run() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                for call_id in ("first", "second"):
                    yield ToolCallEvent(
                        id=call_id,
                        name="channel_send",
                        arguments={
                            "channel": "builtin.web",
                            "destination": harness.session_id,
                            "text": "explicit delivery",
                            "attachments": ["image.png"],
                        },
                    )
            else:
                yield TextDoneEvent(text="ordinary answer")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        if mode == "custom_adapter":
            harness.agent.session = SessionManager(harness.agent.session.db_path)
        await harness.initialize()
        journal = DeliveryJournal(Path(harness.agent.session.db_path))
        service = LocalDeliveryService(
            journal, lambda channel, destination: delivery_origin(harness, channel, destination)
        )
        notices: list[dict[str, ChannelValue]] = []
        failures: list[ChannelError] = []

        async def notify(notice: dict[str, ChannelValue]) -> None:
            notices.append(notice)

        class Sink(Channel):
            name = "builtin.web"

            async def listen(self, receive: ChannelReceiver) -> None:
                pytest.fail("Local delivery must not start a listener")

            async def send(self, message: ChannelSend) -> ChannelDelivery:
                try:
                    return await service.send(
                        self.name,
                        message,
                        ChannelSendCapabilities(text=True, file_media_types=("image/png",)),
                        notify=notify,
                    )
                except ChannelError as error:
                    failures.append(error)
                    raise

        async def approve(request: ApprovalRequest) -> bool:
            return mode != "denied"

        harness.approval_handler = approve
        image = harness.workspace / "image.png"
        image.write_bytes(b"stored image bytes")
        dispatcher = ChannelDispatcher((Sink(),), workspace=harness.workspace)
        try:
            with dispatcher.register_tools(harness.agent.tool_registry) as tools:
                definition = next(tool for tool in tools if tool.name == "channel_send")
                with bind_channel_send(harness, definition), host_run(harness, "host-run"):
                    events = await collect(harness)
            history = await journal.history(harness.session_id)
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert len(results) == 2
            assert any(isinstance(event, TextDoneEvent) and event.text == "ordinary answer" for event in events)
            if mode != "success":
                assert all(result.error for result in results)
                assert not history.current and not notices
                if mode == "custom_adapter":
                    assert failures and all(not failure.outcome_unknown for failure in failures)
                return
            assert all(result.error is None for result in results)
            assert len(history.current) == 2
            first, second = history.current
            assert first.delivery_id != second.delivery_id
            assert first.origin.anchor_message_id == second.origin.anchor_message_id
            assert [receipt.origin.call_position for receipt in history.current] == [0, 1]
            assert {receipt.origin.host_run_id for receipt in history.current} == {"host-run"}
            assert notices == [receipt.notification() for receipt in history.current]
            image.unlink()
            restored = DeliveryJournal(journal.db_path)
            assert await restored.history(harness.session_id) == history
            for receipt in history.current:
                assert (
                    await restored.read_asset(harness.session_id, receipt.delivery_id, receipt.assets[0].asset_id)
                    == b"stored image bytes"
                )
            model_history = await harness.agent.session.get_history(harness.session_id)
            assert not any(
                message.role == "assistant" and message.content == "explicit delivery" for message in model_history
            )
        finally:
            await harness.close()

    asyncio.run(run())
