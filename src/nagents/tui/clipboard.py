"""Optional local clipboard bridge; terminal OSC 52 remains the remote path."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from contextlib import suppress
from pathlib import Path


async def copy_native(text: str, workspace: Path) -> bool:
    """Write via stdin, never a shell/argument/log, with bounded owned-process cleanup.

    Never target a remote host's desktop clipboard through an SSH session, or
    execute a clipboard helper supplied by the workspace being edited.
    """
    if any(os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return False
    commands: list[tuple[str, ...]] = []
    environment = None
    if sys.platform == "darwin":
        commands = [("/usr/bin/pbcopy",)]
        environment = {**os.environ, "LC_CTYPE": "UTF-8"}
        environment.pop("LC_ALL", None)
    elif sys.platform == "win32":
        commands = [
            (
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new(); "
                "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
            )
        ]
    else:
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(("wl-copy", "--type", "text/plain;charset=utf-8"))
        if os.environ.get("DISPLAY"):
            commands.extend(
                [
                    ("xclip", "-selection", "clipboard", "-in", "-target", "UTF8_STRING"),
                    ("xsel", "--clipboard", "--input"),
                ]
            )
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        try:
            resolved = Path(executable).resolve()
            if any(parent.samefile(workspace) for parent in resolved.parents):
                continue
        except OSError:
            continue
        process: asyncio.subprocess.Process | None = None
        starting = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(resolved),
                *command[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
        )
        try:
            process = await asyncio.shield(starting)
            async with asyncio.timeout(1):
                await process.communicate(text.encode("utf-8"))
            if process.returncode == 0:
                return True
        except asyncio.CancelledError:
            with suppress(OSError):
                process = await starting
            raise
        except (OSError, TimeoutError):
            pass
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
    return False
