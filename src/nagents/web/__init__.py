"""Optional, local-only web client for the ngn Harness, not nagents.server."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig


def local_authority(host: str, port: int) -> str:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "ngn serve only accepts loopback hosts: 127.0.0.1, ::1, or localhost. Remote access is unsupported."
        )
    if not 1 <= port <= 65535:
        raise ValueError("ngn serve --port must be between 1 and 65535.")
    return f"{'[' + host + ']' if ':' in host else host}{':' + str(port) if port != 80 else ''}"


def built_assets() -> Path:
    directory = Path(__file__).parent / "static"
    if not (directory / "index.html").is_file() or not (directory / "assets").is_dir():
        raise ValueError(
            "The ngn React assets are missing. In a source checkout run `npm --prefix src/nagents/web-ui ci` then "
            "`npm --prefix src/nagents/web-ui run build`. For a packaged install, reinstall a release that includes web assets. "
            "ngn serve never downloads or builds at startup."
        )
    return directory


def serve(
    config: "HarnessConfig",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    resume_session: str = "",
    continue_session: bool = False,
) -> None:
    local_authority(host, port)
    try:
        import uvicorn

        from .app import create_app
    except ModuleNotFoundError as error:
        if error.name not in {"fastapi", "starlette", "pydantic", "uvicorn", "anyio"}:
            raise
        raise ValueError(
            "The web server dependencies are not installed. Run `pip install 'nagents[web]'` or, "
            "in this source checkout's virtualenv, `pip install -e '.[web]'` (or `poetry install -E web`). "
            "The existing server extra also supplies these dependencies. Textual is not required."
        ) from error
    assets = built_assets()
    app = create_app(
        config,
        host=host,
        port=port,
        assets=assets,
        resume_session=resume_session,
        continue_session=continue_session,
    )
    # One process and one lifespan own every Harness resource. Never trust proxy headers.
    uvicorn.run(app, host=host, port=port, proxy_headers=False, access_log=False, timeout_graceful_shutdown=3)
