"""Expose a native framework Agent beside the machine it can inspect."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
from pathlib import Path

import uvicorn

from nagents import Agent
from nagents import OpenAIProvider
from nagents import SessionManager
from nagents.a2a import A2AConfig
from nagents.a2a import AgentContext
from nagents.a2a import create_app


def machine_status() -> str:
    """Read OS and root-filesystem capacity on this server, without credentials."""
    usage = shutil.disk_usage("/")
    return json.dumps(
        {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "disk_total_bytes": usage.total,
            "disk_free_bytes": usage.free,
        }
    )


def factory(context: AgentContext) -> Agent:
    # This is an ordinary framework Agent. No HarnessConfig or ngn web server
    # is involved. The host selects its tools/provider; A2A metadata cannot.
    return Agent(
        provider=OpenAIProvider(model=os.environ.get("NGN_A2A_MODEL", "gpt-6.1-sol")),
        session_manager=SessionManager(context.data_dir / "agent-sessions.db"),
        system_prompt=(
            "You inspect the machine where this agent runs. Use machine_status for current facts. "
            "Describe only information the tool actually returned."
        ),
        tools=[machine_status],
        streaming=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--trusted-proxies", default="127.0.0.1,::1")
    args = parser.parse_args()
    config = A2AConfig.from_file(args.config)
    app = create_app(config=config, agent_factory=factory, read_only_tools=frozenset({"machine_status"}))
    # A TLS reverse proxy must serve config.public_url. For development use an
    # explicit loopback_http config and keep this listener on loopback.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=args.trusted_proxies,
    )


if __name__ == "__main__":
    main()
