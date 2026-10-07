"""Harder fictional tool-contract scenarios with outcome-based edit grading.

Generated commands are never executed. Shell feasibility/sampling cases require
human review; their structural scores must not be reported as task success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nagents.types import ToolCall

    from .run import Case


EDIT_FIXTURES = {
    "challenge_numbered_crlf": (
        "[server]\r\nhost=localhost\r\nport=8080\r\n",
        "[server]\r\nhost=127.0.0.1\r\nport=9090\r\n",
    ),
    "challenge_duplicate_edit": (
        "[staging]\nenabled=false\n\n[production]\nenabled=false\n",
        "[staging]\nenabled=false\n\n[production]\nenabled=true\n",
    ),
    "challenge_replace_existing": ("mode=debug\nretry=2\n", "mode=release\nretry=4\n"),
    "challenge_delete_exact_line": (
        "allow=read\r\nallow=write\r\nallow=list\r\n",
        "allow=read\r\nallow=list\r\n",
    ),
}


def get_cases(case_type: type[Case]) -> tuple[Case, ...]:
    """Keep the benchmark implementation's Case class out of runtime imports."""
    return (
        case_type(
            "challenge_line_window",
            "The traceback points to line 407 of src/worker.py. Show that line plus exactly six lines before it "
            "and four after it. The file is 30 KB and has 900 lines.",
            "read_file",
            {"path": "src/worker.py", "start_line": 401, "limit": 11},
        ),
        case_type(
            "challenge_numbered_crlf",
            'The latest file observation for server.ini is {"content":"1: [server]\\n2: host=localhost\\n3: port=8080",'
            '"newline_style":"CRLF","newline_counts":{"LF":0,"CRLF":3,"CR":0},"total_lines":3,'
            '"truncated":false,"sha256":"previously-recorded-snapshot"}. It has not changed. '
            "Change the host to 127.0.0.1 and port to 9090 together, preserving the file's line endings and all other text.",
            "edit",
            {"path": "server.ini"},
        ),
        case_type(
            "challenge_duplicate_edit",
            "You just read deploy.ini; its complete LF-terminated text is "
            "'[staging]\\nenabled=false\\n\\n[production]\\nenabled=false\\n'. "
            "It has not changed. Enable production only; staging must remain disabled.",
            "edit",
            {"path": "deploy.ini"},
        ),
        case_type(
            "challenge_replace_existing",
            "Overwrite existing settings.txt with exactly 'mode=release\\nretry=4\\n'. "
            "Its entire current text, already read and unchanged, is 'mode=debug\\nretry=2\\n'. "
            "Use a file tool for the change.",
            "edit",
            {"path": "settings.txt"},
        ),
        case_type(
            "challenge_delete_exact_line",
            'The complete latest observation for access.cfg is {"content":"1: allow=read\\n2: allow=write\\n3: allow=list",'
            '"newline_style":"CRLF","newline_counts":{"LF":0,"CRLF":3,"CR":0},"total_lines":3,'
            '"truncated":false}. No changes occurred since that read. '
            "Remove the entire allow=write line, including its line terminator; leave no blank line and preserve the others.",
            "edit",
            {"path": "access.cfg"},
        ),
        case_type(
            "challenge_read_large_window",
            "I need lines 850 through 2100 inclusive of generated/map.txt, a 70 KB file. "
            "Take the largest valid first contiguous chunk starting at 850; we'll read the remainder afterward.",
            "read_file",
            {"path": "generated/map.txt", "start_line": 850, "limit": 1000},
            True,
        ),
        case_type(
            "challenge_literal_filename_reference",
            "Locate up to five lines in src/ that mention the exact filename migrations[old].sql. "
            "I need references inside source text, including the square brackets, not a list of matching filenames.",
            "search",
            {"query": "migrations[old].sql", "path": "src", "limit": 5},
        ),
        case_type(
            "challenge_shell_milliseconds",
            "Run exactly python -m pytest tests/test_parser.py with a wall-clock limit of 1250 milliseconds.",
            "shell",
            {"command": "python -m pytest tests/test_parser.py", "timeout": 1.25},
        ),
        case_type(
            "challenge_delegate_context",
            "Please ask a background reviewer to compare src/old_parser.py against src/new_parser.py for changes "
            "in handling blank input and return findings without edits. Those are the two files meant by 'old' and 'new'; "
            "include enough detail that the reviewer can work independently.",
            "delegate",
        ),
        case_type(
            "challenge_oversized_sample",
            "The UTF-8 logs/events.log file is 20 MB, with ordinary short lines. I need only the first 20 lines. "
            "The file-reading tool rejected it as over its whole-file byte limit. Take a useful next action; "
            "a bounded read-only shell sample is authorized if necessary. Do not change the file or its limits.",
            "shell",
        ),
        case_type(
            "challenge_impossible_shell_deadline",
            "Execute exactly python -m integration_tests, but only if the available tool can let it run "
            "uninterrupted for up to 90 seconds. The trusted maximum is 60 seconds and cannot be changed. "
            "Do not split, background, detach, or start a command that the tool may kill before 90 seconds.",
            "",
        ),
    )


def is_manual(case: Case) -> bool:
    return case.name in {"challenge_oversized_sample", "challenge_impossible_shell_deadline"}


def grade_challenge(case: Case, call: ToolCall) -> list[str]:
    """Grade canonical arguments; caller checks schema, tool name, and expected fields."""
    if case.name in EDIT_FIXTURES:
        before, after = EDIT_FIXTURES[case.name]
        old, new = call.arguments.get("old"), call.arguments.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return ["invalid_literal_edit"]
        if before.count(old) != 1:
            return ["edit_match_not_unique"]
        if before.replace(old, new, 1) != after:
            return ["edit_wrong_result"]
    if case.name == "challenge_delegate_context":
        prompt = call.arguments.get("prompt")
        if not isinstance(prompt, str):
            return ["missing_self_contained_prompt"]
        missing = [name for name in ("src/old_parser.py", "src/new_parser.py") if name not in prompt]
        failures = ["delegate_missing_file_context"] if missing else []
        if not any(word in prompt.lower() for word in ("blank", "empty")):
            failures.append("delegate_missing_review_focus")
        if call.arguments.get("agent", "assistant") != "assistant":
            failures.append("delegate_unavailable_profile")
        # Whether the prose actually requests review without edits needs human
        # inspection; avoid pretending keyword checks capture its full meaning.
        return failures
    return []
