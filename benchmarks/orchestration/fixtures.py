"""Identical task fixtures and deterministic outcome grading for every strategy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Scenario:
    name: str
    goal: str
    files: dict[str, str]
    expected: dict[str, object]
    corrected_files: dict[str, str]


def scenarios() -> tuple[Scenario, ...]:
    ledgers = {
        "north": [{"kind": "sale", "cents": 1250}, {"kind": "refund", "cents": 250}, {"kind": "sale", "cents": 400}],
        "south": [{"kind": "sale", "cents": 990}, {"kind": "sale", "cents": 1600}, {"kind": "refund", "cents": 300}],
        "west": [{"kind": "refund", "cents": 80}, {"kind": "sale", "cents": 2100}, {"kind": "refund", "cents": 500}],
    }
    policies = {
        "api": {"public": True, "authentication": False, "audit_logging": False, "timeout_seconds": 15},
        "admin": {"public": False, "authentication": True, "audit_logging": True, "timeout_seconds": 90},
        "worker": {"public": True, "authentication": True, "audit_logging": False, "timeout_seconds": 60},
    }
    target = {
        "api": {"timeout_seconds": 90, "retries": 2},
        "worker": {"timeout_seconds": 30, "retries": 8},
        "batch": {"timeout_seconds": 50, "retries": 0},
    }
    fixed = {
        "api": {"timeout_seconds": 60, "retries": 2},
        "worker": {"timeout_seconds": 30, "retries": 5},
        "batch": {"timeout_seconds": 50, "retries": 0},
    }
    return (
        Scenario(
            "ledger_reconciliation",
            "Reconcile the three JSON ledgers in ledgers/. Sales add cents and refunds subtract cents. "
            "Return only a JSON object with keys net_cents_by_region (mapping each region filename stem to its net integer cents), "
            "total_net_cents, and total_entries. Inspect the files; do not change them.",
            {f"ledgers/{name}.json": json.dumps(rows, indent=2) + "\n" for name, rows in ledgers.items()},
            {
                "net_cents_by_region": {"north": 1400, "south": 2290, "west": 1520},
                "total_net_cents": 5210,
                "total_entries": 9,
            },
            {},
        ),
        Scenario(
            "policy_review",
            "Review the three JSON service configurations in services/. Rules: every public service must authenticate; "
            "every public service must enable audit logging; every timeout_seconds must be at most 60. "
            "Return only a JSON object with findings, a list of {service, rule} objects, and total_findings. "
            "Use rule identifiers public_authentication, public_audit_logging, timeout_limit respectively. "
            "Order does not matter. Inspect the files; do not change them.",
            {f"services/{name}.json": json.dumps(config, indent=2) + "\n" for name, config in policies.items()},
            {
                "findings": [
                    {"service": "api", "rule": "public_authentication"},
                    {"service": "api", "rule": "public_audit_logging"},
                    {"service": "admin", "rule": "timeout_limit"},
                    {"service": "worker", "rule": "public_audit_logging"},
                ],
                "total_findings": 4,
            },
            {},
        ),
        Scenario(
            "review_followthrough",
            "Review deployment.json against limits.json and correct every value exceeding its corresponding maximum. "
            "Preserve valid values, services, and other content. Only deployment.json may change. "
            "Do the corrections, then read back the saved result to verify. Return only a JSON object with "
            "changed_services (sorted list), corrected_fields (integer), and verified (boolean).",
            {
                "deployment.json": json.dumps(target, indent=2) + "\n",
                "limits.json": '{"timeout_seconds":60,"retries":5}\n',
            },
            {"changed_services": ["api", "worker"], "corrected_fields": 2, "verified": True},
            {"deployment.json": json.dumps(fixed, indent=2) + "\n"},
        ),
    )


def strategy_instruction(strategy: str) -> str:
    if strategy == "direct":
        return "Work directly on the task yourself. Delegation is unavailable."
    if strategy == "delegated":
        return (
            "Use two or three independent read-only reviewer children for separable parts of this task. "
            "Start their tasks together, before doing synthesis; use the configured reviewer profile. "
            "Give each child a self-contained task. Continue useful independent work while they run, "
            "then combine their findings. You remain responsible for the final answer and any required corrections and verification."
        )
    if strategy == "adaptive":
        return "Choose whether to work directly or delegate independent parts. Complete the task and verify the result."
    raise ValueError("Unknown strategy")


def parse_answer(text: str) -> object:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return "invalid_json"


def grade(scenario: Scenario, workspace: Path, answer: str, verified_paths: set[str]) -> list[str]:
    failures: list[str] = []
    actual = parse_answer(answer)
    expected = scenario.expected
    if scenario.name == "policy_review" and isinstance(actual, dict) and isinstance(actual.get("findings"), list):
        actual = dict(actual)
        actual["findings"] = sorted(actual["findings"], key=lambda item: json.dumps(item, sort_keys=True))
        expected_findings = expected["findings"]
        assert isinstance(expected_findings, list)
        expected = {
            **expected,
            "findings": sorted(expected_findings, key=lambda item: json.dumps(item, sort_keys=True)),
        }
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        failures.append("wrong_final_answer")
    for name, original in scenario.files.items():
        path = workspace / name
        if not path.is_file() or path.is_symlink():
            failures.append(f"missing_or_aliased_file:{name}")
            continue
        content = path.read_text()
        intended = scenario.corrected_files.get(name, original)
        if name in scenario.corrected_files:
            if parse_answer(content) != parse_answer(intended):
                failures.append(f"incorrect_saved_content:{name}")
            if name not in verified_paths:
                failures.append(f"missing_readback:{name}")
        elif content != original:
            failures.append(f"unexpected_mutation:{name}")
    allowed = set(scenario.files) | {".ngn/tools.yaml"}
    for path in workspace.rglob("*"):
        if path.is_file() and path.relative_to(workspace).as_posix() not in allowed:
            failures.append(f"unexpected_file:{path.relative_to(workspace).as_posix()}")
    return failures
