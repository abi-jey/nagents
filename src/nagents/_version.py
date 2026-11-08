"""Resolve the running checkout or installed package version without UI imports."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from pathlib import Path


def framework_version() -> str:
    try:
        source = Path(__file__).resolve()
        root = source.parents[2]
        if source != root / "src" / "nagents" / "_version.py":
            raise FileNotFoundError("Not running from a source checkout")
        document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project: object = document.get("project")
        if isinstance(project, dict) and project.get("name") == "nagents":
            declared: object = project.get("version")
            if isinstance(declared, str) and declared:
                return declared
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        return version("nagents")
    except PackageNotFoundError:
        return "development"
