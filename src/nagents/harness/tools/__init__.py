"""Harness workspace tools, execution binding, and resource lifecycle."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coding import MODIFYING_TOOLS as MODIFYING_TOOLS
    from .coding import READ_ONLY_TOOLS as READ_ONLY_TOOLS
    from .coding import CodingTools as CodingTools
    from .coding import HarnessExecutor as HarnessExecutor

__all__ = ["MODIFYING_TOOLS", "READ_ONLY_TOOLS", "CodingTools", "HarnessExecutor"]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".coding", __name__), name)
    globals()[name] = value
    return value
