"""Read the shipped coding policy independently of workspace instruction discovery."""


def load_default_instructions() -> str:
    """Use the Markdown resource as the sole source of the default coding policy."""
    from .agent_documents import bundled_assistant

    try:
        document, _ = bundled_assistant()
        content = document["prompt"]
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        raise RuntimeError(
            "Cannot read the bundled ngn instructions (nagents/harness/instructions/assistant.md). "
            "Reinstall nagents or restore this UTF-8 resource in your source checkout."
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            "The bundled ngn instructions (nagents/harness/instructions/assistant.md) are empty. "
            "Reinstall nagents or restore this resource in your source checkout."
        )
    return content.strip()
