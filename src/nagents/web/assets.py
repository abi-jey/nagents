"""One verified frontend build for source checkouts and packaged installs."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .build_process import run_build_command

_IGNORED_DIRECTORIES = {"node_modules", ".git", ".test-build", ".vite", ".cache", "__pycache__"}


def _files(directory: Path, *, sources: bool = True) -> list[Path]:
    if not directory.is_dir():
        return []
    result: list[Path] = []
    for path in directory.iterdir():
        if path.is_symlink():
            continue
        if path.is_dir() and (not sources or path.name not in _IGNORED_DIRECTORIES):
            result.extend(_files(path, sources=sources))
        elif path.is_file() and (not sources or not path.name.endswith(".tsbuildinfo")):
            result.append(path)
    return result


def _hash_files(root: Path, files: list[Path]) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def source_hashes(source: Path) -> dict[str, str]:
    """Match the input inventory used by web-ui/scripts/build.mjs."""
    files = [
        path
        for path in source.iterdir()
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".tsbuildinfo")
    ]
    for name in ("src", "public", "scripts"):
        directory = source / name
        if not directory.is_symlink():
            files.extend(_files(directory))
    return _hash_files(source, files)


def _hash_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("Invalid frontend build manifest.")
    result: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not isinstance(digest, str):
            raise ValueError("Invalid frontend build manifest.")
        result[key] = digest
    return result


def current_build(directory: Path, source: Path, *, check_sources: bool = True) -> bool:
    """Check contents, not mtimes: Git operations must not hide stale assets."""
    try:
        manifest: object = json.loads((directory / "build.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            return False
        assets = _hash_map(manifest.get("assets"))
        if "index.html" not in assets or not any(name.startswith("assets/") for name in assets):
            return False
        actual = _hash_files(
            directory, [path for path in _files(directory, sources=False) if path != directory / "build.json"]
        )
        if actual != assets:
            return False
        sources = _hash_map(manifest.get("sources"))
        return not check_sources or not source.is_dir() or source_hashes(source) == sources
    except (OSError, ValueError):
        return False


def prepare_assets(web: Path, *, dev: bool = False) -> Path:
    """Build only the imported package's adjacent source, never the user's workspace."""
    directory = web / "static"
    source = web.parent / "web-ui"
    if dev and not source.is_dir():
        raise ValueError("ngn serve --dev requires an editable source checkout with web-ui sources.")
    if current_build(directory, source, check_sources=dev):
        return directory
    if not dev:
        raise ValueError(
            f"The ngn React assets in {directory} are missing or invalid. "
            "Reinstall a release that includes web assets. For a source checkout, activate its virtualenv, "
            "install it with `pip install -e '.[web]'`, and run `ngn serve --dev`. "
            "You can also build explicitly with `npm --prefix src/nagents/web-ui ci` then "
            "`npm --prefix src/nagents/web-ui run build`."
        )
    npm = shutil.which("npm")
    if not npm:
        raise ValueError(
            f"The ngn React assets are missing or stale for {source}. "
            "Install Node.js 20.19 or newer (including npm), then rerun `ngn serve --dev`. "
            "Packaged installs with bundled assets do not need Node.js."
        )
    print(f"ngn: rebuilding React UI from {source}", file=sys.stderr, flush=True)
    # The same locked dependency install and build command are used in Docker and CI.
    # No shell and no paths from HarnessConfig/workspace are involved.
    try:
        run_build_command([npm, "ci"], cwd=source)
        run_build_command([npm, "run", "build"], cwd=source)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            f"Could not build the ngn React UI in {source}. Fix the npm error above and rerun `ngn serve --dev`. "
            "Stale assets will not be served."
        ) from error
    if not current_build(directory, source):
        raise ValueError(f"The React build in {source} is incomplete or its inputs changed. Rerun `ngn serve --dev`.")
    return directory
