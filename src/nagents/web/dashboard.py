"""Read-only dashboard summary served by the web client."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def dashboard_summary(workspace: Path) -> dict[str, str]:
    """Describe the shell honestly until usage instrumentation is connected."""
    return {"workspace": str(workspace), "status": "not_connected"}
