"""Build the native distribution with a hash-pinned, relocatable Python.

Supported platforms: Linux x86_64, macOS arm64 and macOS x86_64. Each has its
own reviewed runtime lock and vendored upstream notices (see PLATFORMS).

Only the builder needs Python. Dependencies are installed from reviewed binary
wheels, never resolved or compiled during a build. The runtime's own pip performs
the final dependency check; no host Python, pip, venv or generated script is used
to launch the installed adapter. See runtime-lock.json for upstream provenance.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import IO, BinaryIO
from urllib.error import URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PlatformSpec:
    """One validated native target. Linux keeps its original lock/notice paths."""

    name: str
    system: str
    machines: tuple[str, ...]
    triple: str
    wheel_tag: str

    def lock_path(self, root: Path) -> Path:
        suffix = "" if self.name == "linux-x86_64" else f".{self.name}"
        return root / f"ngn/runtime-lock{suffix}.json"

    def notices_dir(self, root: Path) -> Path:
        suffix = "" if self.name == "linux-x86_64" else f"-{self.name}"
        return root / f"ngn/runtime-licenses{suffix}"

    def wheel_compatible(self, filename_or_tag: str) -> bool:
        """Accept py3-none-any or a CPython 3.13 binary tag for exactly this target."""
        tags = filename_or_tag.removesuffix(".whl").split("-")[-3:] if filename_or_tag.endswith(".whl") else None
        if tags is not None:
            python, abi, platforms = tags
            return all(self.wheel_compatible(f"{python}-{abi}-{platform_tag}") for platform_tag in platforms.split("."))
        if filename_or_tag == "py3-none-any":
            return True
        return re.fullmatch(rf"cp313-cp313-{self.wheel_tag}", filename_or_tag) is not None


PLATFORMS = {
    spec.name: spec
    for spec in (
        PlatformSpec("linux-x86_64", "Linux", ("x86_64",), "x86_64-unknown-linux-gnu", r"manylinux[0-9_]+_x86_64"),
        PlatformSpec("macos-arm64", "Darwin", ("arm64",), "aarch64-apple-darwin", r"macosx_\d+_\d+_arm64"),
        PlatformSpec("macos-x86_64", "Darwin", ("x86_64",), "x86_64-apple-darwin", r"macosx_\d+_\d+_x86_64"),
    )
}


def host_platform() -> str:
    """The validated target this host builds for; empty when unsupported."""
    for spec in PLATFORMS.values():
        if platform.system() == spec.system and platform.machine() in spec.machines:
            return spec.name
    return ""


PLATFORM = host_platform() or "linux-x86_64"
RUNNER = ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"]
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
DOCUMENTS = ("LICENSE", "README.md", "CHANGELOG.md", "config.example.json", "config.chatgpt.example.json", "docs")


@dataclass(frozen=True)
class Limits:
    compressed: int = 256 * 1024 * 1024
    expanded: int = 1024 * 1024 * 1024
    members: int = 32_000
    manifest: int = 8 * 1024 * 1024


LIMITS = Limits()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected a JSON object")
    return {str(key): item for key, item in value.items()}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected nonempty text")
    return value


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array")
    return list(value)


def _positive(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Expected a positive integer")
    return value


def _checksum(value: object) -> str:
    result = _text(value)
    if SHA256.fullmatch(result) is None:
        raise ValueError("Invalid SHA-256")
    return result


def _json(data: bytes, limits: Limits = LIMITS) -> dict[str, object]:
    if len(data) > limits.manifest:
        raise ValueError("Manifest exceeds byte limit")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return _object(json.loads(data, object_pairs_hook=unique))


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with _regular(path) as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def _regular(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Expected a regular file")
    return os.fdopen(descriptor, "rb")


def _private_directory(path: Path, *, private: bool = True) -> Path:
    """Create missing ancestors privately; never follow or repair unsafe ones."""
    path = path.absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise ValueError("Unsafe directory ancestry")
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky_root):
                raise ValueError("Unsafe directory ancestry")
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("Cache must be an existing private directory (0700); refusing to repair it")
    finally:
        os.close(descriptor)
    return path


def prepare_output(path: Path) -> Path:
    """Create each missing output ancestor privately without repairing existing paths."""
    return _private_directory(path, private=False)


def _url(url: str, *, redirect: bool = False) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.fragment:
        raise ValueError("Only reviewed HTTPS providers are permitted")
    if redirect and parsed.hostname == "release-assets.githubusercontent.com":
        return  # GitHub's short-lived, signed release-asset redirect, never persisted.
    if parsed.query:
        raise ValueError("Queries are not permitted in upstream URLs")
    if parsed.hostname == "github.com" and parsed.path.startswith(
        "/astral-sh/python-build-standalone/releases/download/"
    ):
        return
    if parsed.hostname == "files.pythonhosted.org" and parsed.path.startswith("/packages/"):
        return
    if parsed.hostname == "pypi.org" and re.fullmatch(r"/pypi/nagents/[0-9]+\.[0-9]+\.[0-9]+/json", parsed.path):
        return
    raise ValueError("Unreviewed upstream provider or path")


class _Redirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: object, newurl: str
    ) -> Request:
        _url(newurl, redirect=True)
        return Request(newurl, headers={"User-Agent": "ngn-bundle/1"})


@dataclass(frozen=True)
class Artifact:
    filename: str
    url: str
    sha256: str
    size: int

    @classmethod
    def parse(cls, value: object) -> Artifact:
        data = _object(value)
        name = _text(data["filename"])
        if name != Path(name).name or "\\" in name or name in {".", ".."}:
            raise ValueError("Unsafe artifact filename")
        url = _text(data["url"])
        _url(url)
        if unquote(urlsplit(url).path.rsplit("/", 1)[-1]) != name:
            raise ValueError("Artifact URL/filename mismatch")
        return cls(name, url, _checksum(data["sha256"]), _positive(data["size"]))


def _download(url: str, destination: BinaryIO, maximum: int) -> int:
    _url(url)
    total = 0
    with build_opener(_Redirect()).open(Request(url, headers={"User-Agent": "ngn-bundle/1"}), timeout=60) as response:
        _url(response.url, redirect=True)
        length = response.headers.get("Content-Length")
        if length is not None and (int(length) < 0 or int(length) > maximum):
            raise ValueError("Download exceeds compressed byte limit")
        while block := response.read(min(1024 * 1024, maximum - total + 1)):
            total += len(block)
            if total > maximum:
                raise ValueError("Download exceeds compressed byte limit")
            destination.write(block)
    return total


def _fetch(artifact: Artifact, cache: Path, limits: Limits = LIMITS) -> Path:
    _url(artifact.url)
    if artifact.size > limits.compressed:
        raise ValueError("Artifact exceeds compressed byte limit")
    cache = _private_directory(cache)
    target = cache / f"{artifact.sha256}-{artifact.filename}"
    if target.exists() or target.is_symlink():
        if target.lstat().st_size != artifact.size or digest(target) != artifact.sha256:
            raise ValueError("Corrupt cached artifact; refusing to reuse it")
        return target
    descriptor, temporary = tempfile.mkstemp(prefix="download-", dir=cache)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            size = _download(artifact.url, stream, min(artifact.size, limits.compressed))
        if size != artifact.size or digest(temporary_path) != artifact.sha256:
            raise ValueError("Upstream artifact SHA-256/size mismatch")
        try:
            os.link(temporary_path, target, follow_symlinks=False)
        except FileExistsError:
            if target.lstat().st_size != artifact.size or digest(target) != artifact.sha256:
                raise ValueError("Conflicting cached artifact") from None
        return target
    finally:
        temporary_path.unlink()


def _parts(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ValueError("Unsafe archive path")
    parts = tuple(name.removesuffix("/").split("/"))
    if any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise ValueError("Unsafe archive path")
    return parts


def _link(path: tuple[str, ...], target: str) -> tuple[str, ...]:
    if not target or "\\" in target or "\x00" in target or target.startswith("/"):
        raise ValueError("Unsafe symlink target")
    result = list(path[:-1])
    for part in target.split("/"):
        if part == "..":
            if not result:
                raise ValueError("Escaping symlink target")
            result.pop()
        elif part not in {"", "."}:
            result.append(part)
    if not result:
        raise ValueError("Escaping symlink target")
    return tuple(result)


class _BoundedReader(io.RawIOBase):
    def __init__(self, source: gzip.GzipFile, maximum: int) -> None:
        self.source = source
        self.remaining = maximum

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        block = self.source.read(self.remaining + 1 if size < 0 else min(size, self.remaining + 1))
        self.remaining -= len(block)
        if self.remaining < 0:
            raise ValueError("Archive exceeds expanded byte limit")
        return block


def _tar_members(path: Path, limits: Limits) -> Iterator[tuple[tarfile.TarFile, tarfile.TarInfo]]:
    with (
        _regular(path) as source,
        gzip.GzipFile(fileobj=source) as compressed,
        _BoundedReader(compressed, limits.expanded) as bounded,
        tarfile.open(fileobj=bounded, mode="r|") as archive,
    ):
        for index, member in enumerate(archive, 1):
            if index > limits.members:
                raise ValueError("Archive exceeds member count limit")
            yield archive, member
        while bounded.read(1024 * 1024):
            pass  # Bound padding/trailing gzip data too, not only tar payload sizes.


def _extract_tar(path: Path, destination: Path, *, prefix: str = "python", limits: Limits = LIMITS) -> None:
    """Validate the entire archive before writing, then create links last."""
    if path.stat().st_size > limits.compressed:
        raise ValueError("Archive exceeds compressed byte limit")
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Extraction requires a new empty private staging directory")
    entries: dict[tuple[str, ...], tarfile.TarInfo] = {}
    size = 0
    for _, member in _tar_members(path, limits):
        parts = _parts(member.name)
        if parts[0] != prefix:
            raise ValueError("Unexpected archive root")
        if parts in entries:
            raise ValueError("Duplicate archive member")
        if not (member.isfile() or member.isdir() or member.issym()) or member.issparse() or member.mode & 0o6000:
            raise ValueError("Special files, hardlinks and privileged modes are forbidden")
        if len(parts) == 1 and not member.isdir():
            raise ValueError("Archive root must be a directory")
        if member.size < 0:
            raise ValueError("Negative archive member size")
        size += member.size
        if size > limits.expanded:
            raise ValueError("Archive exceeds expanded byte limit")
        entries[parts] = member
    for parts, member in entries.items():
        for length in range(1, len(parts)):
            parent = entries.get(parts[:length])
            if parent is not None and not parent.isdir():
                raise ValueError("Archive member has a non-directory ancestor")
        if member.issym():
            link_target = _link(parts, member.linkname)
            if link_target[0] != prefix or link_target not in entries:
                raise ValueError("Escaping or missing symlink target")
            seen = {parts}
            while entries[link_target].issym():
                if link_target in seen:
                    raise ValueError("Cyclic symlink")
                seen.add(link_target)
                link_target = _link(link_target, entries[link_target].linkname)
                if link_target[0] != prefix or link_target not in entries:
                    raise ValueError("Escaping or missing symlink target")
    for archive, member in _tar_members(path, limits):
        parts = _parts(member.name)[1:]
        target = destination.joinpath(*parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Missing archive file data")
            with source, target.open("xb") as stream:
                shutil.copyfileobj(source, stream, length=1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for parts, member in sorted(entries.items()):
        if member.issym():
            target = destination.joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            target.symlink_to(member.linkname)
    for parts, member in entries.items():
        if member.issym() and not destination.joinpath(*parts[1:]).resolve(strict=True).is_relative_to(destination):
            raise ValueError("Escaping symlink")


def _wheel_entries(wheel: ZipFile, limits: Limits) -> list[ZipInfo]:
    entries = wheel.infolist()
    if len(entries) > limits.members:
        raise ValueError("Wheel exceeds member count limit")
    seen: set[tuple[str, ...]] = set()
    total = 0
    for member in entries:
        parts = _parts(member.filename)
        mode = member.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or mode & 0o6000 or member.flag_bits & 1:
            raise ValueError("Special, privileged or encrypted wheel member")
        if parts in seen:
            raise ValueError("Duplicate wheel member")
        if member.filename.endswith((".pth", "/direct_url.json")) or parts[-1] in {
            "sitecustomize.py",
            "usercustomize.py",
            "pyvenv.cfg",
        }:
            raise ValueError("Editable or interpreter-customizing wheel content is forbidden")
        seen.add(parts)
        total += member.file_size
        if total > limits.expanded:
            raise ValueError("Wheel exceeds expanded byte limit")
    return entries


def _normal_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_metadata(path: Path, name: str, version: str, limits: Limits = LIMITS) -> str:
    if path.stat().st_size > limits.compressed or path.suffix != ".whl":
        raise ValueError("Expected a bounded binary wheel")
    with ZipFile(path) as wheel:
        entries = _wheel_entries(wheel, limits)
        metadata = [member for member in entries if member.filename.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or len(_parts(metadata[0].filename)) != 2:
            raise ValueError("Expected exactly one wheel distribution")
        if metadata[0].file_size > limits.manifest:
            raise ValueError("Wheel metadata exceeds byte limit")
        parsed = BytesParser().parsebytes(wheel.read(metadata[0]))
        if _normal_name(str(parsed["Name"])) != _normal_name(name) or str(parsed["Version"]) != version:
            raise ValueError("Wheel distribution name/version mismatch")
        distribution = metadata[0].filename.split("/", 1)[0]
        distributions = {
            _parts(member.filename)[0] for member in entries if _parts(member.filename)[0].endswith(".dist-info")
        }
        if distributions != {distribution}:
            raise ValueError("Wheel contains foreign distribution metadata")
        wheel_info = wheel.getinfo(f"{distribution}/WHEEL")
        wheel.getinfo(f"{distribution}/RECORD")
        if wheel_info.file_size > limits.manifest:
            raise ValueError("Wheel metadata exceeds byte limit")
        wheel_metadata = BytesParser().parsebytes(wheel.read(wheel_info))
        if wheel_metadata["Wheel-Version"] != "1.0":
            raise ValueError("Unsupported wheel format")
        tags = wheel_metadata.get_all("Tag", [])
        spec = PLATFORMS[PLATFORM]
        if not tags or not all(spec.wheel_compatible(str(tag)) for tag in tags):
            raise ValueError(f"Wheel is not compatible with the pinned CPython/{spec.name} target")
        return distribution


def _install_wheel(path: Path, site: Path, name: str, version: str, limits: Limits = LIMITS) -> None:
    """Install the wheel's library scheme without host-bound console scripts.

    Entry-point scripts are deliberately not generated: ngn uses python -I -m.
    All dist-info/license data is retained; RECORD describes installed files.
    Unreviewed wheel data schemes fail closed rather than silently losing files.
    """
    distribution = _wheel_metadata(path, name, version, limits)
    installed: list[Path] = []
    with ZipFile(path) as wheel:
        for member in _wheel_entries(wheel, limits):
            parts = _parts(member.filename)
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                    raise ValueError("Unreviewed wheel data scheme")
                parts = parts[2:]
            target = site.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with wheel.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755 if member.external_attr >> 16 & 0o111 else 0o644)
            installed.append(target)
    installer = site / distribution / "INSTALLER"
    if installer.exists():
        raise ValueError("Wheel must not predeclare its installer")
    installer.write_text("ngn-bundle\n")
    installed.append(installer)
    record = site / distribution / "RECORD"
    with record.open("w", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        for target in sorted(installed):
            relative = target.relative_to(site).as_posix()
            if target == record:
                writer.writerow((relative, "", ""))
            else:
                checksum = base64.urlsafe_b64encode(bytes.fromhex(digest(target))).rstrip(b"=").decode()
                writer.writerow((relative, f"sha256={checksum}", target.stat().st_size))


def _public_core_metadata(version: str, limits: Limits) -> dict[str, object]:
    url = f"https://pypi.org/pypi/nagents/{version}/json"
    stream = io.BytesIO()
    try:
        _download(url, stream, limits.manifest)
    except URLError as error:
        raise ValueError(
            f"Public PyPI metadata for nagents {version} is unavailable; finish PyPI publication first, "
            "then retry the failed native job without rebuilding or relabelling core distributions"
        ) from error
    metadata = _json(stream.getvalue(), limits)
    info = _object(metadata["info"])
    if info["name"] != "nagents" or info["version"] != version:
        raise ValueError("Public PyPI core metadata mismatch")
    return metadata


def _public_core_asset(metadata: dict[str, object], filename: str, kind: str) -> Artifact:
    matches: list[Artifact] = []
    for value in _items(metadata["urls"]):
        item = _object(value)
        if item.get("filename") == filename and item.get("packagetype") == kind and not item.get("yanked"):
            artifact = Artifact.parse(
                {
                    "filename": item["filename"],
                    "url": item["url"],
                    "size": item["size"],
                    "sha256": _object(item["digests"])["sha256"],
                }
            )
            if urlsplit(artifact.url).hostname != "files.pythonhosted.org":
                raise ValueError("Public core distributions must use files.pythonhosted.org")
            matches.append(artifact)
    if len(matches) != 1:
        raise ValueError("Expected exactly one unyanked public PyPI core distribution for the fixed version")
    return matches[0]


def _published_core(wheel: Path, version: str, limits: Limits = LIMITS) -> dict[str, object]:
    """Bind official core bytes to fixed public PyPI version metadata, not an index override."""
    metadata = _public_core_metadata(version, limits)
    artifact = _public_core_asset(metadata, wheel.name, "bdist_wheel")
    if artifact.size != wheel.stat().st_size or artifact.sha256 != digest(wheel):
        raise ValueError("Official core must be the exact published PyPI wheel, not a source rebuild")
    return {
        "metadata_url": f"https://pypi.org/pypi/nagents/{version}/json",
        "url": artifact.url,
        "sha256": artifact.sha256,
        "filename": wheel.name,
    }


@dataclass(frozen=True)
class CoreArtifacts:
    """The two core distributions attached to the single coordinated release."""

    wheel: Path
    sdist: Path


def download_published_core(destination: Path, *, version: str, source_commit: str) -> CoreArtifacts:
    """Fetch exact public wheel/sdist bytes without pip or executing sdist hooks."""
    destination = prepare_output(destination)
    metadata = _public_core_metadata(version, LIMITS)
    artifacts = (
        _public_core_asset(metadata, f"nagents-{version}-py3-none-any.whl", "bdist_wheel"),
        _public_core_asset(metadata, f"nagents-{version}.tar.gz", "sdist"),
    )
    with tempfile.TemporaryDirectory(prefix="public-core-", dir=destination.parent) as temporary:
        for artifact in artifacts:
            verified = _fetch(artifact, Path(temporary))
            with _regular(verified) as source, (destination / artifact.filename).open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
    result = CoreArtifacts(destination / artifacts[0].filename, destination / artifacts[1].filename)
    verify_core_artifacts(result, version=version, source_commit=source_commit)
    return result


def verify_core_artifacts(
    artifacts: CoreArtifacts, *, version: str, source_commit: str, published: bool = False
) -> dict[str, object]:
    """Verify metadata, bounded archive paths, and exact source before any build/import."""
    expected_names = (f"nagents-{version}-py3-none-any.whl", f"nagents-{version}.tar.gz")
    if (artifacts.wheel.name, artifacts.sdist.name) != expected_names:
        raise ValueError("Core wheel/source distribution filename mismatch")
    if published:
        metadata = _public_core_metadata(version, LIMITS)
        for path, kind in ((artifacts.wheel, "bdist_wheel"), (artifacts.sdist, "sdist")):
            artifact = _public_core_asset(metadata, path.name, kind)
            if artifact.size != path.stat().st_size or artifact.sha256 != digest(path):
                raise ValueError(f"Core distribution is not the exact public PyPI bytes: {path.name}")
    _wheel_metadata(artifacts.wheel, "nagents", version)
    binding = _core_source(artifacts.wheel, source_commit)
    _core_sdist(artifacts.sdist, version=version, source_commit=source_commit)
    return {**binding, "wheel_sha256": digest(artifacts.wheel), "sdist_sha256": digest(artifacts.sdist)}


def _core_sdist(path: Path, *, version: str, source_commit: str, limits: Limits = LIMITS) -> None:
    if path.stat().st_size > limits.compressed:
        raise ValueError("Core source distribution exceeds compressed byte limit")
    expected = _core_python_sources(source_commit)
    project = _git_files(source_commit, ("pyproject.toml",))["pyproject.toml"]
    sources: dict[str, bytes] = {}
    seen: set[tuple[str, ...]] = set()
    regular: set[tuple[str, ...]] = set()
    metadata_found = project_found = False
    prefix = f"nagents-{version}"
    for archive, member in _tar_members(path, limits):
        parts = _parts(member.name)
        if parts[0] != prefix:
            raise ValueError("Foreign core source distribution root")
        if parts in seen:
            raise ValueError("Duplicate core source distribution member")
        seen.add(parts)
        if not (member.isfile() or member.isdir()) or member.issparse() or member.mode & 0o6000:
            raise ValueError("Core source distribution permits only regular files/directories")
        if len(parts) == 1 and not member.isdir():
            raise ValueError("Core source distribution root must be a directory")
        if member.isfile():
            regular.add(parts)
        relative = "/".join(parts[1:])
        module = relative.removeprefix("src/")
        wanted = relative in {"PKG-INFO", "pyproject.toml"} or (
            relative.startswith("src/nagents/")
            and relative.endswith((".py", "/py.typed"))
            and not module.startswith(("nagents/web-ui/", "nagents/web/static/"))
        )
        if not wanted:
            continue
        if not member.isfile() or member.size > limits.manifest:
            raise ValueError("Invalid or oversized core source distribution metadata/source")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("Missing core source distribution bytes")
        with stream:
            contents = stream.read(limits.manifest + 1)
        if relative == "PKG-INFO":
            metadata = BytesParser().parsebytes(contents)
            if metadata["Name"] != "nagents" or metadata["Version"] != version:
                raise ValueError("Core source distribution package name/version mismatch")
            metadata_found = True
        elif relative == "pyproject.toml":
            parsed = tomllib.loads(contents.decode())
            if (
                _object(parsed["project"]).get("name") != "nagents"
                or _object(parsed["project"]).get("version") != version
            ):
                raise ValueError("Core source distribution project name/version mismatch")
            if contents != project:
                raise ValueError("Core source distribution project differs from exact source_commit")
            project_found = True
        else:
            sources[module] = contents
    if any(parts[:length] in regular for parts in seen for length in range(1, len(parts))):
        raise ValueError("Core source distribution member has a regular-file ancestor")
    if not metadata_found or not project_found or sources != expected:
        raise ValueError("Core source distribution does not contain the exact checked-in Python source")


def verify_bundle_core(archive: Path, wheel: Path, *, version: str, source_commit: str, official: bool) -> None:
    """Bind the attached wheel to provenance AND every installed core payload byte."""
    if archive.stat().st_size > LIMITS.compressed:
        raise ValueError("Bundle exceeds compressed byte limit")
    distribution = _wheel_metadata(wheel, "nagents", version)
    site = "runtime/lib/python3.13/site-packages/"
    expected: dict[str, str] = {}
    with ZipFile(wheel) as packaged:
        for wheel_member in _wheel_entries(packaged, LIMITS):
            if wheel_member.is_dir() or wheel_member.filename == f"{distribution}/RECORD":
                continue
            parts = _parts(wheel_member.filename)
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                    raise ValueError("Unreviewed core wheel data scheme")
                parts = parts[2:]
            checksum = hashlib.sha256()
            with packaged.open(wheel_member) as stream:
                while block := stream.read(1024 * 1024):
                    checksum.update(block)
            expected[site + "/".join(parts)] = checksum.hexdigest()
    found: dict[str, str] = {}
    documents: dict[str, dict[str, object]] = {}
    document_hashes: dict[str, str] = {}
    seen: set[tuple[str, ...]] = set()
    for source, member in _tar_members(archive, LIMITS):
        parts = _parts(member.name)
        if parts[0] != "ngn" or parts in seen:
            raise ValueError("Foreign or duplicate bundle member")
        seen.add(parts)
        relative = "/".join(parts[1:])
        core_path = relative.startswith((site + "nagents/", site + distribution + "/"))
        if (
            core_path
            and relative not in expected
            and relative not in {site + distribution + "/RECORD", site + distribution + "/INSTALLER"}
            and not member.isdir()
        ):
            raise ValueError("Bundled core contains files absent from the attached wheel")
        if relative not in expected and relative not in {"bundle.json", "notices/provenance.json"}:
            continue
        if not member.isfile() or member.issparse():
            raise ValueError("Bundled core/provenance must be regular files")
        data = source.extractfile(member)
        if data is None:
            raise ValueError("Missing bundled core bytes")
        with data:
            if relative in expected:
                checksum = hashlib.sha256()
                while block := data.read(1024 * 1024):
                    checksum.update(block)
                found[relative] = checksum.hexdigest()
            else:
                if member.size > LIMITS.manifest:
                    raise ValueError("Oversized bundle provenance")
                contents = data.read(LIMITS.manifest + 1)
                documents[relative] = _json(contents)
                document_hashes[relative] = hashlib.sha256(contents).hexdigest()
    if found != expected or set(documents) != {"bundle.json", "notices/provenance.json"}:
        raise ValueError("Bundled core bytes differ from the attached wheel")
    metadata = documents["bundle.json"]
    provenance = documents["notices/provenance.json"]
    core = _object(provenance["core"])
    if core.get("sha256") != digest(wheel) or core.get("filename") != wheel.name:
        raise ValueError("Attached core wheel differs from the exact embedded wheel provenance")
    if (
        metadata.get("source_commit") != source_commit
        or provenance.get("source_commit") != source_commit
        or metadata.get("nagents_version") != version
    ):
        raise ValueError("Bundled core commit/version provenance mismatch")
    if official and (metadata.get("official") is not True or core.get("kind") != "public-pypi-wheel"):
        raise ValueError("Official bundle requires published core provenance")
    files = _object(metadata["files"])
    if files.get("notices/provenance.json") != document_hashes["notices/provenance.json"]:
        raise ValueError("Bundled core provenance is not bound to the inventory")
    if any(files.get(name) != checksum for name, checksum in expected.items()):
        raise ValueError("Bundled core inventory differs from attached wheel bytes")


def _notices(lock: dict[str, object]) -> dict[str, bytes]:
    notices = _object(lock["python_notices"])
    # A stripped Python archive has only PSF's license. Require the separately
    # reviewed full-build metadata and every referenced third-party notice.
    source = Artifact.parse(notices["source"])
    if not source.filename.endswith(".tar.zst") or not source.url.startswith(
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
    ):
        raise ValueError("Python notices require matching full-build provenance")
    spec = PLATFORMS[_text(lock["platform"])]
    directory = spec.notices_dir(ROOT)
    with _regular(directory / "PROVENANCE.json") as stream:
        contents = stream.read(LIMITS.manifest + 1)
    if hashlib.sha256(contents).hexdigest() != _checksum(notices["provenance_sha256"]):
        raise ValueError("Corrupt Python notice provenance")
    provenance = _json(contents)
    python = _object(lock["python"])
    runtime = Artifact.parse(python["artifact"])
    pin = _object(provenance["pin"])
    full = _object(provenance["full_archive"])
    if (
        pin["runtime_sha256"] != runtime.sha256
        or pin["runtime_asset"] != runtime.filename
        or full["sha256"] != source.sha256
        or full["url"] != source.url
        or full["release_commit"] != python["release_commit"]
    ):
        raise ValueError("Python notices are not bound to this exact standalone runtime")
    files: dict[str, bytes] = {"PROVENANCE.json": contents}
    for value in _items(provenance["files"]):
        item = _object(value)
        path = _text(item["vendor_path"])
        with _regular(directory.joinpath(*_parts(path))) as stream:
            contents = stream.read(LIMITS.manifest + 1)
        if (
            path in files
            or len(contents) != _positive(item["size"])
            or hashlib.sha256(contents).hexdigest() != _checksum(item["sha256"])
        ):
            raise ValueError("Duplicate or corrupt Python license notice")
        files[path] = contents
    actual: set[str] = set()
    for path_object in directory.rglob("*"):
        mode = path_object.lstat().st_mode
        if stat.S_ISREG(mode):
            actual.add(path_object.relative_to(directory).as_posix())
        elif not stat.S_ISDIR(mode):
            raise ValueError("Python notices must be regular vendored files")
    if set(files) != actual or not {"PYTHON.json", "PYTHON-RUNTIME/LICENSE.txt", "UPSTREAM/MPL-2.0.txt"}.issubset(
        files
    ):
        raise ValueError("Complete upstream Python/third-party license notices are required")
    metadata = _json(files["PYTHON.json"])
    if metadata.get("python_version") != python["version"] or metadata.get("target_triple") != spec.triple:
        raise ValueError("Python notice metadata version mismatch")
    required = _license_paths(metadata)
    if not required or not required.issubset(files):
        raise ValueError("PYTHON.json references missing third-party license notices")
    return files


def _license_paths(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in _object(value).items():
            if key in {"license_path", "license_paths"}:
                for name in [item] if isinstance(item, str) else _items(item):
                    parts = _parts(_text(name))
                    if len(parts) != 2 or parts[0] != "licenses":
                        raise ValueError("Unexpected upstream license path")
                    result.add(f"LICENSES/{parts[1]}")
            else:
                result.update(_license_paths(item))
    elif isinstance(value, list):
        for item in _items(value):
            result.update(_license_paths(item))
    return result


def _load_lock() -> dict[str, object]:
    with _regular(PLATFORMS[PLATFORM].lock_path(ROOT)) as source:
        lock = _json(source.read(LIMITS.manifest + 1))
    if lock.get("lock_schema") != 1 or lock.get("platform") != PLATFORM:
        raise ValueError("Unsupported runtime lock schema/platform")
    python = _object(lock["python"])
    artifact = Artifact.parse(python["artifact"])
    version = _text(python["version"])
    if python.get("implementation") != "cpython" or re.fullmatch(r"3\.13\.\d+", version) is None:
        raise ValueError("Only pinned non-free-threaded CPython 3.13 is supported")
    triple = PLATFORMS[PLATFORM].triple
    expected = f"cpython-{version}+{_text(python['release'])}-{triple}-install_only_stripped.tar.gz"
    if artifact.filename != expected:
        raise ValueError("Unexpected standalone Python target/build flavor")
    if COMMIT.fullmatch(_text(python["release_commit"])) is None:
        raise ValueError("Missing Python release provenance")
    names: set[str] = set()
    for value in _items(lock["wheels"]):
        wheel = _object(value)
        name = _normal_name(_text(wheel["name"]))
        if name in names or name in {"nagents", "ngn-nagents"}:
            raise ValueError("Duplicate or reserved dependency in runtime lock")
        names.add(name)
        _text(wheel["version"])
        artifact = Artifact.parse(wheel["artifact"])
        if not artifact.filename.endswith(".whl") or urlsplit(artifact.url).hostname != "files.pythonhosted.org":
            raise ValueError("Dependencies must be pinned public binary wheels")
    if not {"aiohttp", "aiosqlite", "pyyaml", "certifi"}.issubset(names):
        raise ValueError("Runtime lock omits a required dependency or bundled CA")
    _notices(lock)
    return lock


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")


def _git_files(commit: str, paths: tuple[str, ...], *, suffixes: tuple[str, ...] = ()) -> dict[str, bytes]:
    listing = subprocess.check_output(["git", "ls-tree", "-rz", commit, "--", *paths], cwd=ROOT)
    files: dict[str, bytes] = {}
    for record in listing.split(b"\x00"):
        if not record:
            continue
        header, filename = record.split(b"\t", 1)
        mode, kind, object_id = header.decode().split(" ")
        parts = _parts(filename.decode())
        if suffixes and not filename.decode().endswith(suffixes):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("Only checked-in regular source files may be bundled")
        files["/".join(parts)] = subprocess.check_output(["git", "cat-file", "blob", object_id], cwd=ROOT)
    return files


def _core_python_sources(commit: str) -> dict[str, bytes]:
    excluded = ("nagents/web-ui/", "nagents/web/static/")
    return {
        name.removeprefix("src/"): contents
        for name, contents in _git_files(commit, ("src/nagents",), suffixes=(".py", "/py.typed")).items()
        if not name.removeprefix("src/").startswith(excluded)
    }


def _core_source(wheel: Path, commit: str) -> dict[str, object]:
    """Official published code must also be the exact checked-in Python code."""
    excluded = ("nagents/web-ui/", "nagents/web/static/")
    expected = _core_python_sources(commit)
    with ZipFile(wheel) as archive:
        actual = {
            member.filename: archive.read(member)
            for member in archive.infolist()
            if member.filename.startswith("nagents/")
            and member.filename.endswith((".py", "/py.typed"))
            and not member.filename.startswith(excluded)
        }
    if not expected or expected != actual:
        raise ValueError(
            "Official core Python source differs from source_commit; release the matching core first and bump its version, "
            "then provide that exact public PyPI wheel"
        )
    hashes = {name: hashlib.sha256(contents).hexdigest() for name, contents in sorted(expected.items())}
    return {
        "source_python_files": len(hashes),
        "source_python_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _adapter_source(wheel: Path, commit: str) -> None:
    prefix = "ngn/integrations/nagents/src/"
    expected = {
        name.removeprefix(prefix): contents for name, contents in _git_files(commit, (prefix + "ngn_nagents",)).items()
    }
    with ZipFile(wheel) as archive:
        for member in archive.infolist():
            top = _parts(member.filename)[0]
            if top != "ngn_nagents" and not (top.startswith("ngn_nagents-") and top.endswith(".dist-info")):
                raise ValueError("Adapter wheel must contain only the exact source_commit adapter and its dist-info")
        actual = {
            member.filename: archive.read(member)
            for member in archive.infolist()
            if member.filename.startswith("ngn_nagents/") and not member.is_dir()
        }
    if not expected or actual != expected:
        raise ValueError("Adapter wheel must contain the exact source_commit adapter, without extra package files")


def _documents(destination: Path, commit: str) -> None:
    for name, contents in _git_files(commit, tuple(f"ngn/{name}" for name in DOCUMENTS)).items():
        parts = _parts(name)
        if parts[0] != "ngn":
            raise ValueError("Unexpected documentation source root")
        target = destination.joinpath(*parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(contents)
    if any(not (destination / name).exists() for name in DOCUMENTS):
        raise ValueError("Source commit lacks required documentation/examples/licenses")


def _inventory(root: Path, limits: Limits = LIMITS) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    symlinks: dict[str, str] = {}
    expanded = 0
    paths = sorted(root.rglob("*"))
    if len(paths) + 1 > limits.members:
        raise ValueError("Bundle exceeds member count limit")
    for path in paths:
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if info.st_mode & 0o6000:
            raise ValueError("Privileged bundle modes are forbidden")
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            _link(_parts(relative), target)
            if not relative.startswith("runtime/") or not path.resolve(strict=True).is_relative_to(root / "runtime"):
                raise ValueError("Only internal runtime symlinks are permitted")
            symlinks[relative] = target
        elif stat.S_ISREG(info.st_mode):
            expanded += info.st_size
            if expanded > limits.expanded or info.st_nlink != 1:
                raise ValueError("Bundle exceeds byte limit or contains hardlinks")
            if relative != "bundle.json":
                files[relative] = digest(path)
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("Special bundle file")
    return files, symlinks


def _verify_inventory(root: Path, limits: Limits = LIMITS) -> None:
    metadata = _json((root / "bundle.json").read_bytes(), limits)
    files, links = _inventory(root, limits)
    if metadata["files"] != files or metadata["symlinks"] != links:
        raise ValueError("Bundle inventory mismatch")


PROBE = r"""
import importlib, importlib.metadata as md, json, pathlib, ssl, sys
from nagents import Agent
from ngn_nagents import auth
import certifi
root = pathlib.Path(sys.executable).resolve().parents[2]
assert sys.prefix == sys.base_prefix, 'runtime must not be a venv'
assert pathlib.Path(sys.prefix).resolve() == root / 'runtime'
assert all(pathlib.Path(path).resolve().is_relative_to(root / 'runtime') for path in sys.path)
modules = {}
for name in ('nagents', 'ngn_nagents', 'certifi', 'aiohttp', 'aiosqlite', 'yaml'):
    module = importlib.import_module(name)
    path = pathlib.Path(module.__file__).resolve()
    assert path.is_relative_to(root / 'runtime'), 'outside-source import'
    modules[name] = path.relative_to(root).as_posix()
assert callable(Agent)
try:
    public_auth = importlib.import_module('nagents.provider.device_auth')
except ImportError:
    raise AssertionError('published core lacks the required public device_auth API') from None
core_files = {pathlib.Path(path.locate()).resolve() for path in (md.distribution('nagents').files or ())}
assert pathlib.Path(public_auth.__file__).resolve() in core_files, 'public auth API must belong to the exact core wheel'
for name in ('OpenAIAuth', 'DeviceAuthorization', 'OpenAIAuthError'):
    assert isinstance(getattr(public_auth, name, None), type), 'public auth type missing'
for name in ('credentials', 'start_device_login', 'complete_device_login', 'status', 'logout_async', 'close'):
    assert callable(getattr(public_auth.OpenAIAuth, name, None)), 'public auth method missing'
assert auth.available(), 'public nagents device_auth API missing (published 0.28.5 is not sufficient)'
ca = pathlib.Path(certifi.where()).resolve()
assert ca.is_relative_to(root / 'runtime')
assert pathlib.Path(ssl.get_default_verify_paths().cafile).resolve() == ca
context = ssl.create_default_context()
assert len(context.get_ca_certs()) > 100
for name in ('_ssl', '_hashlib', '_sqlite3', '_ctypes', 'zlib', 'bz2', 'lzma'):
    importlib.import_module(name)
print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), 'modules': modules,
    'versions': {dist.metadata['Name']: dist.version for dist in md.distributions()},
    'auth_available': True, 'ca_file': ca.relative_to(root).as_posix(), 'ca_count': len(context.get_ca_certs())}, sort_keys=True))
"""


def _environment(root: Path, scratch: Path) -> dict[str, str]:
    ca = root / "runtime/lib/python3.13/site-packages/certifi/cacert.pem"
    return {
        "HOME": str(scratch),
        "XDG_CONFIG_HOME": str(scratch / "config"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "SSL_CERT_FILE": str(ca),
        "SSL_CERT_DIR": "",
        "REQUESTS_CA_BUNDLE": str(ca),
        "PIP_CONFIG_FILE": os.devnull,
    }


def _runtime_pip(root: Path, scratch: Path) -> None:
    """Bootstrap only from this verified Python's ensurepip wheel, never host pip.

    Installing that wheel through the same library-only scheme avoids
    ensurepip's child-pip compilation and absolute-prefix console scripts.
    """
    environment = _environment(root, scratch) | {"PATH": ""}
    interpreter = root / "runtime/bin/python3"
    available = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-B",
            "-c",
            "import importlib.util; raise SystemExit(importlib.util.find_spec('pip') is None)",
        ],
        env=environment,
        cwd=scratch,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if available.returncode not in {0, 1}:
        raise ValueError("Standalone interpreter could not check its own pip availability")
    if available.returncode:
        bundled = list((root / "runtime/lib/python3.13/ensurepip/_bundled").glob("pip-*-py3-none-any.whl"))
        if len(bundled) != 1:
            raise ValueError("Standalone Python has neither pip nor exactly one bundled ensurepip wheel")
        _install_wheel(bundled[0], root / "runtime/lib/python3.13/site-packages", "pip", bundled[0].name.split("-")[1])
    # Neither ngn nor its runtime uses generated pip launchers. In particular,
    # ensurepip must not leave an absolute staging-prefix shebang in the bundle.
    for name in ("pip", "pip3", "pip3.13"):
        launcher = root / "runtime/bin" / name
        if launcher.exists() or launcher.is_symlink():
            if not launcher.is_file() or launcher.is_symlink():
                raise ValueError("Unexpected standalone pip launcher type")
            launcher.unlink()
    _sanitize_pip(root)


def _sanitize_pip(root: Path) -> None:
    """Remove upstream's stale file:///build origin, keeping RECORD accurate."""
    site = root / "runtime/lib/python3.13/site-packages"
    for origin in site.glob("*.dist-info/direct_url.json"):
        if not origin.parent.name.startswith("pip-") or origin.is_symlink() or not origin.is_file():
            raise ValueError("Unexpected local-origin package metadata")
        origin.unlink()
    for record in site.glob("pip-*.dist-info/RECORD"):
        rows: list[list[str]] = []
        with _regular(record) as stream:
            for row in csv.reader(io.StringIO(stream.read().decode())):
                if len(row) != 3 or Path(row[0]).is_absolute():
                    raise ValueError("Invalid standalone pip RECORD")
                target = site / row[0]
                if not target.resolve().is_relative_to(root / "runtime"):
                    raise ValueError("Escaping standalone pip RECORD")
                if target.exists():
                    rows.append(row)
        with record.open("w", newline="") as output:
            csv.writer(output, lineterminator="\n").writerows(rows)


def _probe(
    root: Path, scratch: Path, *, python_version: str, core_version: str, adapter_version: str
) -> list[dict[str, object]]:
    environment = _environment(root, scratch)
    interpreter = root / "runtime/bin/python3"
    reports: list[dict[str, object]] = []
    for path in ({"PATH": ""}, {}):
        result = subprocess.run(
            [str(interpreter), "-I", "-B", "-c", PROBE],
            env=environment | path,
            cwd=scratch,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise ValueError(
                f"Relocated runtime compatibility probe failed (requires the public core auth API): {result.stderr[-4096:]}"
            )
        report = _json(result.stdout.encode())
        versions = _object(report["versions"])
        if (
            report["python"] != python_version
            or versions["nagents"] != core_version
            or versions["ngn-nagents"] != adapter_version
        ):
            raise ValueError("Installed runtime/core/adapter version mismatch")
        reports.append({"path_environment": "empty" if path else "absent", **report})
    subprocess.run(
        [str(interpreter), "-I", "-B", "-m", "pip", "check"],
        env=environment | {"PATH": ""},
        cwd=scratch,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return reports


def _archive(root: Path, destination: Path, limits: Limits = LIMITS) -> None:
    _verify_inventory(root, limits)
    with (
        destination.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in [root, *sorted(root.rglob("*"))]:
            info = path.lstat()
            member = tarfile.TarInfo("ngn" if path == root else f"ngn/{path.relative_to(root).as_posix()}")
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            if stat.S_ISDIR(info.st_mode):
                member.type, member.mode = tarfile.DIRTYPE, 0o755
            elif stat.S_ISLNK(info.st_mode):
                member.type, member.mode, member.linkname = tarfile.SYMTYPE, 0o777, os.readlink(path)
            else:
                member.type, member.mode, member.size = (
                    tarfile.REGTYPE,
                    0o755 if info.st_mode & 0o111 else 0o644,
                    info.st_size,
                )
            if member.isfile():
                with _regular(path) as stream:
                    archive.addfile(member, stream)
            else:
                archive.addfile(member)
    if destination.stat().st_size > limits.compressed:
        raise ValueError("Bundle exceeds compressed byte limit")


def build_bundle(
    output: Path,
    binary: Path,
    *,
    core_wheel: Path,
    adapter_wheel: Path,
    official: bool,
    source_commit: str,
    cache: Path,
) -> Path:
    """Build a verified bundle; never overwrite an existing release payload.

    Official mode requires exact public PyPI core bytes AND the public auth API
    in the installed, relocated runtime. Source-built core wheels are explicitly
    unofficial. The caller retains the outer release/publication guards.
    """
    if host_platform() != PLATFORM:
        raise ValueError("Bundles are built natively; build each validated platform on that platform")
    if COMMIT.fullmatch(source_commit) is None:
        raise ValueError("source_commit must be 40 lowercase hexadecimal characters")
    if not isinstance(official, bool):
        raise ValueError("official must be a boolean")
    lock = _load_lock()
    version = _text(
        _object(_object(tomllib.loads((ROOT / "ngn/Cargo.toml").read_text())["workspace"])["package"])["version"]
    )
    core_version = _text(_object(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"])["version"])
    adapter_version = _text(
        _object(tomllib.loads((ROOT / "ngn/integrations/nagents/pyproject.toml").read_text())["project"])["version"]
    )
    if version != adapter_version or version != core_version or _object(lock["core"])["version"] != core_version:
        raise ValueError("Runtime lock/current source core/adapter version mismatch")
    cache = _private_directory(cache)
    output = _private_directory(output, private=False)
    target = output / f"ngn-v{version}-{PLATFORM}-bundle.tar.gz"
    if target.exists() or target.is_symlink():
        raise ValueError("Refusing to overwrite an existing bundle")
    python = _object(lock["python"])
    python_artifact = Artifact.parse(python["artifact"])
    with tempfile.TemporaryDirectory(prefix="bundle-", dir=cache) as temporary:
        scratch = Path(temporary)
        incoming = scratch / "incoming"
        incoming.mkdir(mode=0o700)
        for wheel in (core_wheel, adapter_wheel):
            if wheel.name != Path(wheel.name).name or wheel.suffix != ".whl":
                raise ValueError("Expected a wheel filename")
            with _regular(wheel) as source, (incoming / wheel.name).open("xb") as destination:
                if os.fstat(source.fileno()).st_size > LIMITS.compressed:
                    raise ValueError("Wheel exceeds compressed byte limit")
                shutil.copyfileobj(source, destination, length=1024 * 1024)
        core_wheel, adapter_wheel = incoming / core_wheel.name, incoming / adapter_wheel.name
        _wheel_metadata(core_wheel, "nagents", core_version)
        _wheel_metadata(adapter_wheel, "ngn-nagents", adapter_version)
        _adapter_source(adapter_wheel, source_commit)
        core_origin: dict[str, object] = {
            "kind": "unofficial-source-wheel",
            "filename": core_wheel.name,
            "sha256": digest(core_wheel),
        }
        if official:
            core_origin = {
                "kind": "public-pypi-wheel",
                **_published_core(core_wheel, core_version),
                **_core_source(core_wheel, source_commit),
            }
        upstream = _fetch(python_artifact, cache / "downloads")
        wheels: list[tuple[Path, str, str]] = [
            (core_wheel, "nagents", core_version),
            (adapter_wheel, "ngn-nagents", adapter_version),
        ]
        for value in _items(lock["wheels"]):
            item = _object(value)
            artifact = Artifact.parse(item["artifact"])
            wheel = _fetch(artifact, cache / "downloads")
            # Cache names are content-addressed; wheel parsing uses metadata,
            # while preserving the original public filename in provenance.
            local = incoming / artifact.filename
            shutil.copyfile(wheel, local, follow_symlinks=False)
            wheels.append((local, _text(item["name"]), _text(item["version"])))
        root = scratch / "ngn"
        root.mkdir(mode=0o700)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        _extract_tar(upstream, runtime)
        site = runtime / "lib/python3.13/site-packages"
        for wheel, name, wheel_version in wheels:
            _install_wheel(wheel, site, name, wheel_version)
        with _regular(binary) as source, (root / "ngn").open("xb") as destination:
            if os.fstat(source.fileno()).st_size > LIMITS.compressed:
                raise ValueError("Native binary exceeds byte limit")
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        (root / "ngn").chmod(0o755)
        _documents(root, source_commit)
        notices = root / "notices/python"
        notices.mkdir(parents=True, mode=0o755)
        for name, contents in _notices(lock).items():
            notice_path = notices.joinpath(*_parts(name))
            notice_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            notice_path.write_bytes(contents)
        _write_json(root / "notices/runtime-lock.json", lock)
        _write_json(
            root / "notices/provenance.json",
            {
                "official": official,
                "source_commit": source_commit,
                "python": python,
                "core": core_origin,
                "adapter": {"filename": adapter_wheel.name, "sha256": digest(adapter_wheel)},
                "binary_sha256": digest(root / "ngn"),
                "dependencies": lock["wheels"],
                "python_notices_source": _object(lock["python_notices"])["source"],
            },
        )
        # Moving the whole tree before execution proves it does not use its
        # extraction prefix. No source tree/venv is present in the child env.
        relocated = scratch / "relocated"
        relocated.mkdir(mode=0o700)
        root.rename(relocated / "ngn")
        root = relocated / "ngn"
        probe_home = scratch / "probe-home"
        probe_home.mkdir(mode=0o700)
        # Hash and check the assembled payload before executing its interpreter
        # or native executable. Archives/wheels were hash-verified separately.
        _inventory(root)
        _runtime_pip(root, probe_home)
        before_files, before_links = _inventory(root)
        reports = _probe(
            root,
            probe_home,
            python_version=_text(python["version"]),
            core_version=core_version,
            adapter_version=adapter_version,
        )
        native = subprocess.run(
            [str(root / "ngn"), "--version"],
            check=True,
            capture_output=True,
            text=True,
            cwd=probe_home,
            env={"PATH": "", "HOME": str(probe_home)},
            timeout=30,
        )
        if native.stdout.strip() != f"ngn {version}":
            raise ValueError("Native executable version mismatch")
        after_files, after_links = _inventory(root)
        if before_files != after_files or before_links != after_links:
            raise ValueError("Runtime probes modified the assembled payload")
        _write_json(root / "notices/runtime-probe.json", {"isolated_relocation": reports, "pip_check": "passed"})
        files, symlinks = _inventory(root)
        _write_json(
            root / "bundle.json",
            {
                "bundle_schema": 1,
                "ngn_version": version,
                "nagents_version": core_version,
                "adapter_version": adapter_version,
                "runtime_protocol": 1,
                "platform": PLATFORM,
                "source_commit": source_commit,
                "official": official,
                "python": {
                    "implementation": "cpython",
                    "version": python["version"],
                    "artifact": python_artifact.filename,
                    "sha256": python_artifact.sha256,
                },
                "runner": RUNNER,
                "ca_file": "runtime/lib/python3.13/site-packages/certifi/cacert.pem",
                "files": files,
                "symlinks": symlinks,
            },
        )
        archive = scratch / target.name
        _archive(root, archive)
        # Atomic no-clobber publication, also when output is on another FS.
        descriptor, name = tempfile.mkstemp(prefix=".bundle-", dir=output)
        pending = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as destination, archive.open("rb") as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            pending.chmod(0o644)
            os.link(pending, target, follow_symlinks=False)
        finally:
            pending.unlink()
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--adapter-wheel", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--official", action="store_true")
    args = parser.parse_args()
    print(
        build_bundle(
            args.output,
            args.binary,
            core_wheel=args.core_wheel,
            adapter_wheel=args.adapter_wheel,
            official=args.official,
            source_commit=args.source_commit,
            cache=args.cache,
        )
    )


if __name__ == "__main__":
    main()
