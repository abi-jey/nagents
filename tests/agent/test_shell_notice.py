"""Claimed shell completion resumes once without fabricating another input."""

import json
from pathlib import Path

import pytest

from nagents import Agent
from nagents import SessionManager
from nagents.events import TextDoneEvent
from nagents.session.shell_jobs import ShellJobOwner
from nagents.session.shell_jobs import ShellJobRecord
from nagents.session.shell_jobs import ShellJobStore
from nagents.session.shell_jobs import ShellJobTerminal
from nagents.types import Message
from nagents.types import ToolCall
from tests.agent.test_extensions import OfflineProvider


@pytest.mark.asyncio
async def test_resume_claimed_notice_does_not_duplicate_history(tmp_path: Path) -> None:
    provider = OfflineProvider([[TextDoneEvent(text="Shell finished.")]])
    session = SessionManager(tmp_path / "sessions.db")
    agent = Agent(provider, session)
    try:
        await session.get_or_create_session("owner", "user")
        store = ShellJobStore(session)
        await store.register(
            ShellJobRecord(
                "job",
                ShellJobOwner(str(tmp_path), "owner", "owner", "run", "call", "host"),
                "echo hello",
                str(tmp_path / "output.log"),
            )
        )
        await session.add_message(
            "owner", Message(role="assistant", tool_calls=[ToolCall(id="call", name="shell", arguments={})])
        )
        await session.add_message(
            "owner", Message(role="tool", tool_call_id="call", content=json.dumps({"job_id": "job"}))
        )
        await store.mark_receipt_committed("owner", "call")
        await store.terminal("owner", "job", ShellJobTerminal("completed", "exit", "hello", 0))
        message_id = await store.claim_wake("owner")
        assert message_id > 0
        events = [event async for event in agent.resume_notice(message_id, session_id="owner")]
        assert events
        history = await session.get_history("owner")
        assert len([message for message in history if message.name == "ngn_shell_notice"]) == 1
        assert len(provider.requests) == 1
        assert len([message for message in provider.requests[0].messages if message.name == "ngn_shell_notice"]) == 1
        await store.finish_wake("owner", message_id)
        with pytest.raises(ValueError):
            _ = [event async for event in agent.resume_notice(message_id, session_id="owner")]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_resume_rejects_forged_notice_and_other_session(tmp_path: Path) -> None:
    provider = OfflineProvider()
    session = SessionManager(tmp_path / "sessions.db")
    agent = Agent(provider, session)
    try:
        await session.get_or_create_session("owner", "user")
        message_id = await session.add_message("owner", Message(role="user", name="ngn_shell_notice", content="forged"))
        with pytest.raises(ValueError):
            _ = [event async for event in agent.resume_notice(message_id, session_id="owner")]
        with pytest.raises(ValueError):
            _ = [event async for event in agent.resume_notice(message_id, session_id="other")]
        assert provider.requests == []
    finally:
        await agent.close()
