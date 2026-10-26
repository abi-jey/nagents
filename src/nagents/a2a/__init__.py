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
    from .client.configuration import RemoteAgentConfig as RemoteAgentConfig
    from .client.conversation import RemoteConversation as RemoteConversation
    from .client.conversation import RemoteUpdate as RemoteUpdate
    from .client.discovery import RemoteDiscovery as RemoteDiscovery
    from .client.discovery import discover_remote as discover_remote
    from .client.transport import RemoteAgent as RemoteAgent
    from .errors import RemoteAgentError as RemoteAgentError
    from .executor import request_input as request_input
    from .images import publish_image as publish_image
    from .images import publish_image_file as publish_image_file
    from .server.application import create_app as create_app
    from .server.application import create_harness_app as create_harness_app
    from .server.config import A2AConfig as A2AConfig
    from .server.config import IssuerConfig as IssuerConfig
    from .server.config import ServiceConfig as ServiceConfig
    from .server.oauth import AccessGrant as AccessGrant
    from .server.oauth import request_credentials as request_credentials

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
    "RemoteDiscovery": "client.discovery",
    "discover_remote": "client.discovery",
    "RemoteAgentConfig": "client.configuration",
    "RemoteConversation": "client.conversation",
    "RemoteUpdate": "client.conversation",
    "RemoteAgent": "client.transport",
    "RemoteAgentError": "errors",
    "A2AConfig": "server.config",
    "IssuerConfig": "server.config",
    "ServiceConfig": "server.config",
    "AgentContext": "agent",
    "AgentFactory": "agent",
    "create_app": "server.application",
    "create_harness_app": "server.application",
    "request_input": "executor",
    "publish_image": "images",
    "publish_image_file": "images",
    "AccessGrant": "server.oauth",
    "request_credentials": "server.oauth",
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
