"""Unstarted claims and unavailable command transports must retain accepted work."""

import asyncio
from pathlib import Path

import aiosqlite

from nagents.channels.runtime import _envelope
from nagents.channels.types import ChannelCommand
from nagents.channels.types import ChannelMessage
from nagents.session import SessionManager
from nagents.web.routing import RoutingStore


async def store_at(path: Path) -> RoutingStore:
    session = SessionManager(path)
    await session.get_or_create_session("ngn-main", "harness")
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS harness_sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        await db.execute("INSERT OR IGNORE INTO harness_sessions VALUES ('ngn-main', 'Main')")
        await db.commit()
    store = RoutingStore(path)
    await store.initialize()
    return store


def test_disabled_ack_does_not_block_web_and_unstarted_claim_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "routing.db"
        store = await store_at(path)
        envelope = _envelope("telegram", ChannelMessage("command", "chat", "sender", "/session main"))
        await store.receive("telegram", envelope, "ngn-main", ChannelCommand("session", "main"))
        assert not await store.has_pending(available_channels=())
        assert await store.claim_work(available_channels=()) is None
        await store.web("ngn-main", "web-message", "Independent work")
        work = await store.claim_work(available_channels=())
        assert work is not None and work.channel == "" and work.message_id == "web-message"
        await store.release_work(work)  # Shutdown won the race before execution started.
        reopened = await store_at(path)
        again = await reopened.claim_work(available_channels=())
        assert again == work
        await reopened.finish_work(again, "completed")
        assert not await reopened.has_pending(available_channels=())
        ack = await reopened.claim_work(available_channels=("telegram",))
        assert ack is not None and ack.message_id == "command" and ack.acknowledgement == "Session: ngn-main"
        await reopened.finish_work(ack, "completed")
        await reopened.receive("telegram", envelope, "ngn-main", ChannelCommand("session", "main"))
        assert not await reopened.has_pending(available_channels=("telegram",))

    asyncio.run(scenario())


def test_sessions_command_remains_sendable_with_many_unicode_titles(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "catalog.db"
        store = await store_at(path)
        async with aiosqlite.connect(path) as db:
            for number in range(100):
                id = f"ngn-session-{number}"
                await db.execute("INSERT INTO v2_sessions(id,user_id) VALUES (?, 'harness')", (id,))
                await db.execute("INSERT INTO harness_sessions VALUES (?, ?)", (id, "😀" * 80))
            await db.commit()
        envelope = _envelope("telegram", ChannelMessage("list", "chat", "sender", "/sessions"))
        await store.receive("telegram", envelope, "ngn-main", ChannelCommand("sessions"))
        work = await store.claim_work(available_channels=("telegram",))
        assert work is not None
        assert len(work.acknowledgement.encode("utf-16-le")) // 2 <= 4096
        assert "More sessions are available in the web UI." in work.acknowledgement

    asyncio.run(scenario())
