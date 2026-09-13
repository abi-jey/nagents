"""Bounded filesystem and package-resource skill sources, plus source composition."""

import asyncio
import os
import stat
import sys
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from .types import Skill
from .types import SkillDiscoverer
from .types import parse_skill_metadata
from .types import skill_catalog

_MAX_FILE_BYTES = 4_000_000
_MAX_DIAGNOSTICS = 20
# Use the already-imported package identity rather than resolving its name via
# import hooks again. Resource readers still enforce package-resource access,
# including when the package is loaded from a wheel/zip rather than a directory.
_PACKAGE = sys.modules[__package__]


class DirectorySkillDiscoverer:
    """Discover SKILL.md under explicitly selected roots, without following symlinks.

    Traversal is bounded by max_entries (including directories) and max_depth.
    Files over 4 MB are skipped with a bounded diagnostic. This source grants
    access to its configured roots; applications with their own filesystem
    authorization should implement SkillDiscoverer using that guarded access.
    Windows rejects all reparse points (including junctions) and pins ancestors
    with read-only sharing; listing/attribute access to ancestors is required.
    Conflicting write/delete handles cause a diagnostic skip or a load error.
    """

    def __init__(self, roots: Iterable[Path | str], *, max_entries: int = 10_000, max_depth: int = 16) -> None:
        if max_entries < 1 or max_depth < 0:
            raise ValueError("max_entries must be positive and max_depth nonnegative")
        self.roots = tuple(Path(os.path.abspath(Path(root).expanduser())) for root in roots)
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.diagnostics: tuple[str, ...] = ()
        self._catalog: dict[str, Skill] = {}

    @staticmethod
    def _open_directory(path: Path) -> int:
        if sys.platform == "win32":
            raise OSError("Windows directory access requires guarded native handles, not POSIX descriptors")
        # Walk from the filesystem anchor using directory descriptors. Checking
        # is_symlink()/resolve() alone would leave a check/open symlink race.
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def _read(cls, path: Path) -> str:
        if sys.platform == "win32":
            from ._windows import read_file

            return read_file(path, _MAX_FILE_BYTES).decode("utf-8")
        parent = cls._open_directory(path.parent)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("SKILL.md must be a regular file")
                content = stream.read(_MAX_FILE_BYTES + 1)
        finally:
            os.close(parent)
        if len(content) > _MAX_FILE_BYTES:
            raise ValueError(f"SKILL.md exceeds {_MAX_FILE_BYTES} bytes")
        return content.decode("utf-8")

    @classmethod
    @contextmanager
    def _scandir(cls, path: Path) -> Iterator[Iterator[os.DirEntry[str]]]:
        if sys.platform == "win32":
            from ._windows import guarded_directory

            with guarded_directory(path) as native_path, os.scandir(native_path) as entries:
                yield entries
        else:
            descriptor = cls._open_directory(path)
            try:
                with os.scandir(descriptor) as entries:
                    yield entries
            finally:
                os.close(descriptor)

    async def discover(self) -> Sequence[Skill]:
        found: list[Skill] = []
        diagnostics: list[str] = []
        visited: set[Path] = set()
        remaining = self.max_entries

        def diagnose(path: Path, error: object) -> None:
            if len(diagnostics) < _MAX_DIAGNOSTICS:
                diagnostics.append(f"{str(path)[:300]}: {str(error)[:300]}")

        async def walk(directory: Path, depth: int) -> None:
            nonlocal remaining
            await asyncio.sleep(0)
            if directory in visited:
                return
            visited.add(directory)
            try:
                with self._scandir(directory) as entries:
                    modes: dict[str, int] = {}
                    for entry in entries:
                        if len(modes) >= remaining:
                            diagnose(directory, "discovery entry limit reached; increase max_entries")
                            # Never publish an arbitrary filesystem-order subset.
                            modes.clear()
                            remaining = 0
                            break
                        modes[entry.name] = 0
                        try:
                            # Inspect entries while their parent is still pinned.
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISLNK(info.st_mode) or (
                                sys.platform == "win32" and info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                            ):
                                continue
                            modes[entry.name] = info.st_mode
                        except OSError as error:
                            diagnose(directory / entry.name, error)
            except (OSError, ValueError) as error:
                diagnose(directory, error)
                return
            remaining -= len(modes)
            for name, mode in sorted(modes.items()):
                await asyncio.sleep(0)
                path = directory / name
                try:
                    if stat.S_ISDIR(mode):
                        if depth < self.max_depth and remaining:
                            await walk(path, depth + 1)
                        else:
                            diagnose(path, "discovery depth/entry limit reached")
                    elif name == "SKILL.md" and stat.S_ISREG(mode):
                        found.append(parse_skill_metadata(self._read(path), str(path), path.parent.name))
                except (OSError, ValueError) as error:
                    diagnose(path, error)

        for root in self.roots:
            if remaining:
                await walk(root, 0)
        catalog = skill_catalog(found)
        self._catalog = catalog
        self.diagnostics = tuple(diagnostics)
        return tuple(catalog.values())

    async def load(self, skill: Skill) -> str:
        await asyncio.sleep(0)
        if self._catalog.get(skill.name) != skill:
            raise ValueError(f"Skill {skill.name!r} is not in this directory catalog; refresh discovery")
        path = Path(skill.location)
        if not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError("Skill location is outside the configured roots")
        content = self._read(path)
        if parse_skill_metadata(content, skill.location, path.parent.name) != skill:
            raise ValueError(f"Skill {skill.name!r} changed; refresh discovery and retry")
        return content


class BuiltinSkillDiscoverer:
    """Load bundled skills via importlib.resources, including from zipped wheels."""

    def __init__(self) -> None:
        self._catalog: dict[str, Skill] = {}
        self._paths: dict[str, str] = {}

    async def discover(self) -> Sequence[Skill]:
        root = resources.files(_PACKAGE).joinpath("bundled")
        found: list[Skill] = []
        paths: dict[str, str] = {}
        if root.is_dir():
            for directory in sorted(root.iterdir(), key=lambda entry: entry.name):
                await asyncio.sleep(0)
                path = directory.joinpath("SKILL.md")
                if directory.is_dir() and path.is_file():
                    location = f"package:nagents.skills/bundled/{directory.name}/SKILL.md"
                    skill = parse_skill_metadata(path.read_text(encoding="utf-8"), location, directory.name)
                    found.append(skill)
                    paths[skill.name] = directory.name
        catalog = skill_catalog(found)
        self._catalog, self._paths = catalog, paths
        return tuple(catalog.values())

    async def load(self, skill: Skill) -> str:
        await asyncio.sleep(0)
        if self._catalog.get(skill.name) != skill:
            raise ValueError(f"Skill {skill.name!r} is not in the bundled catalog; refresh discovery")
        return (
            resources.files(_PACKAGE)
            .joinpath("bundled", self._paths[skill.name], "SKILL.md")
            .read_text(encoding="utf-8")
        )


class CompositeSkillDiscoverer:
    """Combine sources in order; first-source precedence, never stale accumulation."""

    def __init__(self, sources: Iterable[SkillDiscoverer]) -> None:
        self.sources = tuple(sources)
        self._owners: dict[str, tuple[Skill, SkillDiscoverer]] = {}

    async def discover(self) -> Sequence[Skill]:
        owners: dict[str, tuple[Skill, SkillDiscoverer]] = {}
        for source in self.sources:
            for name, skill in skill_catalog(await source.discover()).items():
                owners.setdefault(name, (skill, source))
        self._owners = dict(sorted(owners.items()))
        return tuple(skill for skill, _ in self._owners.values())

    async def load(self, skill: Skill) -> str:
        owner = self._owners.get(skill.name)
        if owner is None or owner[0] != skill:
            raise ValueError(f"Skill {skill.name!r} is not in this composite catalog; refresh discovery")
        content = await owner[1].load(skill)
        if not isinstance(content, str):
            raise TypeError("SkillDiscoverer.load must return str")
        return content
