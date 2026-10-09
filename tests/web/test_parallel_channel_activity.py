"""Parallel roots must not stop another root's transport activity indicator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.channels import ChannelActivity
from tests.web.test_web_channel_notices import fixture

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("same_thread", [False, True])
def test_activity_keeps_other_root_and_shared_destination_active_until_last_owner_ends(
    tmp_path: Path, same_thread: bool
) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as f:
            first = f.run.session_id
            second = await f.state.harness.new_session()
            await f.state.channels.store.assign_owner(second, "fixture", "room-a")
            activities = f.state.channels.activities
            thread = "thread-a" if same_thread else "thread-b"
            await activities.set(
                first, True, {"channel": "fixture", "conversation_id": "room-a", "thread_id": "thread-a"}
            )
            await activities.set(second, True, {"channel": "fixture", "conversation_id": "room-a", "thread_id": thread})
            assert all(event.active for event in f.channel.activities)
            await activities.set(first, False, {})
            assert len(activities.current) == 1
            assert next(iter(activities.current.values()))[1].session_id == second
            if same_thread:
                assert all(event.active for event in f.channel.activities)
            else:
                assert f.channel.activities == [
                    ChannelActivity("room-a", True, "thread-a", first),
                    ChannelActivity("room-a", True, "thread-b", second),
                    ChannelActivity("room-a", False, "thread-a", first),
                ]
            await activities.set(second, False, {})
            assert not activities.current and not activities.sources
            assert f.channel.activities[-1] == ChannelActivity("room-a", False, thread, second)

    asyncio.run(scenario())
