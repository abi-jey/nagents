"""Opt-in source reload using Uvicorn's single-child process supervisor."""

import hashlib
import sys
from functools import partial
from pathlib import Path
from socket import socket

from fastapi import FastAPI
from uvicorn import Config
from uvicorn import Server
from uvicorn.supervisors.basereload import BaseReload

from nagents.harness.config import HarnessConfig

from .app import create_app
from .assets import _files
from .assets import prepare_assets
from .assets import source_hashes
from .runtime import server_config


def source_snapshot(source: Path) -> dict[Path, str]:
    snapshot = {source / name: digest for name, digest in source_hashes(source).items()}
    for path in _files(source.parent):
        if path.suffix == ".py":
            snapshot[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


class SourceReload(BaseReload):
    def __init__(self, config: Config, server: Server, listener: socket, source: Path) -> None:
        super().__init__(config, target=server.run, sockets=[listener])
        self.reloader_name = "ngn --dev"
        self.source = source
        self.previous = source_snapshot(source)

    def should_restart(self) -> list[Path]:
        self.pause()
        try:
            current = source_snapshot(self.source)
        except OSError:
            # Editors can briefly remove/replace directories during a save.
            return []
        changed = [
            path for path in self.previous.keys() | current.keys() if self.previous.get(path) != current.get(path)
        ]
        self.previous = current
        return sorted(changed)


def development_app(config: HarnessConfig, host: str, port: int, resume: str, continue_session: bool) -> FastAPI:
    assets = prepare_assets(Path(__file__).resolve().parent, dev=True)
    build_id = hashlib.sha256((assets / "build.json").read_bytes()).hexdigest()[:12]
    print(f"ngn: React UI build {build_id} from {assets}", file=sys.stderr, flush=True)
    return create_app(
        config, host=host, port=port, assets=assets, resume_session=resume, continue_session=continue_session
    )


def serve_dev(config: HarnessConfig, *, host: str, port: int, resume_session: str, continue_session: bool) -> None:
    web = Path(__file__).resolve().parent
    prepare_assets(web, dev=True)
    factory = partial(development_app, config, host, port, resume_session, continue_session)
    settings = server_config(factory, host=host, port=port, factory=True)
    listener = settings.bind_socket()
    try:
        SourceReload(settings, Server(settings), listener, web.parent / "web-ui").run()
    finally:
        listener.close()
