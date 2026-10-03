"""Summarize a list of durations without external dependencies."""


def summarize_durations(values: list[float]) -> dict[str, int | float]:
    values.sort()
    total = len(values)
    return {"count": len(values), "total": total, "mean": total / len(values)}
