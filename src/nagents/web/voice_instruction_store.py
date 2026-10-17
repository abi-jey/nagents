"""Bounded Markdown voice instructions with revisioned, atomic writes."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Literal

from nagents.harness.providers import _exclusive_lock

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nagents.harness.providers import ScopedProviderRegistryStore

InstructionScope = Literal["global", "workspace", "agent"]
InstructionMode = Literal["inherit", "custom"]
MAX_INSTRUCTION_BYTES = 24000
MAX_INSTRUCTION_CHARACTERS = 12000
_MAX_FILE_BYTES = MAX_INSTRUCTION_BYTES + 64
EMPTY_REVISION = "0" * 64


def validate_instruction_text(text: str) -> str:
    try:
        valid = len(text) <= MAX_INSTRUCTION_CHARACTERS and len(text.encode("utf-8")) <= MAX_INSTRUCTION_BYTES
    except UnicodeError:
        valid = False
    if not valid or "\x00" in text:
        raise ValueError("Voice instructions require valid text within 12,000 characters and 24,000 UTF-8 bytes.")
    return text


@dataclass(frozen=True)
class InstructionPreference:
    path: Path
    mode: InstructionMode = "inherit"
    text: str = ""
    revision: str = EMPTY_REVISION
    present: bool = False


class VoiceInstructionStore:
    """The body is exact Markdown; explicit inheritance suppresses older preferences."""

    def __init__(self, providers: ScopedProviderRegistryStore) -> None:
        # Provider registries may be mounted external files, even two files in
        # the same directory. Writable preferences use the normal scope paths.
        self.global_directory = Path(os.path.abspath(providers.model_store("global").path.parent))
        self.workspace_directory = Path(os.path.abspath(providers.model_store("workspace").path.parent))

    def path(self, scope: InstructionScope, agent: str = "") -> Path:
        if scope == "global":
            return self.global_directory / "live-instructions.md"
        if scope == "workspace":
            return self.workspace_directory / "live-instructions.md"
        if scope != "agent" or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent):
            raise ValueError("Choose a valid agent for Voice instructions.")
        return self.workspace_directory / "agent-live-instructions" / f"{agent}.md"

    @staticmethod
    def _check_parents(path: Path) -> None:
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Voice instruction files must not use symlink directories.")

    def load(self, scope: InstructionScope, agent: str = "") -> InstructionPreference:
        path = self.path(scope, agent)
        self._check_parents(path)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError("Voice instructions must be a bounded regular Markdown file.")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return InstructionPreference(path)
        except OSError:
            raise ValueError("Voice instructions could not be read.") from None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError("Voice instructions must be a bounded regular Markdown file.")
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("Voice instruction file exceeds its storage limit.")
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            raise ValueError("Voice instructions must contain UTF-8 Markdown.") from None
        header = re.match(r"\A---\r?\nmode: (inherit|custom)\r?\n---\r?\n", content)
        if header is None:
            raise ValueError("Voice instruction frontmatter must contain only mode: inherit or mode: custom.")
        mode: InstructionMode = "custom" if header.group(1) == "custom" else "inherit"
        text = validate_instruction_text(content[header.end() :])
        if mode == "inherit" and text:
            raise ValueError("Inherited Voice instructions must not contain a custom body.")
        return InstructionPreference(path, mode, text, hashlib.sha256(raw).hexdigest(), True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """One lock serializes all scopes so ancestor revisions remain stable during writes."""
        lock = self.global_directory / ".live-instructions.lock"
        self._check_parents(lock)
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_parents(lock)
        if lock.is_symlink() or (lock.exists() and not lock.is_file()):
            raise ValueError("Voice instruction lock must be a regular file.")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise ValueError("Voice instruction lock is unavailable.") from None
        with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
            yield

    def save_locked(
        self, scope: InstructionScope, agent: str, mode: InstructionMode, text: str, *, expected: str
    ) -> InstructionPreference:
        """Caller owns locked() and validates the complete parent/configuration revision."""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid Voice instruction revision.")
        validate_instruction_text(text)
        if mode not in {"inherit", "custom"} or (mode == "inherit" and text):
            raise ValueError("Inherited Voice instructions must not contain a custom body.")
        current = self.load(scope, agent)
        if current.revision != expected:
            raise ValueError("Voice instructions changed. Refresh before saving.")
        path = current.path
        self._check_parents(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_parents(path)
        payload = f"---\nmode: {mode}\n---\n{text}".encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".live-instructions-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                if os.name != "nt":
                    os.fchmod(output.fileno(), 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.load(scope, agent)
