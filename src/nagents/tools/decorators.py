"""Explicit function exports for discoverable Python tool modules."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import ParamSpec
from typing import TypeVar
from typing import overload

if TYPE_CHECKING:
    from collections.abc import Callable

_P = ParamSpec("_P")
_R = TypeVar("_R")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class ToolOptions:
    name: str = ""
    description: str = ""


def tool_options(function: object) -> ToolOptions | None:
    """Return explicit export metadata without treating ordinary helpers as tools."""
    options = getattr(function, "__ngn_tool__", None)
    return options if isinstance(options, ToolOptions) else None


@overload
def tool(function: Callable[_P, _R], /) -> Callable[_P, _R]: ...


@overload
def tool(*, name: str = "", description: str = "") -> Callable[[Callable[_P, _R]], Callable[_P, _R]]: ...


def tool(
    function: Callable[_P, _R] | None = None, /, *, name: str = "", description: str = ""
) -> Callable[_P, _R] | Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Mark a sync/async function as a tool, preserving its signature and identity.

    In ngn, decorated functions in `.agents/tools/weather.py` are discovered as
    `weather_<function>` (or `weather_<name>` with an explicit name). The same
    metadata applies when directly registering a function with ToolRegistry.
    """
    if not isinstance(name, str) or (name and not _NAME.fullmatch(name)):
        raise ValueError("Tool name must be an ASCII identifier of at most 64 characters (hyphens are allowed)")
    if not isinstance(description, str):
        raise TypeError("Tool description must be text")

    def decorate(target: Callable[_P, _R]) -> Callable[_P, _R]:
        if not inspect.isfunction(target):
            raise TypeError("@tool requires a Python function")
        if inspect.isgeneratorfunction(target) or inspect.isasyncgenfunction(target):
            raise TypeError("@tool functions must return a result; generators are unsupported")
        target.__dict__["__ngn_tool__"] = ToolOptions(name, description)
        return target

    return decorate if function is None else decorate(function)
