"""Read the shipped coding policy independently of workspace instruction discovery."""

from importlib import resources


def load_default_instructions() -> str:
    """Use the Markdown resource as the sole source of the default coding policy."""
    try:
        content = resources.files("nagents.harness").joinpath("instructions", "AGENTS.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            "Cannot read the bundled ngn instructions (nagents/harness/instructions/AGENTS.md). "
            "Reinstall nagents or restore this UTF-8 resource in your source checkout."
        ) from exc
    if not content.strip():
        raise RuntimeError(
            "The bundled ngn instructions (nagents/harness/instructions/AGENTS.md) are empty. "
            "Reinstall nagents or restore this resource in your source checkout."
        )
    return content.strip()
