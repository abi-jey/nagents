"""Bounded coding tools and the final, post-plugin approval gate.

File tools reject all symlinks (including in-workspace symlinks), multi-link
files, special files, common credential paths, and traversal. POSIX directory
descriptors keep file operations anchored during path changes. This is not an
OS sandbox: approved shell commands and trusted Python have full user access.

Ignore discovery supports nested .gitignore files, !negation, /anchoring,
directory patterns and fnmatch globs; escaped patterns and Git's complete
wildmatch semantics are not supported. Always-pruned directories stay pruned.
Skill frontmatter supports single-line ``name:`` and ``description:`` scalars
(optionally quoted), not general YAML, multiline scalars, or script execution.
"""

import asyncio
import codecs
import copy
import difflib
import fnmatch
import hashlib
import inspect
import json
import math
import os
import signal
import stat
import time
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

from nagents.events import ToolResultEvent
from nagents.tools import ToolExecutor
from nagents.types import JsonSchema
from nagents.types import JsonSchemaProperty
from nagents.types import JsonValue
from nagents.types import ToolCall

from .types import ToolOutput

if TYPE_CHECKING:
    from .runtime import Harness

_PRUNED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_CREDENTIALS = {
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
    ".docker",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "credentials.toml",
    "secrets",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
    "kubeconfig",
    "service-account.json",
    "service_account.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "application_default_credentials.json",
    "serviceaccount.json",
    ".gcloud",
}


def _validate(value: JsonValue, schema: JsonSchema | JsonSchemaProperty, label: str = "arguments") -> None:
    kind = schema.get("type", "object")
    types: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "object": (dict,),
        "array": (list,),
        "null": (type(None),),
    }
    if kind not in types or type(value) not in types[kind]:
        raise ValueError(f"{label} must be {kind}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    property_schema = cast("JsonSchemaProperty", schema)
    if "enum" in property_schema and value not in property_schema["enum"]:
        raise ValueError(f"{label} must be one of {property_schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{label}: missing {', '.join(sorted(missing))}")
        if (label == "arguments" or properties) and value.keys() - properties.keys():
            raise ValueError(f"{label}: unexpected arguments {', '.join(sorted(value.keys() - properties.keys()))}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{label}.{key}")
    if isinstance(value, list) and "items" in property_schema:
        for item in value:
            _validate(item, property_schema["items"], label)


class HarnessExecutor(ToolExecutor):
    def __init__(self, harness: "Harness", tools: "CodingTools") -> None:
        super().__init__(harness.agent.tool_registry)
        self.harness = harness
        self.tools = tools

    async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
        started = time.monotonic()
        call = copy.deepcopy(tool_call)
        token = self.tools.call_id.set(call.id)
        try:
            tool = self._registry.get(call.name)
            builtin = tool is not None and tool.func == self.tools.builtins.get(call.name)
            if self.harness.config.demo and (
                not builtin or call.name not in {"list_files", "find", "read_file", "search", "skill", "demo_preview"}
            ):
                raise PermissionError("OFFLINE DEMO: writes, shell, and custom tools are disabled")
            if self.harness.mode == "reviewer" and (not builtin or call.name in {"edit", "write", "shell"}):
                raise PermissionError("reviewer profile denies edits, shell, and custom tools")
            if tool is not None:
                _validate(call.arguments, tool.parameters)
                if tool.func is not None:
                    inspect.signature(tool.func).bind(**call.arguments)
            if not builtin:
                await self.harness.approve(
                    call.name,
                    call.arguments,
                    "Custom tool: trusted code with full local access.",
                    json.dumps(call.arguments, indent=2, ensure_ascii=True),
                    call.id,
                )
                if self.harness.config.demo or self.harness.mode == "reviewer":
                    raise PermissionError("Active profile no longer allows custom tools")
            return await super().execute(call)
        except Exception as exc:
            return ToolResultEvent(
                id=call.id, name=call.name, error=str(exc), duration_ms=(time.monotonic() - started) * 1000
            )
        finally:
            self.tools.call_id.reset(token)


class CodingTools:
    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        self.root = harness.workspace
        self.read_hashes: dict[str, str] = {}
        self.call_id: ContextVar[str] = ContextVar("harness_tool_call_id", default="")
        self.builtins: dict[str, Callable[..., object]] = {}
        self.skills: dict[str, tuple[str, str]] = {}

    def register(self) -> None:
        for function in (
            self.read_file,
            self.list_files,
            self.find,
            self.search,
            self.edit,
            self.write,
            self.shell,
            self.skill,
        ):
            self.harness.agent.register_tool(function)
            self.builtins[function.__name__] = function
        if self.harness.config.demo:
            self.harness.agent.register_tool(self.demo_preview)
            self.builtins["demo_preview"] = self.demo_preview

    def relative(self, path: str) -> Path:
        if not path or "\x00" in path:
            raise ValueError("Path must not be empty or contain NUL")
        supplied = Path(path)
        if ".." in supplied.parts:
            raise PermissionError("Path traversal is not allowed")
        absolute = supplied if supplied.is_absolute() else self.root / supplied
        try:
            relative = absolute.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError("Path is outside the workspace") from exc
        for part in relative.parts:
            name = part.lower()
            if (
                name == ".git"
                or name in _CREDENTIALS
                or name.startswith((".env", "credentials.", "client_secret"))
                or name.endswith((".pem", ".key", ".p12", ".pfx", ".tfstate"))
            ):
                raise PermissionError(f"Protected path component: {part}")
        if absolute.is_relative_to(self.harness.config.data_dir):
            raise PermissionError("Harness session storage is not accessible to file tools")
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise PermissionError("Symlinks are not allowed by workspace file tools")
        if not absolute.resolve().is_relative_to(self.root):
            raise PermissionError("Resolved path is outside the workspace")
        return relative

    @contextmanager
    def directory(self, relative: Path) -> Iterator[int]:
        if os.name != "posix":
            raise OSError("Guarded workspace file tools currently require POSIX")
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def snapshot(self, path: str) -> tuple[bytes, int]:
        relative = self.relative(path)
        with self.directory(relative.parent) as parent:
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("Only regular, single-link files are allowed")
                data = source.read(self.harness.config.max_file_bytes + 1)
                if len(data) > self.harness.config.max_file_bytes:
                    raise ValueError(f"File exceeds {self.harness.config.max_file_bytes} byte limit")
                if b"\x00" in data:
                    raise ValueError("Binary files are not supported")
                data.decode("utf-8")
                return data, stat.S_IMODE(info.st_mode)

    def instructions(self, relative: Path, *, directory: bool = False) -> str:
        parent = relative if directory else relative.parent
        folders = [Path(), *reversed(parent.parents), parent]
        sections: list[str] = []
        updates: dict[str, str] = {}
        for folder in dict.fromkeys(folders):
            path = folder / "AGENTS.md"
            try:
                content = self.snapshot(str(path))[0].decode("utf-8")
            except FileNotFoundError:
                self.harness.instructions.pop(str(path), None)
                continue
            sections.append(f"## Workspace instructions: {path}\n{content}")
            updates[str(path)] = content
        if sum(len(section.encode("utf-8")) for section in sections) > self.harness.config.max_output:
            raise ValueError(
                "Applicable AGENTS.md instructions exceed max_output; shorten them or increase the trusted limit"
            )
        self.harness.instructions.update(updates)
        self.harness.refresh_instructions()
        return "\n\n".join(sections)

    async def read_file(self, path: str, start_line: int = 1, limit: int = 200) -> dict[str, JsonValue]:
        """Read UTF-8 text with line numbers and a SHA-256 snapshot required for editing. Lines are 1-based."""
        if not 1 <= limit <= 1000 or start_line < 1:
            raise ValueError("start_line must be >= 1 and limit must be 1..1000")
        relative = self.relative(path)
        instructions = self.instructions(relative)
        data, _ = self.snapshot(path)
        digest = hashlib.sha256(data).hexdigest()
        self.read_hashes[str(relative)] = digest
        lines = data.decode("utf-8").splitlines()
        text = "\n".join(
            f"{index}: {line}" for index, line in enumerate(lines[start_line - 1 : start_line - 1 + limit], start_line)
        )
        bounded = text.encode("utf-8")[: self.harness.config.max_output].decode("utf-8", errors="ignore")
        return {
            "path": str(relative),
            "sha256": digest,
            "content": bounded,
            "total_lines": len(lines),
            "truncated": len(bounded) < len(text) or start_line - 1 + limit < len(lines),
            "instructions": instructions,
        }

    def walk(self, path: str, *, recursive: bool = True) -> Iterator[tuple[str, bool]]:
        relative = self.relative(path)
        visited = 0

        def visit(folder: Path, inherited: list[tuple[Path, str]]) -> Iterator[tuple[str, bool]]:
            nonlocal visited
            rules = list(inherited)
            try:
                ignore = self.snapshot(str(folder / ".gitignore"))[0].decode("utf-8")
                rules.extend(
                    (folder, line.strip()) for line in ignore.splitlines() if line.strip() and not line.startswith("#")
                )
            except FileNotFoundError:
                pass
            with self.directory(folder) as fd, os.scandir(fd) as entries:
                names: list[tuple[str, bool]] = []
                for entry in entries:
                    visited += 1
                    if visited > 20000:
                        raise ValueError("Discovery scan limit reached (20000 entries); narrow the path")
                    if entry.name in _PRUNED or entry.is_symlink():
                        continue
                    names.append((entry.name, entry.is_dir(follow_symlinks=False)))
            for name, is_directory in sorted(names):
                child = folder / name
                try:
                    self.relative(str(child))
                except PermissionError:
                    continue
                ignored = False
                for origin, rule in rules:
                    negate = rule.startswith("!")
                    pattern = rule[1:] if negate else rule
                    directory_only = pattern.endswith("/")
                    pattern = pattern.rstrip("/")
                    target = child.relative_to(origin).as_posix()
                    anchored = pattern.startswith("/")
                    pattern = pattern.lstrip("/")
                    match = fnmatch.fnmatchcase(target if anchored or "/" in pattern else name, pattern)
                    if match and (not directory_only or is_directory):
                        ignored = not negate
                if ignored:
                    continue
                yield child.as_posix(), is_directory
                if is_directory and recursive:
                    yield from visit(child, rules)

        # Ancestor ignore rules apply even when searching a nested directory.
        ancestors: list[tuple[Path, str]] = []
        for parent in reversed(relative.parents):
            try:
                ignore = self.snapshot(str(parent / ".gitignore"))[0].decode("utf-8")
                ancestors.extend(
                    (parent, line.strip()) for line in ignore.splitlines() if line.strip() and not line.startswith("#")
                )
            except FileNotFoundError:
                pass
        yield from visit(relative, ancestors)

    async def list_files(self, path: str = ".", limit: int = 200) -> dict[str, JsonValue]:
        """List immediate workspace children, excluding dependencies, credentials and ignored files."""
        return await self._find("*", path, limit, recursive=False)

    async def find(self, pattern: str = "**/*", path: str = ".", limit: int = 200) -> dict[str, JsonValue]:
        """Find paths by glob, relative to path. Use **/*.py for recursive Python files; results are bounded."""
        return await self._find(pattern, path, limit, recursive=True)

    async def _find(self, pattern: str, path: str, limit: int, *, recursive: bool) -> dict[str, JsonValue]:
        if not 1 <= limit <= 1000 or not pattern or len(pattern) > 1000:
            raise ValueError("limit must be 1..1000 and pattern must be 1..1000 characters")
        relative = self.relative(path)
        instructions = self.instructions(relative, directory=True)
        found: list[str] = []
        truncated = False
        size = 0
        for item, is_directory in self.walk(path, recursive=recursive):
            await asyncio.sleep(0)
            local = Path(item).relative_to(relative)
            if local.match(pattern) or (pattern.startswith("**/") and local.match(pattern[3:])):
                size += len(item.encode("utf-8")) + 2
                if len(found) == limit or size > self.harness.config.max_output:
                    truncated = True
                    break
                found.append(item + ("/" if is_directory else ""))
        return {"paths": found, "truncated": truncated, "instructions": instructions}

    async def search(self, query: str, path: str = ".", limit: int = 100) -> dict[str, JsonValue]:
        """Case-sensitive literal text search, not regex. Skips binary/oversized files and ignored paths."""
        if not query or len(query) > 1000 or not 1 <= limit <= 1000:
            raise ValueError("query must be 1..1000 characters and limit must be 1..1000")
        self.instructions(self.relative(path), directory=True)
        matches: list[str] = []
        size = 0
        skipped = 0
        for item, is_directory in self.walk(path):
            await asyncio.sleep(0)
            if is_directory:
                continue
            try:
                text = self.snapshot(item)[0].decode("utf-8")
            except (OSError, ValueError):
                skipped += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query not in line:
                    continue
                match = f"{item}:{number}: {line[:1000]}"
                match_size = len(match.encode("utf-8")) + 1
                if len(matches) == limit or size + match_size > self.harness.config.max_output:
                    return {"matches": matches, "truncated": True, "skipped_files": skipped}
                self.instructions(Path(item))
                matches.append(match)
                size += match_size
        return {"matches": matches, "truncated": False, "skipped_files": skipped}

    def diff(self, path: str, old: str, new: str, *, creating: bool = False) -> str:
        parts = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="/dev/null" if creating else f"a/{path}",
            tofile=f"b/{path}",
        )
        text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in parts)
        if len(text.encode("utf-8")) > self.harness.config.max_output:
            raise ValueError("Diff exceeds approval preview limit; make a smaller edit")
        return text

    def commit(self, path: str, old: bytes | None, new: bytes, mode: int) -> None:
        relative = self.relative(path)
        with self.directory(relative.parent) as parent:
            temporary = f".ngn-{uuid.uuid4().hex}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(new)
                    os.fchmod(output.fileno(), mode)
                    output.flush()
                    os.fsync(output.fileno())
                # No awaits between this final snapshot check and the atomic commit.
                if old is not None:
                    if self.snapshot(path) != (old, mode):
                        raise ValueError("File changed during approval; read it again before editing")
                    os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    # link, unlike replace, cannot clobber a concurrently created file.
                    os.link(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)

    def writable(self) -> None:
        if self.harness.config.demo:
            raise PermissionError("OFFLINE DEMO: no workspace writes or shell execution")
        if self.harness.mode == "reviewer":
            raise PermissionError("reviewer profile is read-only; edits and shell are denied")

    async def edit(self, path: str, old: str, new: str) -> dict[str, JsonValue]:
        """Replace exactly one nonempty literal occurrence after read, conflict check, diff preview and approval."""
        self.writable()
        relative = self.relative(path)
        known = dict(self.harness.instructions)
        instructions = self.instructions(relative)
        if self.harness.instructions != known:
            raise ValueError(f"Applicable instructions changed; read them before retrying edit:\n{instructions}")
        data, mode = self.snapshot(path)
        if self.read_hashes.get(str(relative)) != hashlib.sha256(data).hexdigest():
            raise ValueError("File was not read or has changed since read; read it again before editing")
        text = data.decode("utf-8")
        if not old or text.count(old) != 1:
            raise ValueError("old must be nonempty and match exactly once; include more context")
        replacement = text.replace(old, new, 1)
        encoded = replacement.encode("utf-8")
        if len(encoded) > self.harness.config.max_file_bytes or b"\x00" in encoded:
            raise ValueError("Replacement is binary or exceeds file size limit")
        preview = self.diff(str(relative), text, replacement)
        if not preview:
            return {"path": str(relative), "diff": "", "changed": False}
        await self.harness.approve(
            "edit", {"path": path, "old": old, "new": new}, f"Edit {relative}", preview, self.call_id.get()
        )
        self.writable()
        known = dict(self.harness.instructions)
        self.instructions(relative)
        if self.harness.instructions != known:
            raise ValueError("Applicable instructions changed during approval; review them and retry")
        self.commit(path, data, encoded, mode)
        self.read_hashes[str(relative)] = hashlib.sha256(encoded).hexdigest()
        return {"path": str(relative), "diff": preview, "changed": True}

    async def write(self, path: str, content: str) -> dict[str, JsonValue]:
        """Create a new UTF-8 file in an existing directory, after diff approval. Never overwrite a file."""
        self.writable()
        relative = self.relative(path)
        known = dict(self.harness.instructions)
        instructions = self.instructions(relative)
        if self.harness.instructions != known:
            raise ValueError(f"Read newly discovered applicable instructions before retrying write:\n{instructions}")
        with self.directory(relative.parent) as parent:
            try:
                os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("File exists; use read and edit instead")
        data = content.encode("utf-8")
        if len(data) > self.harness.config.max_file_bytes or b"\x00" in data:
            raise ValueError("Content is binary or exceeds file size limit")
        preview = self.diff(str(relative), "", content, creating=True) or f"Create empty file: {relative}\n"
        await self.harness.approve(
            "write", {"path": path, "content": content}, f"Create {relative}", preview, self.call_id.get()
        )
        self.writable()
        known = dict(self.harness.instructions)
        self.instructions(relative)
        if self.harness.instructions != known:
            raise ValueError("Applicable instructions changed during approval; review them and retry")
        self.commit(path, None, data, 0o644)
        self.read_hashes[str(relative)] = hashlib.sha256(data).hexdigest()
        return {"path": str(relative), "diff": preview, "changed": True}

    async def shell(self, command: str, timeout: float = 0.0) -> dict[str, JsonValue]:
        """Run a local POSIX shell in the workspace after approval. NOT SANDBOXED. Output and time are bounded."""
        self.writable()
        if os.name != "posix":
            raise OSError("Shell process-group cleanup currently requires POSIX")
        if not command.strip() or len(command) > 16384 or "\x00" in command:
            raise ValueError("command must be nonempty, without NUL, and <= 16384 characters")
        seconds = timeout or self.harness.config.shell_timeout
        if not math.isfinite(seconds) or not 0 < seconds <= self.harness.config.shell_timeout:
            raise ValueError(f"timeout must be > 0 and <= {self.harness.config.shell_timeout}")
        await self.harness.approve(
            "shell",
            {"command": command, "timeout": seconds},
            "LOCAL SHELL IS NOT SANDBOXED: may access the network, credentials, and files outside the workspace.",
            f"cwd: {self.root}\ntimeout: {seconds}s\n\n{command}",
            self.call_id.get(),
        )
        self.writable()
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        )
        process: asyncio.subprocess.Process | None = None
        output: list[str] = []
        remaining = self.harness.config.max_output
        truncated = False
        timed_out = False
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            process = await asyncio.shield(spawn)
            assert process.stdout is not None
            async with asyncio.timeout(seconds):
                while chunk := await process.stdout.read(4096):
                    accepted = chunk[:remaining]
                    remaining -= len(accepted)
                    text = decoder.decode(accepted)
                    if text:
                        output.append(text)
                        await self.harness.emit(ToolOutput(self.call_id.get(), "shell", text))
                    if len(accepted) < len(chunk) and not truncated:
                        truncated = True
                        await self.harness.emit(ToolOutput(self.call_id.get(), "shell", "\n[output truncated]\n"))
                await process.wait()
        except TimeoutError:
            timed_out = True
        finally:

            async def cleanup() -> None:
                nonlocal process
                if process is None:
                    process = await spawn
                # Kill before emitting timeout notices, which can backpressure.
                # Drain paused pipes so asyncio can reap a noisy cancelled child.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                if process.stdout is not None:
                    while await process.stdout.read(4096):
                        pass
                await process.wait()

            cleanup_task = asyncio.create_task(cleanup())
            cancelled = False
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup_task.result()
            if cancelled:
                raise asyncio.CancelledError
        assert process is not None
        if timed_out:
            await self.harness.emit(ToolOutput(self.call_id.get(), "shell", f"\n[timed out after {seconds}s]\n"))
        ending = decoder.decode(b"", final=True)
        if ending:
            output.append(ending)
            await self.harness.emit(ToolOutput(self.call_id.get(), "shell", ending))
        return {
            "output": "".join(output),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "truncated": truncated,
        }

    def discover_skills(self) -> None:
        for root in (".ngn/skills", ".agents/skills"):
            if not (self.root / root).exists():
                continue
            for path, is_directory in self.walk(root):
                if is_directory or Path(path).name != "SKILL.md":
                    continue
                text = self.snapshot(path)[0].decode("utf-8")
                name = Path(path).parent.name
                description = "No description; load with skill(name)."
                if text.startswith("---\n"):
                    header, separator, _ = text[4:].partition("\n---\n")
                    if not separator:
                        raise ValueError(f"Unterminated skill frontmatter: {path}")
                    for line in header.splitlines():
                        key, colon, value = line.partition(":")
                        if not colon or key not in {"name", "description"}:
                            raise ValueError(
                                f"{path}: supported skill frontmatter is single-line name/description only"
                            )
                        value = value.strip()
                        if not value or value in {"|", ">", "|-", ">-"}:
                            raise ValueError(f"{path}: multiline/empty skill frontmatter is unsupported")
                        if value[0] in {"'", '"'}:
                            if len(value) < 2 or value[-1] != value[0]:
                                raise ValueError(f"{path}: unclosed frontmatter quote")
                            value = value[1:-1]
                        if key == "name":
                            name = value
                        else:
                            description = value
                if name in self.skills:
                    raise ValueError(f"Duplicate skill name {name!r} at {path}")
                if len(name) > 80 or not name or any(not (char.isalnum() or char in "-_") for char in name):
                    raise ValueError(f"Invalid skill name in {path}")
                self.skills[name] = (path, description[:500])

    async def skill(self, name: str) -> dict[str, JsonValue]:
        """Load a discovered SKILL.md as text only. Never execute its scripts."""
        if name not in self.skills:
            raise ValueError(f"Unknown skill {name!r}; available: {', '.join(self.skills)}")
        path, _ = self.skills[name]
        return await self.read_file(path, limit=1000)

    async def demo_preview(self) -> dict[str, JsonValue]:
        """OFFLINE DEMO: request approval for a sample diff; never change any file."""
        if not self.harness.config.demo:
            raise PermissionError("Preview tool is available only in offline demo")
        preview = "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-print('hello')\n+print('hello, world')\n"
        try:
            await self.harness.approve(
                "demo_preview",
                {},
                "OFFLINE DEMO: sample diff, preview only. No files will be changed.",
                preview,
                self.call_id.get(),
            )
            accepted = True
        except PermissionError:
            accepted = False
        return {
            "approved": accepted,
            "preview_only": True,
            "diff": preview,
            "message": "OFFLINE DEMO: approval recorded for preview only; no file was written.",
        }
