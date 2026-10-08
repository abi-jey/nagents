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


def test_generalization_suite_is_separate_and_prompts_hide_oracles() -> None:
    original = cases()
    novel = cases("generalization")
    assert len(original) == 20
    assert len(novel) == 7
    assert cases("all") == original + novel
    assert not {case.case_id for case in original} & {case.case_id for case in novel}
    for case in novel:
        assert case.split == "heldout"
        assert len(case.prompt.split()) <= 16
        assert not any(word in case.prompt for word in ("read_file", "authorized_paths", "expected_blocked", "grader"))
        for term in case.answer_terms:
            assert term not in case.prompt
        assert set(case.expected_files) == set(case.authorized_paths)


def test_novel_grading_checks_all_artifacts_and_needed_answers(tmp_path: Path) -> None:
    for case in cases("generalization"):
        workspace = tmp_path / case.case_id
        workspace.mkdir()
        for name, content in {**case.files, **case.expected_files}.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
        answer = " ".join(case.answer_terms) or "Please clarify the intended file."
        assert grade(case, workspace, answer) == []
        assert "missing_final_response" in grade(case, workspace, "")
        for term in case.answer_terms:
            assert f"answer_missing_fact:{term}" in grade(case, workspace, "Unknown.")
        sibling = next(iter(case.files))
        (workspace / sibling).write_text("collateral edit")
        assert f"incorrect_saved_content:{sibling}" in grade(case, workspace, answer)
        (workspace / "invented.txt").write_text("fabricated")
        assert "unexpected_file:invented.txt" in grade(case, workspace, answer)


def test_blocked_controls_never_authorize_guessing() -> None:
    novel = cases("generalization")
    blocked = [case for case in novel if case.expected_blocked]
    assert {case.case_id for case in blocked} == {"absent_file", "ambiguous_existing_edit"}
    assert all(not case.authorized_paths and not case.expected_files and not case.answer_terms for case in blocked)
    creation = next(case for case in novel if case.case_id == "create_nested_file")
    assert not set(creation.expected_files) & set(creation.files)


def test_creation_targets_have_existing_parents_without_target_files(tmp_path: Path) -> None:
    for case in cases("all"):
        new_paths = set(case.expected_files) - set(case.files)
        if not new_paths:
            continue
        workspace = tmp_path / f"{case.split}-{case.case_id}"
        workspace.mkdir()
        for name, content in case.files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
        for name in new_paths:
            target = workspace / name
            assert target.parent.is_dir()
            assert not target.exists()
