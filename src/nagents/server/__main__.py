"""Entry point for ``python -m nagents.server``."""

import os

import uvicorn

from .app import SERVER_TOKEN
from .app import app
from .security import is_loopback_host


def main() -> None:
    """Bind locally unless explicitly configured for authenticated network access."""
    host = os.getenv("NAGENTS_SERVER_HOST", "127.0.0.1")
    if not SERVER_TOKEN and not is_loopback_host(host):
        raise ValueError("NAGENTS_SERVER_TOKEN is required for a non-loopback NAGENTS_SERVER_HOST")
    port = int(os.getenv("PORT", "8080"))
    # Do not let untrusted X-Forwarded-For headers turn remote peers into local ones.
    uvicorn.run(app, host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
