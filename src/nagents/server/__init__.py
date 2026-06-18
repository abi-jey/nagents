"""Agent server — FastAPI application with chat, tools, and attachments.

Requires the ``server`` extra. Install with::

    pip install nagents[server]
"""

try:
    from .app import app
    from .app import app as create_app
except ImportError as e:
    raise ImportError("nagents.server requires the 'server' extra. Install with: pip install nagents[server]") from e

__all__ = ["app", "create_app"]
