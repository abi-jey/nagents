"""Payload-free remote failure codes, importable without the optional SDK."""


class RemoteAgentError(RuntimeError):
    """A remote operation failed; ``code`` identifies its safe failure category."""

    def __init__(self, message: str, *, code: str = "A2A_PROTOCOL") -> None:
        super().__init__(message)
        self.code = code
