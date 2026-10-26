"""Harness runtime and conversation lifecycle."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Harness as Harness

__all__ = ["Harness"]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".core", __name__), name)
    globals()[name] = value
    return value
