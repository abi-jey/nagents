"""Guard functional scoring against losing bytes or collateral edits."""

from pathlib import Path

from .cases import cases
from .cases import grade


def test_edit_grading_preserves_crlf_and_untouched_content(tmp_path: Path) -> None:
    case = next(c for c in cases() if c.case_id == "crlf_edit" and c.split == "development")
    path = tmp_path / "server.ini"
    wanted = case.expected_files["server.ini"]
    path.write_bytes(wanted.encode())
    assert grade(case, tmp_path, "Updated.") == []
    path.write_bytes(wanted.replace("\r\n", "\n").encode())
    assert "incorrect_saved_content:server.ini" in grade(case, tmp_path, "Updated.")
    path.write_bytes(wanted.encode())
    (tmp_path / "unexpected.txt").write_text("extra")
    assert "unexpected_file:unexpected.txt" in grade(case, tmp_path, "Updated.")


def test_denied_control_requires_unchanged_file(tmp_path: Path) -> None:
    case = next(c for c in cases() if c.case_id == "permission_denial" and c.split == "development")
    path = tmp_path / "network.ini"
    path.write_text(case.files["network.ini"])
    assert case.expected_blocked
    assert grade(case, tmp_path, "Approval was denied.") == []
    path.write_text("port=9090\n")
    assert "incorrect_saved_content:network.ini" in grade(case, tmp_path, "Done.")
