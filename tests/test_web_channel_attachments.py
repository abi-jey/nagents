"""The web host formats inbound channel events and delivers native attachments."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.channels.types import ChannelAttachment
from nagents.channels.types import ChannelMessage
from nagents.events import TextDoneEvent
from nagents.types import ImageContent
from nagents.types import TextContent
from tests.test_web_channels import site
from tests.test_web_routing import users

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.test_subagents import FakeProvider
    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix

PNG = b"\x89PNG\r\n\x1a\noffline-image"


async def echo(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="seen")


def deliver_image(app: Site, reference: str = "asset", *, configured: bool = True) -> ChannelMessage:
    channel = app.channels[0]
    if configured:
        channel.attachments[reference] = (PNG, "image/png")
    return ChannelMessage(
        "evt-1",
        "chat-a",
        "sender",
        "look at this",
        "",
        "remote-id",
        "message",
        (ChannelAttachment(reference, "image/png", "picture.png", len(PNG)),),
        {"update_id": 1},
        sent_at=1757890872.0,
        sender_name="Abbas Jafari",
        sender_username="realabja",
        conversation_type="private",
    )


def test_web_channel_attachment_reaches_the_model_as_a_native_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.providers[0].script = echo
        assert app.client.portal is not None
        app.client.portal.call(app.channels[0].emit, deliver_image(app))
        app.idle()
        request = app.providers[0].requests[0]
        user = next(item for item in request if item.role == "user")
        assert isinstance(user.content, list)
        header = user.content[0]
        assert isinstance(header, TextContent)
        assert header.text.startswith("[2025-09-14T23:01:12Z] fixture · private")
        assert (
            "Abbas Jafari (@realabja) · user sender · chat chat-a · message evt-1 · reply remote-id · text+image"
            in header.text
        )
        assert header.text.endswith("\n\nlook at this")
        assert "JSON envelope" not in header.text
        assert user.content[1] == ImageContent(
            base64_data=base64.b64encode(PNG).decode("ascii"), media_type="image/png"
        )


def test_web_history_exposes_channel_parts_and_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.providers[0].script = echo
        assert app.client.portal is not None
        app.client.portal.call(app.channels[0].emit, deliver_image(app))
        app.idle()
        row = users(app, app.bindings()["chat-a"])[-1]
        assert row["source_verified"] is True
        assert row["content"] == "look at this"
        parts = cast("list[dict[str, object]]", row["parts"])
        assert [part["type"] for part in parts] == ["text", "image"]
        assert parts[1]["media_type"] == "image/png"
        assert parts[1]["data_base64"] == base64.b64encode(PNG).decode("ascii")
        source = cast("dict[str, object]", row["source"])
        assert source["sender_name"] == "Abbas Jafari"
        assert source["sender_username"] == "realabja"
        assert source["conversation_type"] == "private"
        assert source["attachments"] == [
            {"reference": "asset", "media_type": "image/png", "filename": "picture.png", "size": len(PNG)}
        ]
        messages = cast(
            "list[Message]",
            app.client.portal.call(app.state.harness.agent.session.get_history, app.bindings()["chat-a"]),
        )
        assert isinstance(messages[0].content, list)


def test_web_channel_runtime_receives_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        runtime = app.state.channels.runtime
        assert runtime is not None
        assert runtime.workspace == app.state.harness.workspace


def test_web_channel_send_exposes_and_forwards_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        # The web host registers its own channel_send wrapper; it must advertise
        # attachments, or the model never sees the parameter.
        tool = app.state.harness.agent.tool_registry.get("channel_send")
        assert tool is not None
        attachments = tool.parameters["properties"]["attachments"]
        assert attachments["type"] == "array" and attachments["items"]["type"] == "string"
        workspace = app.state.harness.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "note.txt").write_text("hello", encoding="utf-8")
        assert app.client.portal is not None
        result = app.client.portal.call(app.state.channels.channel_send, "fixture", "chat-a", "", "", "", ["note.txt"])
        assert result["message_ids"]
        delivery = app.channels[0].deliveries[-1]
        assert [item.filename for item in delivery.files] == ["note.txt"]
        assert delivery.files[0].data == b"hello"


def test_failed_fetch_stays_a_bounded_note_in_model_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.providers[0].script = echo
        assert app.client.portal is not None
        app.client.portal.call(app.channels[0].emit, deliver_image(app, configured=False))
        app.idle()
        user = next(item for item in app.providers[0].requests[0] if item.role == "user")
        assert isinstance(user.content, str)
        assert user.content.endswith(
            f"[attachment 1: picture.png · image/png · {len(PNG)} B · Fixture attachment is unavailable]"
        )
