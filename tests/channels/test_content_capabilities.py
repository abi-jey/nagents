"""Opt-in content declarations stay independent, bounded and backward compatible."""

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Literal
from typing import cast

import pytest

from nagents.channels import Channel
from nagents.channels import ChannelContentCapabilities
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelError
from nagents.channels import ChannelReceiveCapabilities
from nagents.channels import ChannelReceiver
from nagents.channels import ChannelRenderCapabilities
from nagents.channels import ChannelSend
from nagents.channels import ChannelSendCapabilities
from nagents.channels import ChannelValue
from nagents.channels.dispatcher import ChannelDispatcher


class Sink(Channel):
    name = "sink"
    capabilities = ("send_text", "send_files")

    def __init__(self, descriptor: ChannelContentCapabilities | None = None) -> None:
        self.content_capabilities = descriptor
        self.sent: list[ChannelSend] = []

    async def listen(self, receive: ChannelReceiver) -> None:
        raise AssertionError("Discovery and dispatch must not start a listener")

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        self.sent.append(message)
        return ChannelDelivery((f"delivery-{len(self.sent)}",))


def run_send(operation: Awaitable[dict[str, ChannelValue]]) -> dict[str, ChannelValue]:
    async def drive() -> dict[str, ChannelValue]:
        return await operation

    return asyncio.run(drive())


def test_legacy_catalog_and_unrestricted_connector_dispatch_are_preserved(tmp_path: Path) -> None:
    channel = Sink()
    dispatcher = ChannelDispatcher((channel,), workspace=tmp_path)
    assert dispatcher.catalog() == [
        {"name": "sink", "description": "", "capabilities": ["send_text", "send_files"], "actions": []}
    ]
    (tmp_path / "opaque.bin").write_bytes(b"opaque data")
    run_send(dispatcher.channel_send("sink", "room", "caption", attachments=["opaque.bin"]))
    assert len(channel.sent) == 1


def test_catalog_expresses_input_delivery_and_rendering_independently() -> None:
    channel = Sink(
        ChannelContentCapabilities(
            receive=ChannelReceiveCapabilities(via="host", text=True),
            send=ChannelSendCapabilities(text=True, file_media_types=("IMAGE/PNG",), max_files=2),
            render=ChannelRenderCapabilities(file_media_types=("video/mp4",), playback="browser_dependent"),
        )
    )
    assert ChannelDispatcher((channel,)).catalog()[0]["content_capabilities"] == {
        "receive": {"via": "host", "text": True, "file_media_types": []},
        "send": {"text": True, "file_media_types": ["image/png"], "max_files": 2},
        "render": {"text": False, "file_media_types": ["video/mp4"], "playback": "browser_dependent"},
    }


@pytest.mark.parametrize(
    "descriptor",
    [
        ChannelContentCapabilities(),
        ChannelContentCapabilities(receive=ChannelReceiveCapabilities()),
        ChannelContentCapabilities(render=ChannelRenderCapabilities()),
    ],
)
def test_undeclared_send_is_not_inferred_from_other_directions(descriptor: ChannelContentCapabilities) -> None:
    channel = Sink(descriptor)
    run_send(ChannelDispatcher((channel,)).channel_send("sink", "room", "text"))
    assert len(channel.sent) == 1


def test_binding_snapshots_descriptor_and_returns_detached_discovery() -> None:
    channel = Sink(ChannelContentCapabilities(send=ChannelSendCapabilities(text=True)))
    dispatcher = ChannelDispatcher((channel,))
    catalog = dispatcher.catalog()
    descriptor = catalog[0]["content_capabilities"]
    assert isinstance(descriptor, dict)
    direction = descriptor["send"]
    assert isinstance(direction, dict)
    direction["text"] = False
    channel.content_capabilities = ChannelContentCapabilities(send=ChannelSendCapabilities())
    run_send(dispatcher.channel_send("sink", "room", "allowed by the bound snapshot"))
    with pytest.raises(ChannelError, match="does not support text"):
        run_send(ChannelDispatcher((channel,)).channel_send("sink", "room", "new binding"))
    assert len(channel.sent) == 1


@pytest.mark.parametrize(
    "descriptor",
    [
        cast("ChannelContentCapabilities", {"send": {"text": True}, "arbitrary": {}}),
        ChannelContentCapabilities(send=cast("ChannelSendCapabilities", {"text": True})),
        ChannelContentCapabilities(receive=cast("ChannelReceiveCapabilities", ChannelSendCapabilities())),
        ChannelContentCapabilities(render=cast("ChannelRenderCapabilities", ChannelSendCapabilities())),
        ChannelContentCapabilities(send=ChannelSendCapabilities(text=cast("bool", 1))),
        ChannelContentCapabilities(receive=ChannelReceiveCapabilities(text=cast("bool", "yes"))),
        ChannelContentCapabilities(render=ChannelRenderCapabilities(text=cast("bool", 0))),
        ChannelContentCapabilities(receive=ChannelReceiveCapabilities(via=cast('Literal["host", "listen"]', "queue"))),
        ChannelContentCapabilities(
            render=ChannelRenderCapabilities(playback=cast('Literal["none", "browser_dependent"]', "guaranteed"))
        ),
    ],
)
def test_invalid_fixed_fields_are_rejected_when_binding(descriptor: ChannelContentCapabilities) -> None:
    with pytest.raises(ChannelError):
        ChannelDispatcher((Sink(descriptor),))


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "3", 2**63])
@pytest.mark.parametrize("field", ["max_files", "max_file_bytes", "max_total_bytes"])
def test_invalid_limits_are_rejected_for_receive_and_send(field: str, limit: object) -> None:
    for direction in (ChannelReceiveCapabilities(), ChannelSendCapabilities()):
        # Deliberately emulate malformed declarations from dynamically typed connectors.
        object.__setattr__(direction, field, limit)
        descriptor = (
            ChannelContentCapabilities(receive=direction)
            if isinstance(direction, ChannelReceiveCapabilities)
            else ChannelContentCapabilities(send=direction)
        )
        with pytest.raises(ChannelError, match="positive"):
            ChannelDispatcher((Sink(descriptor),))


@pytest.mark.parametrize(
    "media_types",
    [
        ["image/png"],
        (True,),
        ("",),
        ("image/*",),
        ("image/png; charset=utf-8",),
        ("image/png\n",),
        ("image/\u0130",),
        ("image/png", "IMAGE/PNG"),
        ("image/" + "x" * 122,),
        tuple(f"image/x-{index}" for index in range(33)),
    ],
)
def test_malformed_or_unbounded_mime_lists_fail_binding(media_types: object) -> None:
    descriptor = ChannelContentCapabilities(
        send=ChannelSendCapabilities(file_media_types=cast("tuple[str, ...]", media_types))
    )
    with pytest.raises(ChannelError):
        ChannelDispatcher((Sink(descriptor),))


def test_total_descriptor_size_is_bounded_across_directions() -> None:
    media_types = tuple("application/" + str(index) + "x" * 105 for index in range(32))
    descriptor = ChannelContentCapabilities(
        receive=ChannelReceiveCapabilities(file_media_types=media_types),
        send=ChannelSendCapabilities(file_media_types=media_types),
        render=ChannelRenderCapabilities(file_media_types=media_types),
    )
    with pytest.raises(ChannelError, match="8 KiB"):
        ChannelDispatcher((Sink(descriptor),))


def test_text_field_support_does_not_permit_text_files_or_partial_delivery(tmp_path: Path) -> None:
    channel = Sink(ChannelContentCapabilities(send=ChannelSendCapabilities(text=True)))
    (tmp_path / "note.txt").write_text("attachment")
    with pytest.raises(ChannelError, match="attachment media type") as failure:
        run_send(
            ChannelDispatcher((channel,), workspace=tmp_path).channel_send(
                "sink", "room", "caption", attachments=["note.txt"]
            )
        )
    assert not failure.value.outcome_unknown
    assert failure.value.retry_after == 0
    assert channel.sent == []


def test_image_only_delivery_does_not_imply_text_support(tmp_path: Path) -> None:
    channel = Sink(ChannelContentCapabilities(send=ChannelSendCapabilities(file_media_types=("image/png",))))
    dispatcher = ChannelDispatcher((channel,), workspace=tmp_path)
    (tmp_path / "image.png").write_bytes(b"image bytes")
    run_send(dispatcher.channel_send("sink", "room", "", attachments=["image.png"]))
    with pytest.raises(ChannelError, match="does not support text"):
        run_send(dispatcher.channel_send("sink", "room", "caption", attachments=["image.png"]))
    assert len(channel.sent) == 1


@pytest.mark.parametrize("field,limit", [("max_files", 1), ("max_file_bytes", 2), ("max_total_bytes", 4)])
def test_declared_limits_are_enforced_before_any_delivery(tmp_path: Path, field: str, limit: int) -> None:
    support = ChannelSendCapabilities(text=True, file_media_types=("image/png",))
    object.__setattr__(support, field, limit)
    channel = Sink(ChannelContentCapabilities(send=support))
    (tmp_path / "first.png").write_bytes(b"12")
    (tmp_path / "second.png").write_bytes(b"345")
    with pytest.raises(ChannelError, match=field) as failure:
        run_send(
            ChannelDispatcher((channel,), workspace=tmp_path).channel_send(
                "sink", "room", "caption", attachments=["first.png", "second.png"]
            )
        )
    assert not failure.value.outcome_unknown
    assert channel.sent == []


def test_declared_limits_allow_the_exact_boundary(tmp_path: Path) -> None:
    support = ChannelSendCapabilities(
        text=True, file_media_types=("image/png",), max_files=2, max_file_bytes=3, max_total_bytes=5
    )
    channel = Sink(ChannelContentCapabilities(send=support))
    (tmp_path / "first.png").write_bytes(b"12")
    (tmp_path / "second.png").write_bytes(b"345")
    run_send(
        ChannelDispatcher((channel,), workspace=tmp_path).channel_send(
            "sink", "room", "caption", attachments=["first.png", "second.png"]
        )
    )
    assert len(channel.sent) == 1
    assert sum(len(file.data) for file in channel.sent[0].files) == 5


def test_empty_declared_send_rejects_text_without_side_effects() -> None:
    channel = Sink(ChannelContentCapabilities(send=ChannelSendCapabilities()))
    with pytest.raises(ChannelError, match="does not support text"):
        run_send(ChannelDispatcher((channel,)).channel_send("sink", "room", "no"))
    assert channel.sent == []


def test_declared_limits_cannot_relax_host_file_caps(tmp_path: Path) -> None:
    channel = Sink(
        ChannelContentCapabilities(send=ChannelSendCapabilities(file_media_types=("image/png",), max_files=4))
    )
    paths = [f"{index}.png" for index in range(4)]
    for path in paths:
        (tmp_path / path).write_bytes(b"image")
    with pytest.raises(ChannelError, match="At most 3"):
        run_send(ChannelDispatcher((channel,), workspace=tmp_path).channel_send("sink", "room", "", attachments=paths))
    assert channel.sent == []


def test_file_type_limit_boundary_can_be_declared() -> None:
    support = ChannelSendCapabilities(file_media_types=("image/" + "x" * 121,), max_files=1)
    channel = Sink(ChannelContentCapabilities(send=support))
    assert "content_capabilities" in ChannelDispatcher((channel,)).catalog()[0]
