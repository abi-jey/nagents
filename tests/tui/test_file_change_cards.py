"""Native file changes expose a path and diff rather than JSON envelopes."""

from __future__ import annotations

import pytest
from textual.content import Content
from textual.style import Style

from nagents.tui.widgets import ToolCard


@pytest.mark.parametrize("tool", ["edit", "write"])
def test_file_change_success_is_path_and_diff(tool: str) -> None:
    card = ToolCard("change", tool, {"path": "before.py", "old": "old", "new": "new"})
    assert not card.arguments.display
    assert card.file_path.display
    card.set_arguments({"path": "actual.py", "old": "old", "new": "new"})
    assert str(card.file_path.content) == "actual.py"
    card.finish(
        {"path": "actual.py", "changed": True, "diff": "--- a/actual.py\n+++ b/actual.py\n-old\n+new\n"}, None, 1
    )
    assert card.diff.display
    assert not card.output.display
    assert str(card.file_path.content) == "actual.py"


def test_errors_unknown_metadata_and_other_tools_remain_visible() -> None:
    card = ToolCard("error", "edit", {"path": "example.py"})
    card.finish(None, "Replacement was not unique", 1)
    assert card.output.display
    assert "Replacement was not unique" in card.output_text
    extra = ToolCard("extra", "edit", {"path": "example.py"})
    extra.finish({"path": "example.py", "changed": True, "diff": "+x", "warning": "Check this"}, None, 1)
    assert extra.output.display
    assert "Check this" in extra.output_text
    generic = ToolCard("custom", "custom", {"path": "example.py"})
    generic.finish({"changed": True}, None, 1)
    assert generic.arguments.display
    assert generic.output.display
    fallback = ToolCard("fallback", "edit", {"path": "example.py"})
    fallback.finish({"path": "example.py", "changed": False}, None, 1)
    assert fallback.output.display
    assert '"changed": false' in fallback.output_text


def test_unmounted_restored_output_retains_file_links() -> None:
    card = ToolCard("restored", "read_file", {"path": "example.py"}, restored=True)
    uri = "file:///workspace/example.py"
    card.finish(f"Source: {uri}", None, 1)
    content = card.output.content
    assert isinstance(content, Content)
    assert str(content) == f"Source: {uri}"
    assert any(
        isinstance(span.style, Style) and span.style.link == uri and str(content)[span.start : span.end] == uri
        for span in content.spans
    )
    assert card._elapsed_timer is None
    assert not card._animations
