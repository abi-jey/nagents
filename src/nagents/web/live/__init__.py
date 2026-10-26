"""Web voice routes, relay, settings, and inspection.

The framework's provider-independent Live API remains in ``nagents.live``.
Route helpers retain their public imports without eagerly loading the web host
when a caller needs only an audio buffer or diagnostic helper.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .routes import Cursor as Cursor
    from .routes import SessionId as SessionId
    from .routes import SessionInput as SessionInput
    from .routes import capabilities as capabilities
    from .routes import create_agent as create_agent
    from .routes import create_login_config as create_login_config
    from .routes import register as register
    from .routes import unavailable_reason as unavailable_reason
    from .routes import voice_instructions as voice_instructions

__all__ = [
    "Cursor",
    "SessionId",
    "SessionInput",
    "capabilities",
    "create_agent",
    "create_login_config",
    "register",
    "unavailable_reason",
    "voice_instructions",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".routes", __name__), name)
    globals()[name] = value
    return value
