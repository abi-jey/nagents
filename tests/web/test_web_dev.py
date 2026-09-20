"""Opt-in development reload boundaries and change detection."""

import os
from pathlib import Path
from socket import socket
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from uvicorn import Server

from nagents.cli import _parser
from nagents.cli import main
from nagents.harness.config import HarnessConfig
from nagents.web import serve
from nagents.web.dev import SourceReload
from nagents.web.runtime import server_config


@pytest.mark.parametrize("dev", [False, True])
def test_cli_development_mode_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dev: bool) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    args = ["serve", "--demo", "-C", str(tmp_path)] + (["--dev"] if dev else [])
    assert _parser().parse_args(args).dev is dev
    with patch("nagents.web.serve") as start:
        assert main(args) == 0
        assert start.call_args.kwargs["dev"] is dev


def test_dev_launch_selects_supervisor_before_creating_application(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path, demo=True)
    with (
        patch("nagents.web.dev.serve_dev") as start,
        patch("nagents.web.built_assets", side_effect=AssertionError("Wrong launch path")),
        patch("nagents.web.app.create_app", side_effect=AssertionError("No Harness in the supervisor")),
    ):
        serve(config, dev=True, host="localhost", port=9876, resume_session="saved")
    start.assert_called_once_with(config, host="localhost", port=9876, resume_session="saved", continue_session=False)


@pytest.mark.parametrize("kind", ["edit", "add", "delete", "python", "lockfile"])
def test_reload_detects_source_changes_without_mtime_dependency(tmp_path: Path, kind: str) -> None:
    package = tmp_path / "installed" / "nagents"
    source = package / "web-ui"
    (source / "src").mkdir(parents=True)
    app = source / "src" / "App.tsx"
    app.write_text("before", encoding="utf-8")
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    python = package / "agent.py"
    python.write_text("# before", encoding="utf-8")
    settings = server_config(FastAPI(), host="127.0.0.1", port=8765)
    settings.reload_delay = 0
    with socket() as listener:
        reloader = SourceReload(settings, Server(settings), listener, source)
        assert not reloader.should_restart()
        changed = app
        if kind == "edit":
            stat = app.stat()
            app.write_text("after!", encoding="utf-8")
            os.utime(app, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        elif kind == "add":
            changed = source / "src" / "new.css"
            changed.write_text("body {}", encoding="utf-8")
        elif kind == "delete":
            app.unlink()
        elif kind == "python":
            changed = python
            python.write_text("# changed", encoding="utf-8")
        else:
            changed = source / "package-lock.json"
            changed.write_text('{"changed":true}', encoding="utf-8")
        assert reloader.should_restart() == [changed]
        assert not reloader.should_restart()


def test_reload_ignores_generated_assets_dependencies_and_workspace(tmp_path: Path) -> None:
    package = tmp_path / "installed" / "nagents"
    source = package / "web-ui"
    source.mkdir(parents=True)
    (source / "package.json").write_text("{}", encoding="utf-8")
    settings = server_config(FastAPI(), host="127.0.0.1", port=8765)
    settings.reload_delay = 0
    with socket() as listener:
        reloader = SourceReload(settings, Server(settings), listener, source)
        for path in (
            package / "web" / "static" / "assets" / "app.js",
            source / "node_modules" / "dependency.py",
            source / ".test-build" / "test.js",
            package / "__pycache__" / "module.pyc",
            tmp_path / "workspace" / "agent.py",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated", encoding="utf-8")
        assert not reloader.should_restart()
        reloader.should_exit.set()
        with pytest.raises(StopIteration):
            reloader.should_restart()


def test_dev_and_normal_servers_share_security_and_shutdown_settings() -> None:
    app = FastAPI()
    normal = server_config(app, host="127.0.0.1", port=8765)
    development = server_config(Mock(return_value=app), host="127.0.0.1", port=8765, factory=True)
    for config in (normal, development):
        assert config.proxy_headers is False
        assert config.access_log is False
        assert config.timeout_graceful_shutdown == 3
        assert config.ws_per_message_deflate is False
        assert config.ws_max_queue == 16
    assert normal.factory is False and development.factory is True
