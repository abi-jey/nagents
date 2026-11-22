"""Report whether the bundled core has ChatGPT device sign-in (checked by `ngn update`)."""

from __future__ import annotations

import importlib

_METHODS = ("credentials", "start_device_login", "complete_device_login", "status", "logout_async", "close")


def available() -> bool:
    try:
        module = importlib.import_module("nagents.provider.device_auth")
    except ImportError:
        return False
    factory = getattr(module, "OpenAIAuth", None)
    return (
        isinstance(factory, type)
        and isinstance(getattr(module, "DeviceAuthorization", None), type)
        and isinstance(getattr(module, "OpenAIAuthError", None), type)
        and all(callable(getattr(factory, name, None)) for name in _METHODS)
    )
