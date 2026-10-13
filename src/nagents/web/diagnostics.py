"""Bounded exception identities for logs, without exception text or payloads."""

from __future__ import annotations


def exception_diagnostic(error: BaseException) -> tuple[str, int | str, str]:
    """Inspect at most eight exceptions, including the outer exception."""
    pending = [error]
    details: list[tuple[str, int | str]] = []
    for current in pending:
        try:
            name = type(current).__name__
        except BaseException:
            name = "Exception"
        name = name[:80] if type(name) is str else "Exception"
        if not name.isascii() or not name.isidentifier():
            name = "Exception"
        try:
            code = getattr(current, "sqlite_errorcode", None)
        except BaseException:
            code = None
        details.append((name, code if type(code) is int and 0 <= code <= 2**31 - 1 else "unavailable"))
        if issubclass(type(current), BaseExceptionGroup) and len(pending) < 8:
            try:
                children = getattr(current, "exceptions", ())
            except BaseException:
                continue
            # Do not invoke a custom collection/iterator supplied by a property.
            if type(children) is tuple:
                pending.extend(
                    child for child in children[: 8 - len(pending)] if issubclass(type(child), BaseException)
                )
    name, code = details[0]
    nested = ",".join(f"{name}:{code}" for name, code in details[1:]) or "none"
    return name, code, nested
