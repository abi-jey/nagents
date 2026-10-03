"""Small, dependency-free presentation of structured native command outcomes."""

from __future__ import annotations

MAX_SAFE_INTEGER = (1 << 53) - 1


def shell_outcome(name: str, result: object) -> str:
    """Return factual process status, without interpreting saved result text.

    This is display metadata, not a framework error or an agent-run outcome.
    Callers must give an explicit framework error precedence over this label.
    """
    if name != "shell" or not isinstance(result, dict):
        return ""
    if result.get("timed_out") is True:
        return "timed out"
    exit_code: object = result.get("exit_code")
    if isinstance(exit_code, float):
        if not exit_code.is_integer():
            return ""
        exit_code = int(exit_code)
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and 0 < abs(exit_code) <= MAX_SAFE_INTEGER:
        return f"exit {exit_code}"
    return ""
