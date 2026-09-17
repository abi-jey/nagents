"""In-chat approval decisions: strict correlation, host policy, and no model input."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.channels import Channel
from nagents.channels import ChannelApproval
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelMessage
from nagents.channels import ChannelSend
from nagents.channels.runtime import _envelope
from nagents.harness.config import HarnessConfig
from nagents.harness.types import ApprovalRequest
from nagents.web.catalog import Connection
from nagents.web.service import Pending
from nagents.web.service import Run
from nagents.web.service import WebState
from tests.hang_guard import HANG_GUARD
from tests.test_web import ControlledHarness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.channels import ChannelReceiver


class ApprovalChannel(Channel):
    name = "fixture"

    def __init__(self, capabilities: tuple[str, ...] = ("receive", "send_text", "approvals")) -> None:
        self.capabilities = capabilities
        self.queue: asyncio.Queue[ChannelMessage] = asyncio.Queue()

    async def listen(self, receive: ChannelReceiver) -> None:
        while True:
            await receive(await self.queue.get())

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        raise AssertionError("Approval decisions must never send transport text")

    def approval(self, message: ChannelMessage) -> ChannelApproval | None:
        callback = message.metadata.get("callback")
        if not isinstance(callback, dict):
            return None
        return ChannelApproval(
            conversation_id=str(callback.get("conversation_id", "")),
            session_id=str(callback.get("session_id", "")),
            run_id=str(callback.get("run_id", "")),
            call_id=str(callback.get("call_id", "")),
            allow=callback.get("allow") is True,
        )


@dataclass
class Fixture:
    state: WebState
    run: Run
    channel: ApprovalChannel


@asynccontextmanager
async def fixture(
    path: Path, *, chat_approvals: bool = True, capabilities: tuple[str, ...] = ("receive", "send_text", "approvals")
) -> AsyncIterator[Fixture]:
    harness = ControlledHarness(HarnessConfig(workspace=path, data_dir=path / "data", demo=True))
    await harness.initialize()
    state = WebState(harness)
    host = state.channels
    await host.store.initialize()
    await host.store.receive(
        "fixture",
        _envelope("fixture", ChannelMessage("ingress", "room-a", "participant", "hello")),
        harness.session_id,
        None,
    )
    work = await host.store.claim_work()
    assert work is not None
    await host.store.finish_work(work, "completed")
    await harness.resume(work.session_id)
    harness.config.demo = False
    host.catalog.allow_plugins = True
    host.catalog.connections["fixture"] = Connection(
        plugin="fixture",
        enabled=True,
        auto_reply=True,
        chat_approvals=chat_approvals,
        config={},
        secrets={},
        main_session_id=work.session_id,
    )
    host.catalog.status["fixture"] = ("running", "")
    channel = ApprovalChannel(capabilities)
    host.channels["fixture"] = channel
    source = asyncio.create_task(host.source("fixture", channel), name="fixture-source")
    host.sources["fixture"] = source
    run = Run(work.session_id)
    task = asyncio.current_task()
    assert task is not None
    run.task = task
    state.active = run
    try:
        yield Fixture(state, run, channel)
    finally:
        state.active = None
        source.cancel()
        with suppress(asyncio.CancelledError):
            await source
        await host.activities.close()
        await harness.close()


def install_pending(f: Fixture, *, call_id: str = "call-1") -> Pending:
    request = ApprovalRequest(id=call_id, tool="read_file", arguments={"path": "README.md"}, description="")
    pending = Pending("approval", request.id, asyncio.get_running_loop().create_future())
    pending.record = {**asdict(request), "approval_id": pending.id, "run_id": f.run.id}
    f.run.pending = pending
    return pending


async def tap(
    f: Fixture,
    *,
    conversation_id: str = "room-a",
    session_id: str = "",
    run_id: str = "",
    call_id: str = "call-1",
    allow: bool = True,
) -> None:
    await f.channel.queue.put(
        ChannelMessage(
            "callback-1",
            conversation_id,
            "participant",
            metadata={
                "callback": {
                    "conversation_id": conversation_id,
                    "session_id": session_id or f.run.session_id,
                    "run_id": run_id or f.run.id,
                    "call_id": call_id,
                    "allow": allow,
                }
            },
        )
    )


class _Plain(Channel):
    name = "plain"

    async def listen(self, receive: ChannelReceiver) -> None:
        return None

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        raise AssertionError("plain fixture never sends")


def test_channel_approval_is_strict_and_default_hook_declines() -> None:
    assert _Plain().approval(ChannelMessage("m", "c", "s")) is None
    assert ChannelApproval().conversation_id == ""
    with pytest.raises(ValueError):
        ChannelApproval(conversation_id=cast("str", 1))
    with pytest.raises(ValueError):
        ChannelApproval(call_id="x" * 257)
    with pytest.raises(ValueError):
        ChannelApproval(allow=cast("bool", 1))


def test_in_chat_approvals_requires_policy_capability_and_live_source(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            host = f.state.channels
            assert await host.in_chat_approvals(f.run.session_id)
            host.catalog.connections["fixture"].chat_approvals = False
            assert not await host.in_chat_approvals(f.run.session_id)
            host.catalog.connections["fixture"].chat_approvals = True
            f.channel.capabilities = ("receive", "send_text")
            assert not await host.in_chat_approvals(f.run.session_id)

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_owning_chat_decision_resolves_pending_without_model_input(tmp_path: Path, decision: str) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            pending = install_pending(f)
            await tap(f, allow=decision == "allow")
            assert await asyncio.wait_for(pending.answer, HANG_GUARD) is (decision == "allow")
            assert not await f.state.channels.store.has_pending()
            assert f.run.pending is pending  # Cleared by the approval handler, not the channel.

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["conversation", "session", "run", "call", "previous-run"])
def test_unrelated_or_stale_decision_is_ignored(tmp_path: Path, mismatch: str) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            pending = install_pending(f)
            conversation, session, run, call = "room-a", f.run.session_id, f.run.id, "call-1"
            if mismatch == "conversation":
                conversation = "room-b"
            elif mismatch == "session":
                session = "other-session"
            elif mismatch == "run":
                run = "previous-run"
            elif mismatch == "call":
                call = "other-call"
            else:
                pending.call_id = "other-call"
            await tap(f, conversation_id=conversation, session_id=session, run_id=run, call_id=call)
            await asyncio.sleep(0.05)
            assert not pending.answer.done()
            assert not await f.state.channels.store.has_pending()

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["chat_approvals", "capability", "disabled"])
def test_decision_requires_host_policy_and_capability(tmp_path: Path, field: str) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            connection = f.state.channels.catalog.connections["fixture"]
            if field == "chat_approvals":
                connection.chat_approvals = False
            elif field == "capability":
                f.channel.capabilities = ("receive", "send_text")
            else:
                connection.enabled = False
            pending = install_pending(f)
            await tap(f)
            await asyncio.sleep(0.05)
            assert not pending.answer.done()
            assert not await f.state.channels.store.has_pending()

    asyncio.run(scenario())


def test_stale_interaction_claimed_by_connector_is_never_admitted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            await tap(f, call_id="")
            await asyncio.sleep(0.05)
            assert not await f.state.channels.store.has_pending()

    asyncio.run(scenario())


def test_ordinary_message_without_decision_still_reaches_the_inbox(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            await f.channel.queue.put(ChannelMessage("plain-1", "room-a", "participant", "hello again"))
            for _ in range(50):
                if await f.state.channels.store.has_pending():
                    break
                await asyncio.sleep(0.02)
            assert await f.state.channels.store.has_pending()

    asyncio.run(scenario())


@pytest.mark.parametrize("in_chat", [True, False])
def test_disconnected_only_denies_when_a_browser_approval_is_required(tmp_path: Path, in_chat: bool) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            f.run.server_owned = True
            pending = install_pending(f)
            pending.in_chat = in_chat
            f.state.disconnected()
            assert pending.answer.done() is not in_chat
            if in_chat:
                pending.answer.set_result(False)

    asyncio.run(scenario())


def test_approve_without_a_browser_subscriber_uses_the_owning_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            f.run.server_owned = True
            f.state.approval_timeout = lambda: 5
            request = ApprovalRequest(id="call-1", tool="read_file", arguments={}, description="")

            async def decide() -> None:
                for _ in range(500):
                    if f.run.pending is not None:
                        break
                    await asyncio.sleep(0.01)
                await tap(f)

            decider = asyncio.create_task(decide())
            assert await f.state.approve(request) is True
            await decider
            assert f.run.pending is None

    asyncio.run(scenario())
