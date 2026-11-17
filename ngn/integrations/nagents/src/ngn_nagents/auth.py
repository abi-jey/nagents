"""Private device-auth subprocess mode. Only allowlisted, non-secret frames leave Python.

No Codex discovery, environment-key fallback, browser launch, or harness imports.
The mutation flock lives in this process, so it survives death of the Rust parent
until the actual identity writer exits. Host instances hold the corresponding
shared flock. Locks are cooperative and require a local POSIX filesystem.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import signal
import stat
import sys
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol
from typing import TypeVar
from typing import cast

from .protocol import ProtocolError

if TYPE_CHECKING:
    from nagents.provider.openai import CodexCredentials

AUTH_VERSION = 1
MAX_AUTH_FRAME = 2048
VERIFICATION_URL = "https://auth.openai.com/codex/device"
MISSING_CORE = (
    "ChatGPT requires the public nagents.provider.device_auth API, absent from published nagents 0.28.5. "
    "Install a source-built core containing that API and this adapter. "
    "Official subscription release is blocked until a new core version is published."
)


class Authorization(Protocol):
    @property
    def verification_url(self) -> str: ...

    @property
    def user_code(self) -> str: ...


class Auth(Protocol):
    async def start_device_login(self) -> Authorization: ...

    async def complete_device_login(self, authorization: Authorization) -> None: ...

    async def credentials(self) -> CodexCredentials: ...

    def status(self) -> str: ...

    async def logout_async(self) -> None: ...

    async def close(self) -> None: ...


class AuthFailure(Exception):
    """An allowlisted machine code, never an upstream exception string."""

    def __init__(self, code: str) -> None:
        self.code = code


def auth_factory() -> Callable[[Path], Auth]:
    """Lazy capability probe: API-key runtimes also work with the published core."""
    try:
        module = importlib.import_module("nagents.provider.device_auth")
        factory = getattr(module, "OpenAIAuth", None)
        if (
            not isinstance(factory, type)
            or not isinstance(getattr(module, "DeviceAuthorization", None), type)
            or not isinstance(getattr(module, "OpenAIAuthError", None), type)
            or any(
                not callable(getattr(factory, name, None))
                for name in (
                    "credentials",
                    "start_device_login",
                    "complete_device_login",
                    "status",
                    "logout_async",
                    "close",
                )
            )
        ):
            raise ImportError
    except Exception:
        raise ProtocolError(MISSING_CORE) from None
    return cast("Callable[[Path], Auth]", factory)


def available() -> bool:
    try:
        auth_factory()
    except ProtocolError:
        return False
    return True


def store_path(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or len(value.encode()) > 4096
        or "\x00" in value
        or not path.is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or path.name != "openai.json"
        or path.parent.name != "auth"
    ):
        raise ProtocolError("ChatGPT auth_store must be an absolute host-owned auth/openai.json path")
    return path


def challenge(authorization: Authorization) -> dict[str, object]:
    if (
        authorization.verification_url != VERIFICATION_URL
        or re.fullmatch(r"[A-Za-z0-9-]{1,64}", authorization.user_code) is None
    ):
        raise AuthFailure("auth_failed")
    # Never serialize the authorization object (it also holds a device_auth_id).
    return {"type": "challenge", "verification_url": VERIFICATION_URL, "user_code": authorization.user_code}


def output(message: dict[str, object]) -> None:
    # Defense in depth: even internal callers cannot emit arbitrary fields/bodies.
    kind = message.get("type")
    if kind == "challenge":
        valid = (
            set(message) == {"type", "verification_url", "user_code"}
            and message["verification_url"] == VERIFICATION_URL
            and isinstance(message["user_code"], str)
            and re.fullmatch(r"[A-Za-z0-9-]{1,64}", message["user_code"]) is not None
        )
    elif kind == "status":
        valid = (
            set(message) == {"type", "status"}
            and isinstance(message["status"], str)
            and message["status"] in {"signed_in", "signed_out", "unusable"}
        )
    elif kind == "error":
        valid = (
            set(message) == {"type", "code"}
            and isinstance(message["code"], str)
            and message["code"]
            in {
                "invalid_request",
                "missing_core",
                "auth_failed",
                "storage",
                "busy",
                "cancelled",
            }
        )
    else:
        valid = False
    if not valid:
        raise ProtocolError("Invalid private authentication response (contents redacted)")
    encoded = json.dumps({"auth_protocol": AUTH_VERSION, "message": message}, ensure_ascii=True, allow_nan=False)
    if len(encoded.encode()) + 1 > MAX_AUTH_FRAME:
        raise ProtocolError("Oversized private authentication response")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


@contextmanager
def identity_guard(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Same stable metadata lock as Rust Host; no tokens or process IDs in it."""
    if os.name != "posix":
        raise AuthFailure("storage")
    import fcntl

    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    descriptor = -1
    try:
        for part in (*path.parent.parent.parts[1:], ".identity"):
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            trusted_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.getuid()} or (stat.S_IMODE(info.st_mode) & 0o022 and not trusted_sticky):
                raise AuthFailure("storage")
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AuthFailure("storage")
        descriptor = os.open(
            "host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
        info = os.fstat(descriptor)
        named = os.stat("host.lock", dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != 0
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise AuthFailure("storage")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthFailure("busy") from None
        named = os.stat("host.lock", dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise AuthFailure("storage")
        yield
    except OSError:
        raise AuthFailure("storage") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


@contextmanager
def mutation_guard(path: Path) -> Iterator[None]:
    with identity_guard(path, exclusive=True):
        yield


T = TypeVar("T")


async def joined_cleanup(operation: Awaitable[T]) -> T:
    """Own and join cleanup even if the caller receives repeated cancellation."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def operate(operation: str, path: Path, emit: Callable[[dict[str, object]], None]) -> None:
    try:
        factory = auth_factory()
    except ProtocolError:
        raise AuthFailure("missing_core") from None
    auth = factory(path)  # Always explicit; never OpenAIAuth()'s legacy default.
    try:
        if operation == "status":
            # The public status method only inspects protected local storage.
            status = {
                "ChatGPT device login saved": "signed_in",
                "Not signed in": "signed_out",
            }.get(auth.status(), "unusable")
            emit({"type": "status", "status": status})
        elif operation == "logout":
            await auth.logout_async()
            emit({"type": "status", "status": "signed_out"})
        elif operation == "login":
            authorization = await auth.start_device_login()
            emit(challenge(authorization))
            await auth.complete_device_login(authorization)
            emit({"type": "status", "status": "signed_in"})
        else:
            raise AuthFailure("invalid_request")
    finally:
        await joined_cleanup(auth.close())


async def serve(arguments: list[str]) -> int:
    try:
        if len(arguments) != 3 or arguments[0] not in {"login", "status", "logout"} or arguments[1] != "--store":
            raise AuthFailure("invalid_request")
        try:
            path = store_path(arguments[2])
        except (ProtocolError, UnicodeError):
            raise AuthFailure("invalid_request") from None
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        signals: list[signal.Signals] = []
        try:
            if os.name == "posix" and task is not None:
                for name in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(name, task.cancel)
                    signals.append(name)
            if arguments[0] == "status":
                await operate("status", path, output)
            else:
                with mutation_guard(path):
                    await operate(arguments[0], path, output)
        finally:
            for name in signals:
                loop.remove_signal_handler(name)
        return 0
    except AuthFailure as error:
        output({"type": "error", "code": error.code})
    except asyncio.CancelledError:
        output({"type": "error", "code": "cancelled"})
    except (BrokenPipeError, ConnectionError):
        return 2
    except Exception:
        output({"type": "error", "code": "auth_failed"})
    return 2
