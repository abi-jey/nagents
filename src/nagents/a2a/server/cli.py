"""CLI host for a single explicit ngn profile, separate from the local web UI."""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .config import A2AConfig

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.config import HarnessConfig


def serve(
    harness_config: HarnessConfig,
    *,
    configuration: Path,
    host: str,
    port: int,
    tls_certfile: str = "",
    tls_keyfile: str = "",
    trusted_proxies: str = "127.0.0.1,::1",
    agent_override: str = "",
) -> None:
    try:
        import uvicorn

        from .application import create_harness_app
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] in {"a2a", "jwt", "starlette", "sse_starlette", "uvicorn"}:
            raise ValueError("Install the optional A2A integration: pip install 'nagents[a2a]'") from None
        raise
    config = A2AConfig.from_file(configuration)
    if agent_override and agent_override != config.agent:
        raise ValueError("--agent must match the exported profile in --a2a-config")
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if urlsplit(config.public_url).scheme == "http" and not local:
        raise ValueError("Development A2A HTTP must bind a loopback address")
    if bool(tls_certfile) != bool(tls_keyfile):
        raise ValueError("Supply both --tls-certfile and --tls-keyfile")
    if not 1 <= port <= 65535:
        raise ValueError("A2A port must be from 1 to 65535")
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_harness_app(config=config, harness_config=harness_config),
        host=host,
        port=port,
        workers=1,
        ssl_certfile=tls_certfile or None,
        ssl_keyfile=tls_keyfile or None,
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxies,
        # OAuth callback URLs contain codes. Our correlated audit deliberately
        # logs only operation classes, never request URLs, headers or bodies.
        access_log=False,
    )
