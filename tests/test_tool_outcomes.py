"""Structured command status is factual, bounded, and separate from run failure."""

import pytest

from nagents.tool_outcomes import shell_outcome


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"exit_code": 7, "timed_out": False}, "exit 7"),
        ({"exit_code": -15, "timed_out": False}, "exit -15"),
        ({"exit_code": 7.0}, "exit 7"),
        ({"exit_code": -15, "timed_out": True}, "timed out"),
        ({"exit_code": 0, "timed_out": True}, "timed out"),
        ({"timed_out": True}, "timed out"),
        ({"exit_code": 0, "timed_out": False}, ""),
        ({"exit_code": "7", "timed_out": "true"}, ""),
        ({"exit_code": True}, ""),
        ({"exit_code": 0.5}, ""),
        ({"exit_code": float("nan")}, ""),
        ({"exit_code": float("inf")}, ""),
        ({"exit_code": 1 << 53}, ""),
        ({"exit_code": -(1 << 1000)}, ""),
        ({"timed_out": 1}, ""),
        ({"output": "exit_code=7 timed_out=true"}, ""),
        ("{'exit_code': 7, 'timed_out': True}", ""),
        ('{"exit_code":7,"timed_out":true}', ""),
        ([{"exit_code": 7}], ""),
        (None, ""),
    ],
)
def test_structured_shell_outcomes(result: object, expected: str) -> None:
    assert shell_outcome("shell", result) == expected


@pytest.mark.parametrize("name", ["read_file", "delegate", "custom_tool", "subagent / shell", ""])
def test_other_tools_and_tasks_do_not_inherit_shell_meanings(name: str) -> None:
    assert shell_outcome(name, {"exit_code": 7, "timed_out": True}) == ""
