"""Packaged Markdown defaults for the editable Live instruction hierarchy."""

from functools import lru_cache
from importlib.resources import files


def default_voice_instructions_path(backend_mode: str) -> str:
    if backend_mode not in {"assistant", "hosted"}:
        raise ValueError("Unsupported voice backend mode")
    return str(files("nagents.web").joinpath("instructions", f"{backend_mode}.md"))


@lru_cache(maxsize=2)
def default_voice_instructions(backend_mode: str) -> str:
    if backend_mode not in {"assistant", "hosted"}:
        raise ValueError("Unsupported voice backend mode")
    return files("nagents.web").joinpath("instructions", f"{backend_mode}.md").read_text(encoding="utf-8").strip()


def legacy_voice_instructions(text: str, backend_mode: str) -> str:
    """Preserve the effective prompt of existing additional-guidance settings."""
    base = default_voice_instructions(backend_mode)
    custom = text.strip()
    return base + ("\n\nAdditional voice instructions configured by the user:\n" + custom if custom else "")
