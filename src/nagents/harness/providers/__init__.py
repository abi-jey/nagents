"""Harness provider connections, credentials, model selection, and factories."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import API_NAMES as API_NAMES
    from .registry import DEFAULT_ENDPOINTS as DEFAULT_ENDPOINTS
    from .registry import ENV as ENV
    from .registry import KINDS as KINDS
    from .registry import MAX_BYTES as MAX_BYTES
    from .registry import NAME as NAME
    from .registry import PROVIDERS as PROVIDERS
    from .registry import REVISION as REVISION
    from .registry import ProviderKind as ProviderKind
    from .registry import ProviderProfile as ProviderProfile
    from .registry import ProviderRegistry as ProviderRegistry
    from .registry import ProviderRegistryStore as ProviderRegistryStore
    from .registry import ScopedModelStore as ScopedModelStore
    from .registry import ScopedProviderRegistryStore as ScopedProviderRegistryStore
    from .registry import env_name as env_name
    from .registry import validate_provider as validate_provider
    from .registry import validate_request_timeout as validate_request_timeout

__all__ = [
    "API_NAMES",
    "DEFAULT_ENDPOINTS",
    "ENV",
    "KINDS",
    "MAX_BYTES",
    "NAME",
    "PROVIDERS",
    "REVISION",
    "ProviderKind",
    "ProviderProfile",
    "ProviderRegistry",
    "ProviderRegistryStore",
    "ScopedModelStore",
    "ScopedProviderRegistryStore",
    "env_name",
    "validate_provider",
    "validate_request_timeout",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".registry", __name__), name)
    globals()[name] = value
    return value
