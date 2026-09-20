"""Protected storage aliases use synthetic files, never real credentials or sessions."""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nagents.harness import Harness
from nagents.harness import HarnessConfig

if TYPE_CHECKING:
    from nagents.harness.tools import CodingTools
    from nagents.harness.types import ApprovalRequest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Guarded file tools require POSIX")
PAYLOAD = '{"access_token":"synthetic-storage-token"}\n'


@pytest.fixture(params=["session", "oauth"])
def storage(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CodingTools, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = workspace / "state"
    xdg = workspace / "xdg"
    auth = xdg / "ngn/auth"
    for path in (data, xdg, auth.parent, auth):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    harness = Harness(HarnessConfig(workspace=workspace, data_dir=data, auth="api-key"))
    protected = data if request.param == "session" else auth
    (protected / "nested").mkdir(mode=0o700)
    for folder in (protected, protected / "nested"):
        file = folder / "openai.json"
        file.write_text(PAYLOAD)
        file.chmod(0o600)
    return harness.tools, protected


async def assert_storage_blocked(tools: CodingTools, path: Path) -> None:
    relative = path.relative_to(tools.root)
    for target in (relative, relative / "nested"):
        with pytest.raises(PermissionError, match="storage"):
            tools.relative(str(target))
        with pytest.raises(PermissionError, match="storage"), tools.directory(target):
            pytest.fail("Protected directory descriptor escaped")
        with pytest.raises(PermissionError, match="storage"):
            await tools.read_file(str(target / "openai.json"))
        with pytest.raises(PermissionError, match="storage"):
            await tools.edit(str(target / "openai.json"), "synthetic-storage-token", "changed")
        with pytest.raises(PermissionError, match="storage"):
            await tools.write(str(target / "new.txt"), "not allowed")
        with pytest.raises(PermissionError, match="storage"):
            await tools.list_files(str(target))
        with pytest.raises(PermissionError, match="storage"):
            await tools.find(path=str(target))
        with pytest.raises(PermissionError, match="storage"):
            await tools.search("synthetic-storage-token", path=str(target))
    listing = await tools.list_files(str(relative.parent))
    assert isinstance(listing["paths"], list)
    assert f"{relative}/" not in listing["paths"]
    found = (await tools.find(path=str(relative.parent)))["paths"]
    assert isinstance(found, list)
    assert not any(str(item).startswith(f"{relative}/") for item in found)
    assert not (await tools.search("synthetic-storage-token", path=str(relative.parent)))["matches"]
    assert (path / "openai.json").read_text() == PAYLOAD
    assert not (path / "new.txt").exists()


def test_actual_case_insensitive_storage_aliases(
    storage: tuple[CodingTools, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, protected = storage
    alias = protected.with_name(protected.name.upper())
    if not alias.exists() or not alias.samefile(protected):
        pytest.skip("This filesystem is case-sensitive")
    # Configure a different spelling too, so scandir's real names miss lexical guards.
    if protected == tools.harness.config.data_dir:
        tools.harness.config.data_dir = alias
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tools.root / "XDG"))
    asyncio.run(assert_storage_blocked(tools, protected))
    asyncio.run(assert_storage_blocked(tools, alias))
    tools.root = protected / "nested"
    with pytest.raises(PermissionError, match="storage"):
        tools.relative("openai.json")


def test_observed_storage_identity_survives_rename(storage: tuple[CodingTools, Path]) -> None:
    tools, protected = storage
    assert tools.relative("public") == Path("public")
    alias = tools.root / "moved-storage"
    protected.rename(alias)
    # Replacing the original pathname must not forget the old protected identity.
    protected.mkdir(mode=0o700)
    asyncio.run(assert_storage_blocked(tools, alias))
    with pytest.raises(PermissionError, match="storage"):
        tools._check_storage(protected.stat())


@pytest.mark.parametrize("at_root", [False, True])
def test_opened_directory_identity_is_checked_and_descriptor_closed(
    storage: tuple[CodingTools, Path], monkeypatch: pytest.MonkeyPatch, at_root: bool
) -> None:
    tools, protected = storage
    (tools.root / "public/nested").mkdir(parents=True)
    real_open = os.open
    opened: list[int] = []

    def alias_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == (tools.root if at_root else "public"):
            descriptor = real_open(protected, flags, mode)
            opened.append(descriptor)
            return descriptor
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", alias_open)
    with pytest.raises(PermissionError, match="storage"), tools.directory(Path("public/nested")):
        pytest.fail("A protected root or intermediate descriptor escaped")
    assert len(opened) == 1
    with pytest.raises(OSError) as error:
        os.fstat(opened[0])
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("operation", ["read", "write", "commit", "list", "find", "search"])
def test_storage_swap_after_inspection_fails_closed(
    storage: tuple[CodingTools, Path], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    tools, protected = storage
    public = tools.root / "public"
    public.mkdir()
    real_open = os.open
    swapped = False

    def raced_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if path == "public" and not swapped:
            public.rename(tools.root / "old-public")
            protected.rename(public)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)

    async def scenario() -> None:
        with pytest.raises(PermissionError, match="storage"):
            if operation == "read":
                await tools.read_file("public/openai.json")
            elif operation == "write":
                await tools.write("public/new.txt", "not allowed")
            elif operation == "commit":
                tools.commit("public/new.txt", None, b"not allowed", 0o644)
            elif operation == "list":
                await tools.list_files("public")
            elif operation == "find":
                await tools.find(path="public")
            else:
                await tools.search("synthetic-storage-token", path="public")

    asyncio.run(scenario())
    assert swapped
    assert (public / "openai.json").read_text() == PAYLOAD
    assert not (public / "new.txt").exists()
    assert not list(public.glob(".ngn-*.tmp"))


@pytest.mark.parametrize("operation", ["write", "edit"])
def test_storage_swap_during_approval_is_rejected(storage: tuple[CodingTools, Path], operation: str) -> None:
    tools, protected = storage
    public = tools.root / "public"
    public.mkdir()
    (public / "openai.json").write_text("ordinary text")
    approved = False

    async def approve(request: ApprovalRequest) -> bool:
        nonlocal approved
        public.rename(tools.root / "old-public")
        protected.rename(public)
        approved = True
        return True

    async def scenario() -> None:
        tools.harness.approval_handler = approve
        await tools.read_file("public/openai.json")
        with pytest.raises(PermissionError, match="storage"):
            if operation == "write":
                await tools.write("public/new.txt", "not allowed")
            else:
                await tools.edit("public/openai.json", "ordinary", "changed")

    asyncio.run(scenario())
    assert approved
    assert (public / "openai.json").read_text() == PAYLOAD
    assert not (public / "new.txt").exists()
    assert not list(public.glob(".ngn-*.tmp"))


def test_workspace_inside_observed_storage_is_blocked(storage: tuple[CodingTools, Path]) -> None:
    tools, protected = storage
    tools.relative("public")
    alias = tools.root / "moved-storage"
    protected.rename(alias)
    tools.root = alias / "nested"
    with pytest.raises(PermissionError, match="storage"):
        tools.relative("openai.json")
    with pytest.raises(PermissionError, match="storage"), tools.directory(Path()):
        pytest.fail("A workspace below protected storage escaped")


def test_storage_stat_errors_fail_closed(storage: tuple[CodingTools, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    tools, protected = storage
    real_stat = Path.stat

    def inaccessible(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == protected:
            raise OSError(errno.EIO, "synthetic metadata failure")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", inaccessible)
    with pytest.raises(OSError, match="synthetic metadata failure"):
        tools.relative("public")
    with pytest.raises(OSError, match="synthetic metadata failure"), tools.directory(Path()):
        pytest.fail("Unavailable storage metadata was ignored")


def test_storage_identity_includes_device(storage: tuple[CodingTools, Path]) -> None:
    tools, protected = storage
    info = protected.stat()
    other_device = os.stat_result(
        (
            info.st_mode,
            info.st_ino,
            info.st_dev + 1,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
            info.st_size,
            info.st_atime,
            info.st_mtime,
            info.st_ctime,
        )
    )
    tools._check_storage(other_device)
    with pytest.raises(PermissionError, match="storage"):
        tools._check_storage(info)


def test_missing_storage_keeps_lexical_guards_and_ordinary_io(
    storage: tuple[CodingTools, Path], tmp_path: Path
) -> None:
    tools, protected = storage
    protected.rename(tmp_path / "removed-storage")
    with pytest.raises(PermissionError, match="storage"):
        tools.relative(str(protected / "new.txt"))

    async def approve(request: ApprovalRequest) -> bool:
        return True

    async def scenario() -> None:
        tools.harness.approval_handler = approve
        await tools.write("ordinary.txt", "public text")
        assert (await tools.read_file("ordinary.txt"))["content"] == "1: public text"
        await tools.edit("ordinary.txt", "public", "updated")
        assert (tools.root / "ordinary.txt").read_text() == "updated text"
        with pytest.raises(PermissionError, match="traversal"):
            tools.relative("../outside.txt")
        with pytest.raises(PermissionError, match="Protected path"):
            tools.relative(".env.synthetic")
        (tools.root / "link").symlink_to(tmp_path / "removed-storage", target_is_directory=True)
        with pytest.raises(PermissionError, match="Symlinks"):
            await tools.read_file("link/openai.json")
        (tools.root / "hardlink.json").hardlink_to(tmp_path / "removed-storage/openai.json")
        with pytest.raises(PermissionError, match="single-link"):
            await tools.read_file("hardlink.json")

    asyncio.run(scenario())
