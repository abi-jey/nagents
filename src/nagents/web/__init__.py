"""Optional, local-only web client for the ngn Harness, not nagents.server."""

import hashlib
import ipaddress
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .assets import prepare_assets

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _is_bindable(host: str) -> bool:
    if is_loopback_host(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def local_authority(host: str, port: int) -> str:
    # Loopback stays the safe default; a literal IP (for example 0.0.0.0 or a
    # specific interface address) opts into binding beyond loopback. Hostnames
    # stay rejected so a suffix cannot masquerade as a trusted name.
    if not _is_bindable(host):
        raise ValueError(
            "ngn serve --host accepts localhost, 127.0.0.1, ::1, or an explicit IP address "
            "(for example 0.0.0.0 or a specific interface address); other hostnames are not accepted."
        )
    if not 1 <= port <= 65535:
        raise ValueError("ngn serve --port must be between 1 and 65535.")
    return f"{'[' + host + ']' if ':' in host else host}{':' + str(port) if port != 80 else ''}"


def built_assets() -> Path:
    return prepare_assets(Path(__file__).resolve().parent)


def serve(
    config: "HarnessConfig",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    resume_session: str = "",
    continue_session: bool = False,
    dev: bool = False,
) -> None:
    local_authority(host, port)
    try:
        import uvicorn

        from .app import create_app
        from .runtime import server_config
    except ModuleNotFoundError as error:
        if error.name not in {"fastapi", "starlette", "pydantic", "uvicorn", "anyio"}:
            raise
        raise ValueError(
            "The web server dependencies are not installed. From the repository root, in this source checkout's "
            "virtualenv, run `pip install -e '.[web]'` (or `poetry install -E web`). "
            "The existing server extra also supplies these dependencies. Textual is not required."
        ) from error
    if dev:
        from .dev import serve_dev

        serve_dev(config, host=host, port=port, resume_session=resume_session, continue_session=continue_session)
        return
    assets = built_assets()
    build_id = hashlib.sha256((assets / "build.json").read_bytes()).hexdigest()[:12]
    print(f"ngn: React UI build {build_id} from {assets}", file=sys.stderr, flush=True)
    app = create_app(
        config,
        host=host,
        port=port,
        assets=assets,
        resume_session=resume_session,
        continue_session=continue_session,
    )
    uvicorn.Server(server_config(app, host=host, port=port)).run()
