"""Skill names remain visible in collapsed tool cards."""

import pytest

from nagents.tui.widgets import ToolCard


@pytest.mark.parametrize("restored", [False, True])
def test_skill_tool_summary_includes_name(restored: bool) -> None:
    card = ToolCard("skill-call", "skill", {"name": "ngn-customize"}, restored=restored)
    assert card._hint == "ngn-customize"
    card.set_arguments({"name": "shared-browser"})
    assert card._hint == "shared-browser"


@pytest.mark.parametrize("arguments", [{}, {"name": 123}, {"name": "  "}, None])
def test_skill_tool_summary_ignores_missing_or_invalid_names(arguments: object) -> None:
    assert ToolCard("skill-call", "skill", arguments)._hint == ""


def test_tool_summary_does_not_expose_arbitrary_name_arguments() -> None:
    assert ToolCard("custom-call", "custom", {"name": "private"})._hint == ""
    assert ToolCard("skill-call", "skill", {"name": "ngn-\ncustomize\x00"})._hint == "ngn- customize"
