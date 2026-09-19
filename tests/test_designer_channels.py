"""Designed channel routes reuse real web admission, ownership and approvals."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.designer.runtime import DesignProvider
from nagents.designer.schema import STARTER
from nagents.designer.schema import parse
from nagents.designer.schema import serialize
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from tests.test_web_channels import site

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.test_web_channels import Site


def publish(app: Site, source: str) -> None:
    current = app.client.get("/api/designer/designs/my-team", headers=app.headers)
    revision = current.json().get("revision", "") if current.status_code == 200 else ""
    saved = app.client.post("/api/designer/save", headers=app.headers, json={"source": source, "revision": revision})
    assert saved.status_code == 200, saved.text
    response = app.client.post(
        "/api/designer/channels", headers=app.headers, json={"source": source, "revision": saved.json()["revision"]}
    )
    assert response.status_code == 200, response.text


def test_channel_schema_references_and_no_credentials() -> None:
    design = parse(STARTER + "\nchannels:\n  telegram: assistant\n")
    assert parse(serialize(design)).channels == {"telegram": "assistant"}
    assert parse(STARTER + "\nchannels:\n  _telegram.prod: assistant\n").channels == {"_telegram.prod": "assistant"}
    with pytest.raises(ValueError, match="known agents"):
        parse(STARTER + "\nchannels:\n  telegram: missing\n")
    with pytest.raises(ValueError):
        parse(STARTER + "\nchannels:\n  telegram:\n    token: forbidden\n")


@pytest.mark.requires_posix
def test_routed_chat_keeps_pinned_agent_after_publish_restart_and_web_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def generate(self: DesignProvider, messages: list[Message], **kwargs: object) -> AsyncIterator[Event]:
        tools = kwargs.get("tools")
        assert isinstance(tools, list)
        assert {tool.name for tool in tools} == {"channel_list", "channel_send", "channel_action"}
        instructions = "\n".join(str(message.content) for message in messages if message.role == "system")
        seen.append(instructions)
        if messages[-1].role != "tool":
            yield ToolCallEvent(
                id="designed-reply",
                name="channel_send",
                arguments={"channel": "fixture", "destination": "chat-a", "text": "designed answer"},
            )
        else:
            yield TextDoneEvent(text="sent")

    monkeypatch.setattr(DesignProvider, "generate", generate)
    design = parse(STARTER)
    design.channels = {"fixture": "assistant"}
    design.agents["assistant"].instructions.text = "PINNED ORIGINAL INSTRUCTIONS"
    with site(tmp_path, monkeypatch) as app:
        app.configure(auto_reply=True)
        publish(app, serialize(design))
        assert len(app.channels) == 1
        app.emit("hello designed agent")
        app.idle()
        session = app.bindings()["chat-a"]
        assert any("PINNED ORIGINAL INSTRUCTIONS" in text for text in seen)
        assert any(delivery.text == "designed answer" for delivery in app.channels[0].deliveries)
        history = app.history(session)["history"]
        assert isinstance(history, list) and any(row["role"] == "user" for row in history)
        design.agents["assistant"].instructions.text = "NEW INSTRUCTIONS"
        publish(app, serialize(design))
        seen.clear()
        app.submit("web continuation", session)
        app.idle()
        assert seen and all("PINNED ORIGINAL INSTRUCTIONS" in text for text in seen)
        assert app.state.running_harness is app.state.harness
        assert app.state.harness.agent.tool_registry.get("channel_send") in app.state.channels.tools

    seen.clear()
    with site(tmp_path, monkeypatch) as app:
        app.emit("after restart")
        app.idle()
        assert app.bindings()["chat-a"] == session
        assert seen and all("PINNED ORIGINAL INSTRUCTIONS" in text for text in seen)
        app.emit("/new")
        app.idle()
        seen.clear()
        app.emit("new conversation")
        app.idle()
        assert app.bindings()["chat-a"] != session
        assert seen and all("NEW INSTRUCTIONS" in text for text in seen)


@pytest.mark.requires_posix
def test_routes_do_not_replace_existing_conversations_or_steal_other_designs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("existing conversation")
        app.idle()
        design = parse(STARTER)
        design.channels = {"fixture": "assistant"}
        publish(app, serialize(design))
        assert app.client.portal is not None
        assert app.client.portal.call(app.state.designed_channels.pin, app.bindings()["chat-a"]) == ("", "")
        design.id = "other-team"
        response = app.client.post("/api/designer/save", headers=app.headers, json={"source": serialize(design)})
        conflict = app.client.post(
            "/api/designer/channels",
            headers=app.headers,
            json={"source": serialize(design), "revision": response.json()["revision"]},
        )
        assert conflict.status_code == 409
