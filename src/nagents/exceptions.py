"""
Custom exceptions for nagents.
"""


class NagentsError(Exception):
    """Base exception for all nagents errors."""

    pass


class ModelListError(NagentsError):
    """Model discovery failed. Messages exclude credentials and upstream data."""


class ToolHallucinationError(NagentsError):
    """
    Raised when the LLM tries to call a tool that doesn't exist.

    This exception is only raised when `fail_on_invalid_tool=True` is set
    on the Agent. Otherwise, the error is passed back to the LLM so it
    can recover gracefully.

    Attributes:
        tool_name: The name of the non-existent tool the LLM tried to call.
        available_tools: List of tools that are actually registered.
        message: The full error message.
    """

    def __init__(self, tool_name: str, available_tools: list[str], message: str):
        self.tool_name = tool_name
        self.available_tools = available_tools
        self.message = message
        super().__init__(message)
