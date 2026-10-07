"""Offline validation of frozen splits, grading and efficiency eligibility."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .cases import cases
from .run import assess
from .run import grade
from .run import source_hashes

if TYPE_CHECKING:
    from pathlib import Path


def test_splits_are_declared_disjoint_and_fixture_variants_differ() -> None:
    development = [case for case in cases() if case.split == "development"]
    heldout = [case for case in cases() if case.split == "heldout"]
    assert len(development) == len(heldout) == 10
    assert {case.scenario.name for case in development}.isdisjoint(case.scenario.name for case in heldout)
    for first, second in zip(development, heldout, strict=True):
        assert first.scenario.goal != second.scenario.goal
    assert all(not case.scenario.corrected_files for case in cases() if "authorized" not in case.scenario.name)


def test_clarification_requires_actual_question_and_no_mutation(tmp_path: Path) -> None:
    scenario = next(case.scenario for case in cases() if case.scenario.name == "development_ambiguous_edit")
    for name, text in scenario.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert "missing_clarification_question" in grade(scenario, tmp_path, '{"needs_clarification":true}', set())
    answer = '{"needs_clarification":true,"question":"Which environment configuration should I change?"}'
    assert grade(scenario, tmp_path, answer, set()) == []
    path.write_text("{}")
    assert any("unexpected_mutation" in failure for failure in grade(scenario, tmp_path, answer, set()))


def test_authorized_correction_requires_saved_readback(tmp_path: Path) -> None:
    scenario = next(case.scenario for case in cases() if case.scenario.name == "development_authorized_correction")
    for name, text in scenario.corrected_files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    answer = json.dumps(scenario.expected)
    assert grade(scenario, tmp_path, answer, set()) == [f"missing_readback:{name}"]
    assert grade(scenario, tmp_path, answer, {name}) == []


def test_tool_free_intent_fails_unnecessary_calls_and_incomplete_usage() -> None:
    case = cases()[0]
    result: dict[str, object] = {
        "failures": [],
        "tool_events": [],
        "usage": {"usage_complete": True},
        "run_complete": True,
        "source_changed": False,
        "provider_errors": [],
        "child_failures": 0,
    }
    assert assess(case, result)["efficiency_eligible"] is True
    result["tool_events"] = [{"tool": "read_file"}]
    assert assess(case, result)["correct"] is False
    result["tool_events"] = []
    result["failures"] = []
    result["usage"] = {"usage_complete": False}
    assert assess(case, result)["efficiency_eligible"] is False


def test_source_protection_covers_native_harness_and_benchmark() -> None:
    hashes = source_hashes()
    assert "src/nagents/harness/runtime.py" in hashes
    assert "benchmarks/intent/cases.py" in hashes
    assert "benchmarks/orchestration/telemetry.py" in hashes


def test_denied_edit_attempt_is_intent_failure_despite_correct_answer() -> None:
    case = next(case for case in cases() if case.scenario.name == "development_review_question")
    result: dict[str, object] = {
        "task_correct": True,
        "failures": [],
        "tool_events": [{"tool": "edit", "error": "approval_denied"}],
        "usage": {"usage_complete": True},
        "run_complete": True,
        "source_changed": False,
        "provider_errors": [],
        "child_failures": 0,
    }
    assessed = assess(case, result)
    assert assessed["task_correct"] is True
    assert assessed["intent_compliant"] is False
    assert assessed["correct"] is False
    assert assessed["intent_failures"] == ["unrequested_mutation_attempt"]
    assert assessed["efficiency_eligible"] is False


def test_harmless_explanatory_fields_do_not_fail_required_contract(tmp_path: Path) -> None:
    case = cases()[0]
    assert (
        grade(
            case.scenario, tmp_path, '{"idempotent":true,"explanation":"Repeating assignment changes nothing."}', set()
        )
        == []
    )
    assert "wrong_final_answer" in grade(case.scenario, tmp_path, '{"explanation":"No answer."}', set())


def test_advice_inspection_is_correct_but_exceeds_soft_efficiency_target() -> None:
    case = next(case for case in cases() if case.scenario.name == "development_advice_only")
    result: dict[str, object] = {
        "task_correct": True,
        "failures": [],
        "tool_events": [{"tool": "read_file", "error": ""}],
        "usage": {"usage_complete": True},
        "run_complete": True,
        "source_changed": False,
        "provider_errors": [],
        "child_failures": 0,
    }
    assessed = assess(case, result)
    assert assessed["correct"] is True
    assert assessed["within_action_target"] is False
