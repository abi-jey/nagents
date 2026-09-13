"""Windows handle-based, no-reparse-point access for directory skills.

The POSIX backend uses openat/O_NOFOLLOW. Windows instead retains a handle to
every ancestor, denying write/delete sharing until traversal or reading ends.
That prevents a checked directory being renamed or turned into a junction while
a child is opened by its absolute path. All components, including the leaf, are
opened with OPEN_REPARSE_POINT and inspected through their retained handles.
"""

import ctypes
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from pathlib import PurePath
from pathlib import PureWindowsPath
from typing import NoReturn
from typing import Protocol

_DIRECTORY = 0x10
_REPARSE_POINT = 0x400
_FILE_TYPE_DISK = 1
_FILE_LIST_DIRECTORY = 1
_FILE_READ_ATTRIBUTES = 0x80
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 1
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _HandleInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("access_time", wintypes.FILETIME),
        ("write_time", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class _FileInfo:
    attributes: int
    size: int


class _FileAPI(Protocol):
    def open(self, path: str, *, directory: bool) -> int: ...
    def info(self, handle: int) -> _FileInfo: ...
    def read(self, handle: int, limit: int) -> bytes: ...
    def close(self, handle: int) -> None: ...


def _raise_last_error() -> NoReturn:
    if sys.platform == "win32":
        raise ctypes.WinError(ctypes.get_last_error())
    raise OSError("Windows handle access is available only on Windows")


class _Kernel32:
    """Small typed boundary around the standard-library ctypes Win32 calls."""

    _kernel: ctypes.CDLL

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows handle access is available only on Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_HandleInformation)]
        kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        kernel.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

    def open(self, path: str, *, directory: bool) -> int:
        handle = self._kernel.CreateFileW(
            path,
            # Attribute-only handles do not participate in Windows share-access
            # checks. LIST_DIRECTORY (the READ_DATA bit) is essential to pinning.
            (_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES) if directory else _GENERIC_READ,
            _FILE_SHARE_READ,  # Neither writable handles nor rename/delete may race our inspection.
            None,
            _OPEN_EXISTING,
            _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            _raise_last_error()
        return int(handle)

    def info(self, handle: int) -> _FileInfo:
        if self._kernel.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OSError("Skill paths must be ordinary disk files/directories")
        info = _HandleInformation()
        if not self._kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            _raise_last_error()
        return _FileInfo(int(info.attributes), (int(info.size_high) << 32) | int(info.size_low))

    def read(self, handle: int, limit: int) -> bytes:
        result = bytearray()
        buffer = ctypes.create_string_buffer(min(limit, 65536))
        while len(result) < limit:
            count = wintypes.DWORD()
            if not self._kernel.ReadFile(
                handle, buffer, min(len(buffer), limit - len(result)), ctypes.byref(count), None
            ):
                _raise_last_error()
            if not count.value:
                break
            result.extend(buffer.raw[: count.value])
        return bytes(result)

    def close(self, handle: int) -> None:
        if not self._kernel.CloseHandle(handle):
            _raise_last_error()


@cache
def _api() -> _FileAPI:
    return _Kernel32()


def _native_path(path: PureWindowsPath) -> str:
    text = str(path)
    # Extended paths are generated here, not accepted as arbitrary device access.
    # Reject streams and parent components rather than allow Win32 normalization
    # to redirect an apparently contained path to a different object.
    if (
        not path.is_absolute()
        or text.startswith(("\\\\?\\", "\\\\.\\"))
        or any(part in {".", ".."} or ":" in part for part in path.parts[1:])
    ):
        raise ValueError("Skill roots must be absolute drive/UNC paths without device namespaces or streams")
    return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text


@contextmanager
def _handle(path: PureWindowsPath, api: _FileAPI, *, directory: bool) -> Iterator[tuple[int, _FileInfo]]:
    handle = api.open(_native_path(path), directory=directory)
    try:
        info = api.info(handle)
        if info.attributes & _REPARSE_POINT:
            raise OSError(f"Skill paths must not contain reparse points (symlinks/junctions): {path}")
        if bool(info.attributes & _DIRECTORY) != directory:
            raise OSError(f"Skill path must be a {'directory' if directory else 'regular file'}: {path}")
        yield handle, info
    finally:
        api.close(handle)


@contextmanager
def _directory(path: PureWindowsPath, api: _FileAPI) -> Iterator[str]:
    native_path = _native_path(path)
    with ExitStack() as stack:
        for parent in (*reversed(path.parents), path):
            stack.enter_context(_handle(parent, api, directory=True))
        yield native_path


@contextmanager
def guarded_directory(path: PurePath) -> Iterator[str]:
    """Keep all ancestors pinned while a Windows directory is enumerated."""
    with _directory(PureWindowsPath(path), _api()) as native_path:
        yield native_path


def read_file(path: PurePath, max_bytes: int) -> bytes:
    """Read a bounded regular file from a retained, no-reparse-point handle."""
    windows_path = PureWindowsPath(path)
    api = _api()
    with _directory(windows_path.parent, api), _handle(windows_path, api, directory=False) as (handle, info):
        if info.size > max_bytes:
            raise ValueError(f"SKILL.md exceeds {max_bytes} bytes")
        content = api.read(handle, max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError(f"SKILL.md exceeds {max_bytes} bytes")
        return content
