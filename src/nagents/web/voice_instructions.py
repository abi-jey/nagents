"""Packaged Markdown defaults for the editable Live instruction hierarchy."""

from functools import lru_cache
from importlib.resources import files


def default_voice_instructions_path(backend_mode: str) -> str:
    if backend_mode not in {"assistant", "hosted"}:
        raise ValueError("Unsupported voice backend mode")
    return (
        str(files("nagents.harness").joinpath("instructions", "assistant.md"))
        if backend_mode == "assistant"
        else str(files("nagents.web").joinpath("instructions", "hosted.md"))
    )


@lru_cache(maxsize=2)
def default_voice_instructions(backend_mode: str) -> str:
    if backend_mode not in {"assistant", "hosted"}:
        raise ValueError("Unsupported voice backend mode")
    if backend_mode == "assistant":
        from nagents.harness.agent_documents import bundled_assistant

        document, _ = bundled_assistant()
        live = document["live"]
        assert isinstance(live, dict) and isinstance(live["prompt"], str)
        return live["prompt"]
    return files("nagents.web").joinpath("instructions", f"{backend_mode}.md").read_text(encoding="utf-8").strip()
