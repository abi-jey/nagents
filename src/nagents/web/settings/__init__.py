"""Web agent, model, provider, tool, and workspace preferences.

Public preference types remain importable here without eagerly loading routes.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .preferences import DEFAULT_COMPACT_MESSAGES as DEFAULT_COMPACT_MESSAGES
    from .preferences import DEFAULT_COMPACT_TOKENS as DEFAULT_COMPACT_TOKENS
    from .preferences import MAX_API_KEY_BYTES as MAX_API_KEY_BYTES
    from .preferences import MAX_SETTINGS_BYTES as MAX_SETTINGS_BYTES
    from .preferences import SettingsInput as SettingsInput
    from .preferences import SettingsModelReference as SettingsModelReference
    from .preferences import SettingsRevision as SettingsRevision
    from .preferences import SettingsValues as SettingsValues
    from .preferences import WebSettings as WebSettings

__all__ = [
    "DEFAULT_COMPACT_MESSAGES",
    "DEFAULT_COMPACT_TOKENS",
    "MAX_API_KEY_BYTES",
    "MAX_SETTINGS_BYTES",
    "SettingsInput",
    "SettingsModelReference",
    "SettingsRevision",
    "SettingsValues",
    "WebSettings",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".preferences", __name__), name)
    globals()[name] = value
    return value
