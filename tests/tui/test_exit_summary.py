"""Exit styling preserves literal, copyable commands and plain redirected output."""

from io import StringIO

from rich.console import Console

from nagents.tui.exit_summary import print_exit_summary


def test_plain_summary_preserves_commands_and_paths() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=40)
    command = "ngn -C '/a long/workspace [x]' --resume session-123456789"
    print_exit_summary("2:39:11", "session-123456789", command, "/logs/[test].jsonl", opened=True, console=console)
    text = output.getvalue()
    assert command in text
    assert "/logs/[test].jsonl" in text
    assert "ngn closed" in text
    assert "\x1b" not in text


def test_styled_summary_and_unsaved_session() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=True, color_system="standard")
    print_exit_summary("1s", "", "", "/log", opened=True, console=console)
    text = output.getvalue()
    assert "\x1b[" in text
    assert "No session was saved." in text
    assert "not opened" in text
