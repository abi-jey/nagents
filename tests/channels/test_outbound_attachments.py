"""Outbound workspace attachments: bounded resolution and connector dispatch."""

import asyncio
from pathlib import Path

import pytest

from nagents.channels.runtime import MAX_OUTBOUND_FILES
from nagents.channels.runtime import ChannelRuntime
from nagents.channels.runtime import _outbound_files
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelFile
from nagents.channels.types import ChannelSend
from tests.channels.test_channels_runtime import MemoryChannel
from tests.channels.test_channels_runtime import OfflineProvider
from tests.channels.test_channels_runtime import make_agent

PDF = b"%PDF-1.7 offline"


def test_reads_a_workspace_file_and_infers_the_media_type(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_bytes(PDF)
    assert _outbound_files(tmp_path, ["report.pdf"]) == (ChannelFile("report.pdf", "application/pdf", PDF),)


def test_unknown_extensions_use_the_opaque_media_type(tmp_path: Path) -> None:
    (tmp_path / "blob.zzz").write_bytes(b"data")
    assert _outbound_files(tmp_path, ["blob.zzz"]) == (ChannelFile("blob.zzz", "application/octet-stream", b"data"),)


def test_no_paths_is_an_empty_tuple(tmp_path: Path) -> None:
    assert _outbound_files(tmp_path, None) == ()
    assert _outbound_files(tmp_path, []) == ()


@pytest.mark.parametrize("paths", ["report.pdf", b"report.pdf", ["a", 1], {"a": 1}])
def test_malformed_path_lists_are_rejected(tmp_path: Path, paths: object) -> None:
    with pytest.raises(ChannelError, match="workspace-relative"):
        _outbound_files(tmp_path, paths)


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/hosts", "nested/../../escape.txt", "", "."])
def test_absolute_and_traversal_paths_are_rejected(tmp_path: Path, path: str) -> None:
    with pytest.raises(ChannelError):
        _outbound_files(tmp_path, [path])


def test_missing_and_directory_paths_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    with pytest.raises(ChannelError, match="not a regular file"):
        _outbound_files(tmp_path, ["missing.txt"])
    with pytest.raises(ChannelError, match="not a regular file"):
        _outbound_files(tmp_path, ["folder"])


def test_oversized_and_excess_files_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * (20 * 1024 * 1024 + 1))
    with pytest.raises(ChannelError, match="per-file size limit"):
        _outbound_files(tmp_path, ["big.bin"])
    for index in range(MAX_OUTBOUND_FILES + 1):
        (tmp_path / f"f{index}.txt").write_text("x")
    with pytest.raises(ChannelError, match="At most"):
        _outbound_files(tmp_path, [f"f{index}.txt" for index in range(MAX_OUTBOUND_FILES + 1)])


def test_a_workspace_is_required_for_any_attachment(tmp_path: Path) -> None:
    with pytest.raises(ChannelError, match="workspace"):
        _outbound_files(None, ["report.pdf"])


@pytest.mark.requires_posix
def test_symlinked_paths_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    with pytest.raises(ChannelError, match="symlink"):
        _outbound_files(tmp_path, ["link.txt"])


def test_channel_send_dispatches_workspace_files(tmp_path: Path) -> None:
    async def drive() -> None:
        (tmp_path / "note.txt").write_text("hello")
        channel = MemoryChannel("left")
        agent = make_agent(tmp_path / "outbound.db", OfflineProvider())
        runtime = ChannelRuntime(agent, (channel,), "identity", workspace=tmp_path)
        runtime._active = True
        try:
            result = await runtime._channel_send(channel="left", destination="room", text="", attachments=["note.txt"])
            assert result["message_ids"] == ["remote-1"]
            assert channel.sent == [
                ChannelSend(destination="room", text="", files=(ChannelFile("note.txt", "text/plain", b"hello"),))
            ]
        finally:
            await agent.close()

    asyncio.run(drive())


def test_channel_send_rejects_attachment_escapes(tmp_path: Path) -> None:
    async def drive() -> None:
        channel = MemoryChannel("left")
        agent = make_agent(tmp_path / "escape.db", OfflineProvider())
        runtime = ChannelRuntime(agent, (channel,), "identity", workspace=tmp_path)
        runtime._active = True
        try:
            with pytest.raises(ChannelError):
                await runtime._channel_send(
                    channel="left", destination="room", text="x", attachments=["../outside.txt"]
                )
            assert channel.sent == []
        finally:
            await agent.close()

    asyncio.run(drive())


def test_channel_send_requires_text_or_files(tmp_path: Path) -> None:
    async def drive() -> None:
        channel = MemoryChannel("left")
        agent = make_agent(tmp_path / "blank.db", OfflineProvider())
        runtime = ChannelRuntime(agent, (channel,), "identity", workspace=tmp_path)
        runtime._active = True
        try:
            with pytest.raises(ChannelError):
                await runtime._channel_send(channel="left", destination="room", text="")
            assert channel.sent == []
        finally:
            await agent.close()

    asyncio.run(drive())
