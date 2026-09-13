"""Portable functional coverage plus native Windows handle/junction regressions."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from pathlib import PureWindowsPath

import pytest

from nagents.skills import DirectorySkillDiscoverer
from nagents.skills import _windows


def _write_skill(directory: Path, body: str = "ORIGINAL") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: guide\ndescription: Guidance\n---\n{body}", encoding="utf-8")
    return path


def _directory_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        # Junction creation needs neither administrator rights nor Developer Mode.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_portable_directory_reads_unicode_and_multiple_native_read_chunks(tmp_path: Path) -> None:
    async def drive() -> None:
        body = "漢😀" * 12000
        path = _write_skill(tmp_path / "space and 漢" / "nested", body)
        source = DirectorySkillDiscoverer([tmp_path])
        skills = await source.discover()
        assert len(skills) == 1
        assert skills[0].location == str(path)
        assert await source.load(skills[0]) == path.read_text(encoding="utf-8")
        assert not source.diagnostics

    asyncio.run(drive())


def test_portable_relative_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _write_skill(tmp_path / "skills" / "guide")

    async def drive() -> None:
        source = DirectorySkillDiscoverer(["skills"])
        skill = (await source.discover())[0]
        assert skill.location == str(path)
        assert "ORIGINAL" in await source.load(skill)

    asyncio.run(drive())


@pytest.mark.parametrize("at_root", [False, True], ids=["child-link", "root-link"])
def test_portable_directory_rejects_symlink_or_windows_junction(tmp_path: Path, at_root: bool) -> None:
    outside = tmp_path / "outside"
    _write_skill(outside, "EXTERNAL")
    root = tmp_path / "root"
    if at_root:
        _directory_link(root, outside)
    else:
        root.mkdir()
        _directory_link(root / "junction", outside)

    async def drive() -> None:
        source = DirectorySkillDiscoverer([root])
        assert await source.discover() == ()
        if at_root:
            assert source.diagnostics

    asyncio.run(drive())


def test_portable_load_revalidates_ancestor_replaced_after_discovery(tmp_path: Path) -> None:
    root = tmp_path / "root"
    inside = root / "guide"
    _write_skill(inside)
    outside = tmp_path / "outside"
    _write_skill(outside, "EXTERNAL")  # Identical metadata would otherwise pass the load-time parser.

    async def drive() -> None:
        source = DirectorySkillDiscoverer([root])
        skill = (await source.discover())[0]
        inside.rename(tmp_path / "original")
        _directory_link(inside, outside)
        with pytest.raises(OSError):
            await source.load(skill)
        assert await source.discover() == ()

    asyncio.run(drive())


def test_portable_discovery_revalidates_parent_between_scan_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    inside = root / "guide"
    _write_skill(inside)
    outside = tmp_path / "outside"
    _write_skill(outside, "EXTERNAL")
    read = DirectorySkillDiscoverer._read

    def raced_read(path: Path) -> str:
        inside.rename(tmp_path / "original")
        _directory_link(inside, outside)
        return read(path)

    monkeypatch.setattr(DirectorySkillDiscoverer, "_read", staticmethod(raced_read))

    async def drive() -> None:
        source = DirectorySkillDiscoverer([root])
        assert await source.discover() == ()
        assert source.diagnostics

    asyncio.run(drive())


def test_portable_oversize_and_replaced_nonregular_leaf(tmp_path: Path) -> None:
    path = _write_skill(tmp_path / "guide")

    async def drive() -> None:
        source = DirectorySkillDiscoverer([tmp_path])
        skill = (await source.discover())[0]
        path.write_bytes(b"x" * 4_000_001)
        with pytest.raises(ValueError, match="exceeds"):
            await source.load(skill)
        assert await source.discover() == ()
        assert source.diagnostics
        _write_skill(path.parent)
        skill = (await source.discover())[0]
        path.unlink()
        path.mkdir()
        with pytest.raises((OSError, ValueError)):
            await source.load(skill)

    asyncio.run(drive())


class _FakeAPI:
    """Exercise handle ownership/failure unwinding on non-Windows test hosts too."""

    def __init__(self, *, reparse: str = "", info_error: str = "", body: bytes = b"body") -> None:
        self.reparse = reparse
        self.info_error = info_error
        self.body = body
        self.live: dict[int, tuple[str, bool]] = {}
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.reads: list[int] = []

    def open(self, path: str, *, directory: bool) -> int:
        handle = len(self.opened) + 1
        self.opened.append(handle)
        self.live[handle] = (path, directory)
        return handle

    def info(self, handle: int) -> _windows._FileInfo:
        path, directory = self.live[handle]
        if path == self.info_error:
            raise OSError("Injected handle inspection failure")
        attributes = 0x10 if directory else 0
        if path == self.reparse:
            attributes |= 0x400
        return _windows._FileInfo(attributes, 0 if directory else len(self.body))

    def read(self, handle: int, limit: int) -> bytes:
        assert len(self.live) == 4  # Volume, root, skill directory, and regular leaf all remain pinned.
        self.reads.append(handle)
        return self.body[:limit]

    def close(self, handle: int) -> None:
        assert handle in self.live
        del self.live[handle]
        self.closed.append(handle)


def test_windows_handle_ownership_and_bounded_read_on_all_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeAPI()
    monkeypatch.setattr(_windows, "_api", lambda: api)
    assert _windows.read_file(PureWindowsPath("C:/root/guide/SKILL.md"), 4) == b"body"
    assert api.reads == [4] and not api.live
    assert api.closed == list(reversed(api.opened))
    with pytest.raises(ValueError, match="exceeds"):
        _windows.read_file(PureWindowsPath("C:/root/guide/SKILL.md"), 3)
    assert api.reads == [4] and not api.live


@pytest.mark.parametrize("failure", ["read-error", "size-changed"])
def test_windows_read_failures_and_stale_size_cannot_escape_bound_or_leak_handles(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class ReadFailure(_FakeAPI):
        def read(self, handle: int, limit: int) -> bytes:
            raise OSError("Injected read failure")

    class StaleSize(_FakeAPI):
        def info(self, handle: int) -> _windows._FileInfo:
            return _windows._FileInfo(super().info(handle).attributes, 0)

    api = ReadFailure() if failure == "read-error" else StaleSize(body=b"oversized")
    monkeypatch.setattr(_windows, "_api", lambda: api)
    with pytest.raises((OSError, ValueError)):
        _windows.read_file(PureWindowsPath("C:/root/guide/SKILL.md"), 4)
    assert not api.live
    assert api.closed == list(reversed(api.opened))


@pytest.mark.parametrize("failure", ["reparse", "info-error"])
@pytest.mark.parametrize("component", ["root", "root/guide", "root/guide/SKILL.md"])
def test_windows_rejects_reparse_or_failed_inspection_and_closes_every_handle(
    monkeypatch: pytest.MonkeyPatch, failure: str, component: str
) -> None:
    target = _windows._native_path(PureWindowsPath("C:/") / component)
    api = _FakeAPI(reparse=target) if failure == "reparse" else _FakeAPI(info_error=target)
    monkeypatch.setattr(_windows, "_api", lambda: api)
    with pytest.raises(OSError):
        _windows.read_file(PureWindowsPath("C:/root/guide/SKILL.md"), 100)
    assert not api.reads and not api.live
    assert api.closed == list(reversed(api.opened))


@pytest.mark.parametrize(
    "path", ["relative/guide", "C:/root/../other", "C:/root/SKILL.md:stream", "//./C:/root", "//?/C:/root"]
)
def test_windows_rejects_device_stream_or_ambiguous_paths(path: str) -> None:
    with pytest.raises(ValueError, match="absolute drive/UNC"):
        _windows._native_path(PureWindowsPath(path))


def test_windows_extended_drive_and_unc_names_are_not_resolved_elsewhere() -> None:
    assert _windows._native_path(PureWindowsPath("C:/root/space 漢")) == "\\\\?\\C:\\root\\space 漢"
    assert _windows._native_path(PureWindowsPath("//server/share/root")) == "\\\\?\\UNC\\server\\share\\root"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows sharing/rename semantics")
def test_native_windows_directory_handles_block_parent_and_leaf_renames(tmp_path: Path) -> None:
    root = tmp_path / "root"
    leaf = root / "guide"
    path = _write_skill(leaf)
    with _windows.guarded_directory(leaf) as native_path:
        with pytest.raises(OSError):
            leaf.rename(root / "moved")
        with pytest.raises(OSError):
            root.rename(tmp_path / "moved-root")
        with os.scandir(native_path) as entries:
            assert [entry.name for entry in entries] == [path.name]
    leaf.rename(root / "moved")  # Every retained ancestor handle was released.


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows file sharing semantics")
def test_native_windows_read_holds_leaf_against_writes_and_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_skill(tmp_path / "guide")
    original = path.read_bytes()
    read = _windows._Kernel32.read
    calls: list[int] = []

    def guarded_read(api: _windows._Kernel32, handle: int, limit: int) -> bytes:
        with pytest.raises(OSError):
            path.write_text("REPLACED", encoding="utf-8")
        with pytest.raises(OSError):
            path.unlink()
        calls.append(handle)
        return read(api, handle, limit)

    monkeypatch.setattr(_windows._Kernel32, "read", guarded_read)
    assert _windows.read_file(path, 1000) == original
    assert len(calls) == 1
    path.write_text("Writable after read", encoding="utf-8")
