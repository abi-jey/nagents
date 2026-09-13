"""Frontend freshness and source/package startup regressions."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from nagents.web.assets import prepare_assets
from nagents.web.assets import source_hashes


def _stamp(web: Path) -> None:
    directory = web / "static"
    manifest = {
        "version": 1,
        "sources": source_hashes(web.parent / "web-ui"),
        "assets": {
            path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*")
            if path.is_file() and path.name != "build.json"
        },
    }
    (directory / "build.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def web(tmp_path: Path) -> Path:
    web = tmp_path / "installed" / "nagents" / "web"
    source = web.parent / "web-ui"
    (source / "src").mkdir(parents=True)
    (source / "src" / "App.tsx").write_text("old source", encoding="utf-8")
    (source / "package.json").write_text("{}", encoding="utf-8")
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    (web / "static" / "assets").mkdir(parents=True)
    (web / "static" / "index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
    (web / "static" / "assets" / "app.js").write_text("old build", encoding="utf-8")
    _stamp(web)
    return web


def test_fresh_source_build_needs_no_npm(web: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    npm = Mock(side_effect=AssertionError("Fresh builds must work offline without Node"))
    monkeypatch.setattr("nagents.web.assets.shutil.which", npm)
    assert prepare_assets(web) == web / "static"
    npm.assert_not_called()


@pytest.mark.parametrize(
    "change", ["edit", "add", "delete", "lockfile", "config", "script", "broken-asset", "missing-asset", "old-build"]
)
def test_stale_source_build_uses_canonical_npm_commands(
    web: Path, monkeypatch: pytest.MonkeyPatch, change: str, tmp_path: Path
) -> None:
    source = web.parent / "web-ui"
    app = source / "src" / "App.tsx"
    if change == "edit":
        stat = app.stat()
        app.write_text("new source", encoding="utf-8")
        os.utime(app, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif change == "add":
        (source / "src" / "new.css").write_text("body {}", encoding="utf-8")
    elif change == "delete":
        app.unlink()
    elif change == "lockfile":
        (source / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    elif change == "config":
        (source / "vite.config.ts").write_text("export default {}", encoding="utf-8")
    elif change == "script":
        (source / "scripts").mkdir()
        (source / "scripts" / "build.mjs").write_text("// new build command", encoding="utf-8")
    elif change == "broken-asset":
        (web / "static" / "assets" / "app.js").write_text("broken", encoding="utf-8")
    elif change == "missing-asset":
        (web / "static" / "assets" / "app.js").unlink()
    else:
        (web / "static" / "build.json").unlink()

    def rebuild(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        assert cwd == source
        assert check
        if command[1:] == ["run", "build"]:
            (web / "static" / "assets" / "app.js").write_text("new build", encoding="utf-8")
            _stamp(web)
        return subprocess.CompletedProcess(command, 0)

    run = Mock(side_effect=rebuild)
    monkeypatch.setattr("nagents.web.assets.subprocess.run", run)
    monkeypatch.setattr("nagents.web.assets.shutil.which", lambda _: "/tools/npm")
    # A different current directory/workspace must never choose a different frontend.
    monkeypatch.chdir(tmp_path)
    assert prepare_assets(web) == web / "static"
    assert [call.args[0] for call in run.call_args_list] == [["/tools/npm", "ci"], ["/tools/npm", "run", "build"]]
    assert prepare_assets(web) == web / "static"
    assert run.call_count == 2


def test_packaged_build_uses_bundle_without_npm_or_workspace_source(web: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = web.parent / "web-ui"
    workspace = web.parents[1] / "workspace"
    source.rename(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("nagents.web.assets.shutil.which", Mock(side_effect=AssertionError("No npm in a wheel")))
    assert prepare_assets(web) == web / "static"


@pytest.mark.parametrize("damage", ["missing", "invalid-json", "wrong-version", "invalid-map", "corrupt-assets"])
def test_broken_packaged_build_fails_without_downloading(
    web: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    (web.parent / "web-ui").rename(web.parent / "unrelated-source")
    manifest = web / "static" / "build.json"
    if damage == "missing":
        manifest.unlink()
    elif damage == "invalid-json":
        manifest.write_text("{", encoding="utf-8")
    elif damage == "wrong-version":
        manifest.write_text('{"version": 2}', encoding="utf-8")
    elif damage == "invalid-map":
        manifest.write_text('{"version": 1, "assets": {"index.html": 3}}', encoding="utf-8")
    else:
        (web / "static" / "assets" / "app.js").write_text("corrupt", encoding="utf-8")
    monkeypatch.setattr("nagents.web.assets.shutil.which", Mock(side_effect=AssertionError("No downloads")))
    with pytest.raises(ValueError, match="Reinstall a release"):
        prepare_assets(web)


def test_stale_source_without_node_has_actionable_error(web: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (web / "static" / "build.json").unlink()
    monkeypatch.setattr("nagents.web.assets.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match=r"Node.js 20.19.*ngn serve"):
        prepare_assets(web)


@pytest.mark.parametrize("failure", ["install", "build", "unverified-output"])
def test_failed_rebuild_never_falls_back_to_stale_assets(
    web: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    (web.parent / "web-ui" / "src" / "App.tsx").write_text("new source", encoding="utf-8")

    def fail(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        if (failure == "install" and command[1] == "ci") or (failure == "build" and command[1] == "run"):
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("nagents.web.assets.subprocess.run", fail)
    monkeypatch.setattr("nagents.web.assets.shutil.which", lambda _: "/tools/npm")
    with pytest.raises(ValueError, match=r"Could not build|incomplete"):
        prepare_assets(web)


def test_packaged_asset_inventory_includes_source_excluded_names(web: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = web / "static" / ".cache" / "example.tsbuildinfo"
    extra.parent.mkdir()
    extra.write_text("public asset", encoding="utf-8")
    _stamp(web)
    (web.parent / "web-ui").rename(web.parent / "unrelated-source")
    monkeypatch.setattr("nagents.web.assets.shutil.which", Mock(side_effect=AssertionError("No npm in a wheel")))
    assert prepare_assets(web) == web / "static"
    extra.write_text("changed asset", encoding="utf-8")
    with pytest.raises(ValueError, match="Reinstall a release"):
        prepare_assets(web)
