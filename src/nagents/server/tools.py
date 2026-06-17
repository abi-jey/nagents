"""Built-in tools for the agent server."""

import hashlib
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

from nagents import docker_run


# ── Attachment store (in-memory registry, files on disk) ──────────────────────

@dataclass
class Attachment:
    id: str
    path: Path
    description: str
    content_type: str
    created_at: float
    fetch_count: int = 0
    last_fetched_at: float | None = None


_attachments: dict[str, Attachment] = {}
_pending_attachments: list[str] = []


def _reset_pending() -> None:
    _pending_attachments.clear()


def _collect_pending() -> list[str]:
    return list(_pending_attachments)


def _make_attachment_id(path: Path) -> str:
    return hashlib.sha256(f"{path.resolve()}{time.time()}".encode()).hexdigest()[:12]


# ── Tool functions ────────────────────────────────────────────────────────────


def run_shell_command(command: str) -> str:
    """Run a shell command inside an isolated Docker sandbox and return output."""
    return docker_run(command)


def read_file(path: str) -> str:
    """Read a file."""
    return Path(path).read_text()


def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Written to {path}"


def list_directory(path: str = ".") -> str:
    """List directory contents."""
    return "\n".join(str(p) for p in Path(path).iterdir())


def attach_file(file_path: str, description: str = "") -> str:
    """Attach an existing file so it can be served via /attachments/{id}."""
    p = Path(file_path)
    if not p.exists():
        return f"Error: File not found: {file_path}"
    if not p.is_file():
        return f"Error: Not a regular file: {file_path}"

    aid = _make_attachment_id(p)
    mime, _ = mimetypes.guess_type(p.name)

    _attachments[aid] = Attachment(
        id=aid,
        path=p.resolve(),
        description=description or p.name,
        content_type=mime or "application/octet-stream",
        created_at=time.time(),
    )
    _pending_attachments.append(aid)

    size = p.stat().st_size
    return (
        f"File attached: /attachments/{aid}\n"
        f"Name: {p.name}\n"
        f"Size: {size} bytes"
    )


BASE_TOOLS = [
    run_shell_command,
    read_file,
    write_file,
    list_directory,
    attach_file,
]
