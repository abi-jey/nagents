"""Optional authenticated Agent2Agent integration for native nagents Agents.

Install ``nagents[a2a]`` to serve protocol 1.0 or use its SDK client. Importing
``nagents`` or this package does not import the optional A2A SDK or start ngn.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import AgentContext as AgentContext
    from .agent import AgentFactory as AgentFactory
    from .application import create_app as create_app
    from .application import create_harness_app as create_harness_app
    from .client import RemoteAgent as RemoteAgent
    from .config import A2AConfig as A2AConfig
    from .config import IssuerConfig as IssuerConfig
    from .config import ServiceConfig as ServiceConfig
    from .conversation import RemoteConversation as RemoteConversation
    from .conversation import RemoteUpdate as RemoteUpdate
    from .discovery import RemoteDiscovery as RemoteDiscovery
    from .discovery import discover_remote as discover_remote
    from .errors import RemoteAgentError as RemoteAgentError
    from .executor import request_input as request_input
    from .images import publish_image as publish_image
    from .images import publish_image_file as publish_image_file
    from .oauth import AccessGrant as AccessGrant
    from .oauth import request_credentials as request_credentials
    from .remote_config import RemoteAgentConfig as RemoteAgentConfig

__all__ = [
    "A2AConfig",
    "AccessGrant",
    "AgentContext",
    "AgentFactory",
    "IssuerConfig",
    "RemoteAgent",
    "RemoteAgentConfig",
    "RemoteAgentError",
    "RemoteConversation",
    "RemoteDiscovery",
    "RemoteUpdate",
    "ServiceConfig",
    "create_app",
    "create_harness_app",
    "discover_remote",
    "publish_image",
    "publish_image_file",
    "request_credentials",
    "request_input",
]
_MODULES = {
    "RemoteDiscovery": "discovery",
    "discover_remote": "discovery",
    "RemoteAgentConfig": "remote_config",
    "RemoteConversation": "conversation",
    "RemoteUpdate": "conversation",
    "RemoteAgent": "client",
    "RemoteAgentError": "errors",
    "A2AConfig": "config",
    "IssuerConfig": "config",
    "ServiceConfig": "config",
    "AgentContext": "agent",
    "AgentFactory": "agent",
    "create_app": "application",
    "create_harness_app": "application",
    "request_input": "executor",
    "publish_image": "images",
    "publish_image_file": "images",
    "AccessGrant": "oauth",
    "request_credentials": "oauth",
}


def __getattr__(name: str) -> object:
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(name)
    try:
        value: object = getattr(import_module(f"{__name__}.{module}"), name)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] in {"a2a", "jwt", "starlette", "sse_starlette", "uvicorn"}:
            raise ModuleNotFoundError("Install the optional A2A integration: pip install 'nagents[a2a]'") from None
        raise
    globals()[name] = value
    return value
