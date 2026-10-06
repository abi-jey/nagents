"""Explicit model-facing descriptions retain their complete instruction text."""

from __future__ import annotations

import pytest

from nagents.adapters import anthropic
from nagents.adapters import gemini
from nagents.adapters import openai
from nagents.adapters import responses
from nagents.provider.openai import _request_body
from nagents.tools import ToolRegistry
from nagents.types import Message


def described(value: str = "") -> str:
    """Fallback summary with one
    hard line break.

    Additional documentation is not an implicit model description.

    Args:
        value: The value to echo.
    """
    return value


@pytest.mark.parametrize(
    "description",
    [
        "First paragraph.\n\nSecond paragraph with required behavior.\n\nThird paragraph.",
        "First line\nSecond line\r\nThird line",
        "  Markdown hard break:  \nnext line\n\n```json\n{}\n```\n",
        "   ",
    ],
)
def test_explicit_description_is_preserved_verbatim(description: str) -> None:
    registry = ToolRegistry()
    definition = registry.register(described, description=description)
    assert definition.description == description
    assert definition.parameters["properties"]["value"]["description"] == "The value to echo."


@pytest.mark.parametrize("description", [None, ""])
def test_empty_or_omitted_override_keeps_docstring_summary_compatibility(description: str | None) -> None:
    definition = ToolRegistry().register(described, description=description)
    assert [line.strip() for line in definition.description.splitlines()] == [
        "Fallback summary with one",
        "hard line break.",
    ]
    assert "Additional documentation" not in definition.description
    assert "Args:" not in definition.description


@pytest.mark.parametrize("description", [None, ""])
def test_missing_docstring_uses_tool_name_fallback(description: str | None) -> None:
    def undocumented(value: str) -> str:
        return value

    definition = ToolRegistry().register(undocumented, name="renamed", description=description)
    assert definition.description == "Call renamed"


def test_all_provider_wire_formats_preserve_explicit_description_and_schema() -> None:
    description = (
        "Use this tool for a bounded operation.\n\nOnly use the exact schema below.  \nDo not infer omitted arguments."
    )
    tool = ToolRegistry().register(described, description=description)
    native = _request_body("fixture", [Message(role="user", content="fixture")], [tool], None)
    generic = responses.format_request("fixture", [Message(role="user", content="fixture")], [tool], None, True)
    chat = openai.format_tools([tool])[0]["function"]
    claude = anthropic.format_tools([tool])[0]
    google = gemini.format_tools([tool])[0]
    assert chat["description"] == claude["description"] == google["description"] == description
    assert chat["parameters"] == claude["input_schema"] == google["parameters"] == tool.parameters
    for payload in (native, generic):
        tools = payload["tools"]
        assert isinstance(tools, list) and len(tools) == 1
        assert tools[0]["description"] == description
        assert tools[0]["parameters"] == tool.parameters
