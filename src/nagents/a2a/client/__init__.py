"""A2A discovery, configuration, task conversations, and client transport."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import RemoteAgent as RemoteAgent
    from .transport import ResourceBearer as ResourceBearer
    from .transport import remote_failure_code as remote_failure_code

__all__ = ["RemoteAgent", "ResourceBearer", "remote_failure_code"]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".transport", __name__), name)
    globals()[name] = value
    return value
